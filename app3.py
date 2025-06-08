# --- ขั้นตอนที่สำคัญที่สุด: Monkey Patch ต้องอยู่บนสุด ---
import eventlet
eventlet.monkey_patch()

# --- Imports อื่นๆ ---
import os
import subprocess
import threading
import select
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

# --- Configuration ---
USB_SOUND_CARD = 'plughw:1,0'
VOX_THRESHOLD = 500
CAPTURE_RATE = "16000"
CAPTURE_FORMAT = "S16_LE"

app = Flask(__name__)
app.config['SECRET_KEY'] = '2aa05de948623906118908dda27ec3f7255b6b097329fb20'
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

# --- Global Variables & Lock ---
ptt_process = None
radio_listen_thread = None
stop_thread = threading.Event()
is_transmitting = False
ptt_lock = threading.Lock() # Lock ยังคงจำเป็นสำหรับจัดการสถานะหลัก

# --- Web Interface ---
@app.route('/')
def index():
    return render_template('index.html')

# --- SocketIO Event Handlers ---
@socketio.on('connect')
def handle_connect(auth):
    print(f'Client connected. Auth: {auth}')
    start_radio_listen_thread()

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')
    if is_transmitting:
        stop_ptt()

# --- PTT Logic ---
@socketio.on('ptt_start')
def handle_ptt_start():
    global ptt_process, is_transmitting
    with ptt_lock: # <-- Lock ถูกควบคุมจากที่นี่ที่เดียว
        if is_transmitting:
            return
        
        print("PTT start request received.")
        stop_radio_listen_thread() # เรียกฟังก์ชันย่อย (ที่ไม่มี lock แล้ว)
        socketio.sleep(0.1)

        is_transmitting = True
        
        print("Starting aplay process...")
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
        except (IOError, ValueError):
            print("Pipe to aplay broke. Stopping PTT.")
            if is_transmitting:
                stop_ptt()

@socketio.on('ptt_stop')
def handle_ptt_stop():
    stop_ptt()

def stop_ptt():
    global ptt_process, is_transmitting
    with ptt_lock: # <-- Lock ถูกควบคุมจากที่นี่ที่เดียว
        if not is_transmitting:
            return
        
        print("PTT stop request received.")
        is_transmitting = False
        
        if ptt_process:
            # ... (ส่วนจัดการ aplay เหมือนเดิม) ...
            if ptt_process.stdin and not ptt_process.stdin.closed:
                ptt_process.stdin.close()
            if ptt_process.poll() is None:
                ptt_process.terminate()
                ptt_process.wait()
            ptt_process = None
            print("aplay process stopped.")
        
        socketio.sleep(0.1)
        start_radio_listen_thread() # เรียกฟังก์ชันย่อย (ที่ไม่มี lock แล้ว)

# --- Radio Listening Thread ---
def radio_listen():
    # ... (โค้ดในฟังก์ชันนี้เหมือนเดิมทุกประการ) ...
    print("Radio listener thread starting 'arecord'...")
    command = ['arecord', '-D', USB_SOUND_CARD, '-f', CAPTURE_FORMAT, '-r', CAPTURE_RATE, '-c', '1', '-t', 'raw']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    is_receiving = False
    while not stop_thread.is_set():
        audio_chunk = process.stdout.read(1024)
        if not audio_chunk:
            print("arecord process stream ended.")
            break
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
    if process.poll() is None:
        process.terminate()
        process.wait()
    print("Radio listener thread stopped.")

# --- Thread Management (เวอร์ชันแก้ไข) ---
def start_radio_listen_thread():
    global radio_listen_thread
    # [การแก้ไข] เอา 'with ptt_lock:' ออก
    if radio_listen_thread is None:
        stop_thread.clear()
        radio_listen_thread = socketio.start_background_task(target=radio_listen)
        print("Radio listener thread started.")

def stop_radio_listen_thread():
    global radio_listen_thread
    # [การแก้ไข] เอา 'with ptt_lock:' ออก
    if radio_listen_thread:
        print("Stopping radio listener thread...")
        stop_thread.set()
        radio_listen_thread = None

# --- Main Execution Block ---
if __name__ == '__main__':
    try:
        print("Starting Flask-SocketIO server on http://127.0.0.1:8000 for Nginx proxy...")
        socketio.run(app, host='127.0.0.1', port=8000, log_output=True)
    except KeyboardInterrupt:
        print("Server shutting down...")
    finally:
        stop_radio_listen_thread()
