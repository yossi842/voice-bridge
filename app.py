from flask import Flask, request, Response
import os, requests, threading, time, io

app = Flask(__name__)

YEMOT_TOKEN = os.environ.get('YEMOT_TOKEN', '')
BRAIN_URL = os.environ.get('BRAIN_URL', 'https://voice-brain-o2ym.onrender.com')
EXT = os.environ.get('YEMOT_EXT', '1')
YBASE = 'https://www.call2all.co.il/ym/api'
seen = set()
status = {'last_poll': 'never', 'last_error': '', 'processed': []}

def brain_voice(wav_bytes, fname='rec.wav'):
    r = requests.post(f'{BRAIN_URL}/voice', files={'audio': (fname, wav_bytes, 'audio/wav')}, timeout=120)
    if r.headers.get('Content-Type', '').startswith('audio'):
        return None, r.content
    try:
        j = r.json()
        return j.get('reply', j.get('error', 'שגיאה')), None
    except Exception:
        return 'שגיאה בעיבוד', None

def next_num_name():
    try:
        r = requests.get(f'{YBASE}/GetIVR2Dir', params={'token': YEMOT_TOKEN, 'path': EXT}, timeout=30).json()
        nums = []
        for f in r.get('files', []):
            n = (f.get('name') or '')
            if n.endswith('.wav') and n[:-4].isdigit():
                nums.append(int(n[:-4]))
        return f'{max(nums) + 1:03d}.wav' if nums else '001.wav'
    except Exception:
        return f'Reply-{int(time.time())}.wav'

def upload_reply_audio(audio):
    name = next_num_name()
    requests.post(f'{YBASE}/UploadFile',
        params={'token': YEMOT_TOKEN, 'path': f'ivr2:/{EXT}/{name}', 'convertAudio': 1},
        files={'file': ('ans.mp3', audio, 'audio/mpeg')}, timeout=60)
    seen.add(f'ivr2:/{EXT}/{name}')
    return name

def yemot_upload_tts(text, dest):
    r = requests.post(f'{YBASE}/UploadTextFile', data={'token': YEMOT_TOKEN, 'what': dest, 'contents': text}, timeout=30)
    seen.add(dest)
    return r.text[:200]

@app.route('/health')
def health():
    return 'ok'

@app.route('/debug')
def debug():
    import json as _j
    return Response(_j.dumps(status, ensure_ascii=False), mimetype='application/json')

@app.route('/hook')
def hook():
    rec = request.args.get('rec', '')
    if not rec:
        back = request.host_url.rstrip('/') + '/hook'
        return Response(f'record={back}?rec=RECFILE&val=1&len=10', mimetype='text/plain')
    try:
        dl = requests.get(f'{YBASE}/DownloadFile', params={'token': YEMOT_TOKEN, 'path': rec}, timeout=30)
        text, audio = brain_voice(dl.content)
        if audio:
            name = upload_reply_audio(audio)
            return Response(f'playfile={EXT}/{name[:-4]}', mimetype='text/plain')
        safe = (text or '')[:900].replace('\n', ' ')
        return Response(f'read=t-{safe}', mimetype='text/plain')
    except Exception as e:
        return Response(f'read=t-שגיאה: {str(e)[:100]}', mimetype='text/plain')

def poll_once():
    status['last_poll'] = time.strftime('%H:%M:%S')
    try:
        r = requests.get(f'{YBASE}/GetIVR2Dir', params={'token': YEMOT_TOKEN, 'path': EXT}, timeout=30).json()
        for f in r.get('files', []):
            what = f.get('what', '')
            if what in seen or not what.endswith('.wav'):
                continue
            if f.get('source') != 'Record':
                seen.add(what)
                continue
            seen.add(what)
            if len(seen) > 200:
                seen.clear()
            dl = requests.get(f'{YBASE}/DownloadFile', params={'token': YEMOT_TOKEN, 'path': what}, timeout=30)
            text, audio = brain_voice(dl.content, what.split('/')[-1])
            status['processed'].append(what)
            if audio:
                upload_reply_audio(audio)
            elif text:
                dest = f"ivr2:/{EXT}/Reply-{int(time.time())}.tts"
                yemot_upload_tts(text[:1000], dest)
    except Exception as e:
        status['last_error'] = str(e)[:300]

def loop():
    while True:
        poll_once()
        time.sleep(30)

threading.Thread(target=loop, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
