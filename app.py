from flask import Flask, request, Response
import os, requests, threading, time, io
import json

app = Flask(__name__)

YEMOT_TOKEN = os.environ.get('YEMOT_TOKEN', '')
BRAIN_URL = os.environ.get('BRAIN_URL', 'https://voice-brain-o2ym.onrender.com')
EXT = os.environ.get('YEMOT_EXT', '2')
AGENT_EXT = os.environ.get('AGENT_EXT', '5')
YBASE = 'https://www.call2all.co.il/ym/api'
import json as _json
SEEN_PATH = os.environ.get('SEEN_PATH', '/tmp/voice_seen.json')
try:
    seen = set(_json.load(open(SEEN_PATH)))
except Exception:
    seen = set()

def save_seen():
    try:
        keep = sorted(seen)[-400:]
        _json.dump(keep, open(SEEN_PATH, 'w'))
    except Exception:
        pass

first_run = True
import time as _t
START_TS = _t.time()
status = {'last_poll': 'never', 'last_error': '', 'processed': []}

def _file_ts(ds):
    try:
        import datetime as _d
        dt = _d.datetime.strptime(ds, '%d/%m/%Y %H:%M')
        return dt.timestamp() - 3 * 3600  # wall clock is Israel time (UTC+3)
    except Exception:
        return 0

def brain_voice(wav_bytes, fname='rec.wav', agent=False):
    ep = '/voice-agent' if agent else '/voice'
    r = requests.post(f'{BRAIN_URL}{ep}', files={'audio': (fname, wav_bytes, 'audio/wav')}, timeout=120)
    if r.headers.get('Content-Type', '').startswith('audio'):
        return None, r.content
    try:
        j = r.json()
        return j.get('reply', j.get('error', 'שגיאה')), None
    except Exception:
        return 'שגיאה בעיבוד', None

def next_num_name(path=None):
    path = path or EXT
    try:
        r = requests.get(f'{YBASE}/GetIVR2Dir', params={'token': YEMOT_TOKEN, 'path': path}, timeout=30).json()
        status['dir_status'] = str(r.get('responseStatus', ''))[:50]
        status['files_count'] = len(r.get('files', []))
        status['ext'] = EXT
        nums = []
        for f in r.get('files', []):
            n = (f.get('name') or '')
            if n.endswith('.wav') and n[:-4].isdigit():
                nums.append(int(n[:-4]))
        return f'{max(nums) + 1:03d}.wav' if nums else '001.wav'
    except Exception:
        return f'Reply-{int(time.time())}.wav'

def upload_reply_audio(audio, path=None):
    path = path or EXT
    name = next_num_name(path)
    requests.post(f'{YBASE}/UploadFile',
        params={'token': YEMOT_TOKEN, 'path': f'ivr2:/{path}/{name}', 'convertAudio': 1},
        files={'file': ('ans.mp3', audio, 'audio/mpeg')}, timeout=120)
    seen.add(f'ivr2:/{path}/{name}')
    save_seen()
    return name

def yemot_upload_tts(text, dest):
    r = requests.post(f'{YBASE}/UploadTextFile', data={'token': YEMOT_TOKEN, 'what': dest, 'contents': text}, timeout=30)
    seen.add(dest)
    return r.text[:200]

def handle_media(url):
    dl = requests.get(url, timeout=300)
    if len(dl.content) > 150_000_000:
        return None
    return upload_reply_audio(dl.content)

def handle_mediaq(query):
    try:
        from urllib.parse import quote_plus
        r = requests.get(f'{ORACLE_MEDIA}/media', params={'key': MEDIA_KEY, 'q': query}, timeout=600)
        if r.ok and len(r.content) > 10000:
            return upload_reply_audio(r.content)
    except Exception:
        pass
    try:
        from gtts import gTTS
        import io as _io
        buf = _io.BytesIO()
        gTTS('לא מצאתי את המבוקש. אמור בבקשה את השם לאט ובצורה ברורה.', lang='iw').write_to_fp(buf)
        return upload_reply_audio(buf.getvalue())
    except Exception:
        return None

