# ไม่ต้องใช้ eventlet.monkey_patch()
import os
import subprocess
import threading
import select
# ไม่ต้องใช้ pyaudio
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

# --- Configuration ---
USB_SOUND_CARD = 'plughw:1,0'  # ใช้ plughw เพื่อความเข้ากันได้สูงสุด
VOX_THRESHOLD = 500  # ปรับค่าตามความเหมาะสม
# --- ตั้งค่าสำหรับ arecord ที่เหมาะสมกับ Pi Zero 2 W ---
CAPTURE_RATE = "16000" # "16000" คืออีกทางเลือกที่ปลอดภัย
CAPTURE_FORMAT = "S16_LE"

app = Flask(__name__)
# ใช้ Secret Key ของคุณ
app.config['SECRET_KEY'] = '2aa05de948623906118908dda27ec3f7255b6b097329fb20'
socketio = SocketIO(app, async_mode='eventlet')

# --- PTT Lock (จากขั้นตอนแก้ Race Condition) ---
ptt_process = None
is_transmitting = False
ptt_lock = threading.Lock()

# --- Web Interface ---
@app.route('/')
def index():
    return render_template('index.html')

# --- SocketIO Event Handlers (เหมือนเดิม) ---
@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')
    if is_transmitting:
        stop_ptt() # หยุด PTT ถ้า client หลุด

@socketio.on('control_volume')
def handle_volume(data):
    # ฟังก์ชันปรับเสียง (ถ้าต้องการใช้)
    # set_alsa_volume(data.get('control'), data.get('value'))
    pass

@socketio.on('ptt_start')
def handle_ptt_start():
    global ptt_process, is_transmitting
    with ptt_lock:
        if is_transmitting:
            return
        print("PTT started by client. Stopping radio listener and starting aplay...")
        stop_radio_listen_thread() # หยุดฟังก่อนพูด
        is_transmitting = True
        
        ptt_process = subprocess.Popen(
            ['aplay', '-D', USB_SOUND_CARD, '-f', CAPTURE_FORMAT, '-r', CAPTURE_RATE, '-c', '1', '-t', 'raw'],
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )

@socketio.on('audio_chunk_to_server')
def handle_audio_chunk(chunk):
    if ptt_process and ptt_process.stdin and not ptt_process.stdin.closed:
        try:
            ptt_process.stdin.write(chunk)
        except (BrokenPipeError, IOError, ValueError):
            print("Pipe to aplay is broken. Stopping PTT.")
            if is_transmitting:
                stop_ptt()

@socketio.on('ptt_stop')
def handle_ptt_stop():
    stop_ptt()

def stop_ptt():
    global ptt_process, is_transmitting
    with ptt_lock:
        if not is_transmitting:
            return
        print("PTT process stopping.")
        is_transmitting = False
        
        if ptt_process:
            if ptt_process.stdin and not ptt_process.stdin.closed:
                ptt_process.stdin.close()
            if ptt_process.poll() is None:
                ptt_process.terminate()
                ptt_process.wait()
            ptt_process = None
            print("aplay process stopped.")
        
        # กลับมาเริ่มฟังใหม่หลังจากพูดเสร็จ
        start_radio_listen_thread()

# --- Radio Listening Thread (กลับมาใช้ Subprocess) ---
radio_listen_thread = None
stop_thread = threading.Event()

def radio_listen():
    global is_transmitting
    command = ['arecord', '-D', USB_SOUND_CARD, '-f', CAPTURE_FORMAT, '-r', CAPTURE_RATE, '-c', '1', '-t', 'raw']
    
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    print(f"Subprocess 'arecord' started with PID {process.pid}")
    is_receiving = False
    
    while not stop_thread.is_set():
        if is_transmitting:
            socketio.sleep(0.1)
            continue
            
        r, _, _ = select.select([process.stdout], [], [], 0.01)
        if not r:
            continue

        audio_chunk = process.stdout.read(1024)
        if not audio_chunk:
            break

        # VOX Logic (เหมือนเดิม)
        try:
            samples = [int.from_bytes(audio_chunk[i:i+2], 'little', signed=True) for i in range(0, len(audio_chunk), 2)]
            rms = (sum(s**2 for s in samples) / len(samples))**0.5
        except (ValueError, ZeroDivisionError):
            rms = 0

        if rms > VOX_THRESHOLD:
            if not is_receiving:
                is_receiving = True
                socketio.emit('radio_status', {'status': 'receiving'})
            socketio.emit('audio_chunk_from_server', audio_chunk)
        elif is_receiving:
            is_receiving = False
            socketio.emit('radio_status', {'status': 'idle'})

    process.terminate()
    process.wait()
    print("arecord process stopped.")

# --- Thread Management (เหมือนเดิม) ---
def start_radio_listen_thread():
    global radio_listen_thread, stop_thread
    if radio_listen_thread is None or not radio_listen_thread.is_alive():
        stop_thread.clear()
        radio_listen_thread = socketio.start_background_task(target=radio_listen)
        print("Starting radio listener thread...")

def stop_radio_listen_thread():
    global stop_thread
    if radio_listen_thread and radio_listen_thread.is_alive():
        stop_thread.set()
        print("Stopping radio listener thread...")

# --- Main Execution Block (สำหรับ Nginx + Eventlet) ---
if __name__ == '__main__':
    try:
        start_radio_listen_thread()
        print("Starting Flask-SocketIO server on http://127.0.0.1:8000 for Nginx proxy...")
        # รันบน localhost:8000 เพื่อให้ Nginx รับช่วงต่อ
        socketio.run(app, host='127.0.0.1', port=8000)
    except KeyboardInterrupt:
        print("Server shutting down...")
    finally:
        stop_radio_listen_thread()
