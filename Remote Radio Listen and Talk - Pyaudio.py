import os
import subprocess
import threading
import select
from flask import Flask, render_template
from flask_socketio import SocketIO, emit
import pyaudio

# --- Configuration ---
# !!! สำคัญมาก: เปลี่ยนค่านี้ให้ตรงกับ USB Sound Card ของคุณที่ได้จากคำสั่ง aplay -l !!!
USB_SOUND_CARD = 'plughw:1,0'
# !!! สำคัญมาก: ปรับค่าความดังขั้นต่ำเพื่อเริ่มส่งเสียง (VOX Threshold) !!!
# คุณอาจจะต้องลองปรับค่านี้ให้เหมาะสมกับสัญญาณรบกวนของวิทยุคุณ
VOX_THRESHOLD = 500  # ลองเริ่มจากค่านี้น้อยๆ ก่อน เช่น 100 แล้วค่อยๆ เพิ่ม

# ตั้งค่า PyAudio
CHUNK = 1024  # ขนาดของข้อมูลเสียงที่จะอ่านในแต่ละครั้ง
FORMAT = pyaudio.paInt16 # 16-bit, ตรงกับ S16_LE
CHANNELS = 1
RATE = 24000  # ใช้ค่า Sweet Spot ของเรา
# !!! สำคัญ: ใส่เลข Index ที่หาได้จากขั้นตอนที่ 2 !!!
DEVICE_INDEX = 1

# ตั้งค่า Flask และ SocketIO
app = Flask(__name__)
app.config['SECRET_KEY'] = '2aa05de948623906118908dda27ec3f7255b6b097329fb20'  # เปลี่ยนเป็น key ของคุณ
socketio = SocketIO(app)

# ตัวแปรสำหรับจัดการ Process การส่งและรับเสียง
ptt_process = None
is_transmitting = False
ptt_lock = threading.Lock()

# --- ALSA Volume Control ---
def set_alsa_volume(control_name, value):
    """ใช้คำสั่ง amixer เพื่อปรับเสียง"""
    try:
        # แยก Card number จาก USB_SOUND_CARD
        card_num = USB_SOUND_CARD.split(':')[1].split(',')[0]
        command = f"amixer -c {card_num} sset {control_name} {value}"
        subprocess.run(command, shell=True, check=True)
        print(f"Set {control_name} to {value}")
    except subprocess.CalledProcessError as e:
        print(f"Error setting volume for {control_name}: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")


# --- Web Interface ---
@app.route('/')
def index():
    """แสดงหน้าเว็บหลัก"""
    return render_template('index.html')


# --- SocketIO Event Handlers ---
@socketio.on('connect')
def handle_connect():
    """เมื่อมี Client เชื่อมต่อเข้ามา"""
    print('Client connected')


@socketio.on('disconnect')
def handle_disconnect():
    """เมื่อ Client หลุดการเชื่อมต่อ"""
    print('Client disconnected')


@socketio.on('control_volume')
def handle_volume(data):
    """รับคำสั่งปรับเสียงจากหน้าเว็บ"""
    control = data.get('control')  # เช่น 'Mic' หรือ 'Speaker'
    value = data.get('value')  # เช่น '80%'
    if control and value:
        set_alsa_volume(control, value)


@socketio.on('ptt_start')
def handle_ptt_start():
    """เริ่มกระบวนการส่งเสียง (PTT) จาก Client"""
    global ptt_process, is_transmitting
    with ptt_lock:  # <-- ใช้ Lock
        if is_transmitting:
            return
        is_transmitting = True
        # หยุดการฟังเสียงจากวิทยุชั่วคราว
        stop_radio_listen_thread()
        print("PTT started by client. Starting aplay...")
        # เริ่ม aplay เพื่อรอรับข้อมูลเสียงจาก client แล้วเล่นออกลำโพง
        ptt_process = subprocess.Popen(
            ['aplay', '-D', USB_SOUND_CARD, '-f', 'S16_LE', '-r', '22050', '-c', '1', '-t', 'raw'],
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )


@socketio.on('audio_chunk_to_server')
def handle_audio_chunk(chunk):
    """รับชิ้นส่วนเสียงจาก Client แล้วส่งไปที่ aplay"""
    # ไม่จำเป็นต้องใช้ Lock ที่นี่ เพราะเราอยากให้มันส่งข้อมูลได้เร็ว
    if ptt_process and ptt_process.stdin:
        try:
            ptt_process.stdin.write(chunk)
        except (BrokenPipeError, IOError, ValueError):  # <-- ดักจับ ValueError ด้วย
            print("Pipe to aplay is broken or closed. Stopping PTT.")
            # เรียกใช้ stop_ptt() แค่ครั้งเดียวก็พอ
            if is_transmitting:
                stop_ptt()