def handle_agent(task):
    try:
        import time as _t
        r = requests.post(f'{ORACLE_MEDIA}/agent-task', params={'key': MEDIA_KEY},
                          json={'task': task}, timeout=60).json()
        tid = r.get('id', '')
        status['agent_task'] = tid + ': polling'
        for _ in range(36):
            _t.sleep(20)
            s = requests.get(f'{ORACLE_MEDIA}/agent-result', params={'key': MEDIA_KEY, 'id': tid}, timeout=60).json()
            if s.get('status') == 'done':
                status['agent_task'] = tid + ': done'
                if s.get('files'):
                    f = requests.get(f'{ORACLE_MEDIA}/agent-file', params={'key': MEDIA_KEY, 'p': s['files'][0]}, timeout=300)
                    if len(f.content) > 10000:
                        return upload_reply_audio(f.content)
                    return None
                import re as _re
                m = _re.search(r'https?://\S+', s.get('result') or '')
                if m:
                    u = m.group(0).rstrip(').,')
                    try:
                        d = requests.get(f'{ORACLE_MEDIA}/media', params={'key': MEDIA_KEY, 'url': u}, timeout=600)
                        if d.ok and len(d.content) > 10000:
                            return upload_reply_audio(d.content)
                    except Exception:
                        pass
                return None
            if s.get('status') == 'error':
                status['agent_task'] = tid + ': error'
                return None
    except Exception as e:
        status['agent_task'] = 'failed: ' + str(e)[:150]
        pass
    return None

def handle_agent_async(task):
    threading.Thread(target=handle_agent, args=(task,), daemon=True).start()
    status['agent_task'] = 'started: ' + task[:80]

def handle_mediaq_async(query):
    threading.Thread(target=handle_mediaq, args=(query,), daemon=True).start()

ORACLE_MEDIA = os.environ.get('ORACLE_BROWSE_URL', 'http://129.159.159.222:8080')
MEDIA_KEY = os.environ.get('BROWSE_KEY', 'tzvi-free-1234')

@app.route('/health')
def health():
    return 'ok'

@app.route('/debug')
def debug():
    import json as _j
    return Response(_j.dumps(status, ensure_ascii=False), mimetype='application/json')

_POLL_LOCK = threading.Lock()

@app.route('/poll-now')
def poll_now():
    # serialise: cron keepalive, manual trigger and the 30s loop must never
    # process the same recording twice. acquired with a timeout so a stuck
    # poll can never freeze the loop permanently.
    if not _POLL_LOCK.acquire(blocking=False):
        return Response(json.dumps(status, ensure_ascii=False), mimetype='application/json')
    try:
        return _poll_now_inner()
    finally:
        try:
            _POLL_LOCK.release()
        except Exception:
            pass

def _poll_now_inner():
    only = request.args.get('file', '')
    if only:
        try:
            # force-reprocess a specific recording exactly once: mark it seen
            # BEFORE work so a concurrent poll can't answer it a second time.
            seen.add(only)
            save_seen()
            dl = requests.get(f'{YBASE}/DownloadFile', params={'token': YEMOT_TOKEN, 'path': only}, timeout=30)
            text, audio = brain_voice(dl.content, only.split('/')[-1])
            status['processed'].append(only)
            if text and (text.startswith('MEDIA:') or text.startswith('MEDIAQ:') or text.startswith('AGENT:')):
                if text.startswith('AGENT:'):
                    handle_agent_async(text[6:].strip())
                    status['note'] = 'browser-agent task started in background'
                elif text.startswith('MEDIAQ:'):
                    handle_mediaq_async(text[7:].strip())
                    status['note'] = 'media download started in background'
                else:
                    handle_media(text[6:].strip())
            elif audio:
                upload_reply_audio(audio)
            elif text:
                dest = f"ivr2:/{EXT}/Reply-{int(time.time())}.tts"
                yemot_upload_tts(text[:1000], dest)
        except Exception as e:
            status['last_error'] = str(e)[:300]
        import json as _j
        return Response(_j.dumps(status, ensure_ascii=False), mimetype='application/json')
    poll_once(EXT)
    poll_once(AGENT_EXT)
    return Response(json.dumps(status, ensure_ascii=False), mimetype='application/json')

