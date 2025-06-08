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
CAPTURE_RATE = "16000"  # ค่าที่เหมาะสมสำหรับ Pi Zero 2 W
CAPTURE_FORMAT = "S16_LE"

app = Flask(__name__)
app.config['SECRET_KEY'] = '2aa05de948623906118908dda27ec3f7255b6b097329fb20' # ใช้ Secret Key ของคุณ
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

# --- Global Variables & Lock ---
ptt_process = None
radio_listen_thread = None
stop_thread = threading.Event()
is_transmitting = False
ptt_lock = threading.Lock()

# --- Web Interface ---
@app.route('/')
def index():
    return render_template('index.html')

# --- SocketIO Event Handlers ---
@socketio.on('connect')
def handle_connect(auth): # <-- [การแก้ไขที่ 1] เพิ่ม (auth)
    print(f'Client connected. Auth: {auth}')
    # เริ่ม thread ฟังเสียงเมื่อมีคนเชื่อมต่อเข้ามา
    start_radio_listen_thread()

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')
    if is_transmitting:
        stop_ptt()

# --- PTT Logic: ควบคุมการสลับใช้งานระหว่าง arecord และ aplay ---

@socketio.on('ptt_start')
def handle_ptt_start():
    global ptt_process, is_transmitting
    with ptt_lock:
        if is_transmitting:
            return
        
        print("PTT start request received.")
        # 1. หยุด Thread ที่กำลังฟังเสียง (arecord) ก่อน
        stop_radio_listen_thread()
        socketio.sleep(0.1) # รอเล็กน้อยเพื่อให้ thread หยุดสนิท

        is_transmitting = True
        
        # 2. เริ่มโปรเซส aplay เพื่อรอรับเสียง
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
    with ptt_lock:
        if not is_transmitting:
            return
        
        print("PTT stop request received.")
        is_transmitting = False
        
        if ptt_process:
            if ptt_process.stdin and not ptt_process.stdin.closed:
                ptt_process.stdin.close()
            if ptt_process.poll() is None:
                ptt_process.terminate()
                ptt_process.wait()
            ptt_process = None
            print("aplay process stopped.")
        
        socketio.sleep(0.1)
        start_radio_listen_thread()


# --- Radio Listening Thread (ใช้ Subprocess) ---

def radio_listen():
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


# --- Thread Management ---
def start_radio_listen_thread():
    global radio_listen_thread
    with ptt_lock:
        # [การแก้ไขที่ 2] เปลี่ยนเงื่อนไขการตรวจสอบ
        if radio_listen_thread is None:
            stop_thread.clear()
            radio_listen_thread = socketio.start_background_task(target=radio_listen)
            print("Radio listener thread started.")

def stop_radio_listen_thread():
    global radio_listen_thread
    with ptt_lock:
        if radio_listen_thread:
            print("Stopping radio listener thread...")
            stop_thread.set()
            # การ join อาจทำให้เกิดปัญหา blocking ใน eventlet เราจะปล่อยให้มันจบเอง
            # และตั้งค่าเป็น None เพื่อให้สามารถเริ่มใหม่ได้
            radio_listen_thread = None


# --- Main Execution Block (สำหรับ Nginx + Eventlet) ---
if __name__ == '__main__':
    try:
        print("Starting Flask-SocketIO server on http://127.0.0.1:8000 for Nginx proxy...")
        socketio.run(app, host='127.0.0.1', port=8000, log_output=True)
    except KeyboardInterrupt:
        print("Server shutting down...")
    finally:
        stop_radio_listen_thread()