@socketio.on('ptt_stop')
def handle_ptt_stop():
    """หยุดกระบวนการส่งเสียง (PTT)"""
    # is_transmitting ถูกเช็คใน stop_ptt() แล้ว
    stop_ptt()


def stop_ptt():
    """ฟังก์ชันสำหรับหยุด aplay process (ทำให้ปลอดภัยขึ้น)"""
    global ptt_process, is_transmitting
    with ptt_lock:  # <-- ใช้ Lock เพื่อป้องกันการทำงานซ้ำซ้อน
        if not is_transmitting:
            return  # ถ้าไม่ได้ส่งอยู่ ก็ไม่ต้องทำอะไร

        print("PTT stopped by client or error.")
        is_transmitting = False  # <-- ย้าย is_transmitting มาไว้ใน Lock

        if ptt_process:
            if ptt_process.stdin:
                try:
                    ptt_process.stdin.close()
                except (BrokenPipeError, IOError):
                    pass  # ไม่ต้องทำอะไรถ้าท่อปิดไปแล้ว
            if ptt_process.poll() is None:
                ptt_process.terminate()
                ptt_process.wait()
            ptt_process = None
            print("aplay process stopped.")

        # กลับมาเริ่มฟังเสียงจากวิทยุใหม่
        # การเรียก start_radio_listen_thread() อาจไม่จำเป็นต้องอยู่ใน lock
        # แต่เพื่อความง่าย เอาไว้ตรงนี้ก่อน
        start_radio_listen_thread()


# --- Radio Listening Thread ---
radio_listen_thread = None
stop_thread = threading.Event()


def radio_listen():
    """
    Thread ที่ทำงานเบื้องหลังเพื่ออ่านเสียงด้วย PyAudio
    """
    global is_transmitting

    p = None
    stream = None

    try:
        p = pyaudio.PyAudio()
        stream = p.open(format=FORMAT,
                        channels=CHANNELS,
                        rate=RATE,
                        input=True,
                        frames_per_buffer=CHUNK,
                        input_device_index=DEVICE_INDEX)

        print("PyAudio radio listening thread started.")
        is_receiving = False

        while not stop_thread.is_set():
            if is_transmitting:
                socketio.sleep(0.1)
                continue

            audio_chunk = stream.read(CHUNK, exception_on_overflow=False)

            # --- ส่วน VOX และการส่งข้อมูลเหมือนเดิมทุกประการ ---
            try:
                samples = [int.from_bytes(audio_chunk[i:i + 2], 'little', signed=True) for i in
                           range(0, len(audio_chunk), 2)]
                rms = (sum(s ** 2 for s in samples) / len(samples)) ** 0.5
            except (ValueError, ZeroDivisionError):
                rms = 0

            if rms > VOX_THRESHOLD:
                if not is_receiving:
                    is_receiving = True
                    socketio.emit('radio_status', {'status': 'receiving'})
                    print(f"VOX triggered! RMS: {rms:.2f}")

                socketio.emit('audio_chunk_from_server', audio_chunk)

            elif is_receiving:
                is_receiving = False
                socketio.emit('radio_status', {'status': 'idle'})
                print("Radio signal ended.")
            # --- จบส่วน VOX ---

    except Exception as e:
        print(f"An error occurred in PyAudio thread: {e}")
    finally:
        # --- Cleanup: ปิด Stream และ Terminate PyAudio ให้เรียบร้อย ---
        if stream is not None:
            stream.stop_stream()
            stream.close()
            print("PyAudio stream closed.")
        if p is not None:
            p.terminate()
            print("PyAudio terminated.")
        print("Radio listening thread stopped.")


def start_radio_listen_thread():
    """ฟังก์ชันสำหรับเริ่ม Thread ฟังเสียง"""
    global radio_listen_thread, stop_thread
    if radio_listen_thread is None or not radio_listen_thread.is_alive():
        stop_thread.clear()
        radio_listen_thread = socketio.start_background_task(target=radio_listen)
        print("Starting radio listener...")


def stop_radio_listen_thread():
    """ฟังก์ชันสำหรับหยุด Thread ฟังเสียง"""
    global stop_thread
    stop_thread.set()
    print("Stopping radio listener...")


if __name__ == '__main__':
    try:
        set_alsa_volume("'Mic'", "80%")  # ตั้งค่าเสียงเริ่มต้น
        set_alsa_volume("'Speaker'", "85%")  # ตั้งค่าเสียงเริ่มต้น
        start_radio_listen_thread()
        print("Starting Flask-SocketIO server...")
        # ใช้ host='0.0.0.0' เพื่อให้เข้าถึงได้จากทุก IP ในเครือข่ายเดียวกัน
        socketio.run(app, host='0.0.0.0', port=5000, debug=False)
    finally:
        print("Shutting down...")
        stop_radio_listen_thread()
        if ptt_process:
            stop_ptt()