@app.route('/hook')
def hook():
    rec = request.args.get('rec', '')
    if not rec:
        back = request.host_url.rstrip('/') + '/hook'
        return Response(f'record={back}?rec=RECFILE&val=1&len=10', mimetype='text/plain')
    try:
        dl = requests.get(f'{YBASE}/DownloadFile', params={'token': YEMOT_TOKEN, 'path': rec}, timeout=30)
        text, audio = brain_voice(dl.content)
        if text and (text.startswith('MEDIA:') or text.startswith('MEDIAQ:') or text.startswith('AGENT:')):
            if text.startswith('AGENT:'):
                name = handle_agent(text[6:].strip())
            elif text.startswith('MEDIAQ:'):
                name = handle_mediaq(text[7:].strip())
            else:
                name = handle_media(text[6:].strip())
            if name:
                return Response(f'playfile={EXT}/{name[:-4]}', mimetype='text/plain')
            return Response('read=t-לא הצלחתי להוריד את השמע', mimetype='text/plain')
        if audio:
            name = upload_reply_audio(audio)
            return Response(f'playfile={EXT}/{name[:-4]}', mimetype='text/plain')
        safe = (text or '')[:900].replace('\n', ' ')
        return Response(f'read=t-{safe}', mimetype='text/plain')
    except Exception as e:
        return Response(f'read=t-שגיאה: {str(e)[:100]}', mimetype='text/plain')

def poll_once(path=None):
    global first_run
    path = path or EXT
    status['last_poll'] = time.strftime('%H:%M:%S')
    try:
        r = requests.get(f'{YBASE}/GetIVR2Dir', params={'token': YEMOT_TOKEN, 'path': path}, timeout=30).json()
        status['dir_status'] = str(r.get('responseStatus', ''))[:50]
        status['files_count'] = len(r.get('files', []))
        status['ext'] = EXT
        if first_run:
            for f in r.get('files', []):
                what = f.get('what') or ''
                if what.endswith('.wav') and _file_ts(f.get('date') or '') < START_TS - 600:
                    seen.add(what)
            save_seen()
            first_run = False
            status['note'] = 'baseline marked (old only)'
            return
        for f in r.get('files', []):
            what = f.get('what', '')
            if what in seen or not what.endswith('.wav'):
                continue
            if f.get('source') != 'Record':
                seen.add(what)
                save_seen()
                continue
            # do NOT mark seen before success - only after we actually answer
            try:
                dl = requests.get(f'{YBASE}/DownloadFile', params={'token': YEMOT_TOKEN, 'path': what}, timeout=30)
                text, audio = brain_voice(dl.content, what.split('/')[-1], agent=(path == AGENT_EXT))
            except Exception:
                continue
            seen.add(what)
            save_seen()
            status['processed'].append(what)
            if text and (text.startswith('MEDIA:') or text.startswith('MEDIAQ:') or text.startswith('AGENT:')):
                if text.startswith('AGENT:'):
                    handle_agent_async(text[6:].strip())
                    status['note'] = 'browser-agent task started in background'
                elif text.startswith('MEDIAQ:'):
                    handle_mediaq_async(text[7:].strip())
                    status['note'] = 'media download started in background'
                else:
                    handle_media(text[6:].strip())
            elif audio:
                upload_reply_audio(audio)
            elif text:
                dest = f"ivr2:/{path}/Reply-{int(time.time())}.tts"
                yemot_upload_tts(text[:1000], dest)
    except Exception as e:
        status['last_error'] = str(e)[:300]

def loop():
    while True:
        got = False
        try:
            got = _POLL_LOCK.acquire(blocking=False)
            if got:
                poll_once(EXT)          # ext 2 = chat bot
                poll_once(AGENT_EXT)    # ext 5 = full browser agent
        except Exception as e:
            status['last_error'] = 'loop: ' + str(e)[:200]
        finally:
            if got:
                try:
                    _POLL_LOCK.release()
                except Exception:
                    pass
        time.sleep(30)

def _start_loop():
    threading.Thread(target=loop, daemon=True).start()

_start_loop()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
