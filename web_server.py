import os
import subprocess
import threading
import select
import socket
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

# --- Configuration ---
# ไม่ต้องมีการตั้งค่าเกี่ยวกับ arecord ที่นี่แล้ว
USB_SOUND_CARD = 'plughw:1,0'
PTT_RATE = "16000"
PTT_FORMAT = "S16_LE"
UDP_IP = "127.0.0.1"
UDP_PORT = 8001

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
    """
    Thread ที่ทำงานเบื้องหลังเพื่อรับข้อมูลเสียงจาก UDP
    """
    print(f"UDP listener waiting for audio stream on {UDP_IP}:{UDP_PORT}")
    
    # สร้าง UDP socket เพื่อรับข้อมูล
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_IP, UDP_PORT))
    
    is_receiving = False

    while not stop_thread.is_set():
        try:
            # รับข้อมูลเสียงจาก UDP
            audio_chunk, addr = sock.recvfrom(2048) # buffer size
            
            # เมื่อได้รับข้อมูลครั้งแรก ให้ถือว่ากำลัง receiving
            if not is_receiving:
                is_receiving = True
                socketio.emit('radio_status', {'status': 'receiving'})
            
            # ส่งข้อมูลเสียงต่อไปยังเบราว์เซอร์
            socketio.emit('audio_chunk_from_server', audio_chunk)

            # หมายเหตุ: เราอาจจะต้องเพิ่ม Logic การจับ VOX หรือ timeout
            # แต่เพื่อทดสอบ ให้ส่งทุกอย่างที่ได้รับไปก่อน
            
        except socket.timeout:
            # ถ้าไม่มีข้อมูลเข้ามาซักพัก ให้ถือว่าหยุด receiving
            if is_receiving:
                is_receiving = False
                socketio.emit('radio_status', {'status': 'idle'})
        except Exception as e:
            print(f"Error in UDP listener: {e}")
            break
    
    sock.close()
    print("UDP listener thread stopped.")

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

if __name__ == '__main__':
    try:
        start_radio_listen_thread()
        print("Starting Web Server on http://127.0.0.1:8000 for Nginx proxy...")
        socketio.run(app, host='127.0.0.1', port=8000)
    except KeyboardInterrupt:
        print("Server shutting down...")
    finally:
        stop_radio_listen_thread()
