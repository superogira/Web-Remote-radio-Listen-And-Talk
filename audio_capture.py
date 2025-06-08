import subprocess
import socket

# --- การตั้งค่า ---
UDP_IP = "127.0.0.1"  # ส่งข้อมูลภายในเครื่องเท่านั้น
UDP_PORT = 8001        # Port สำหรับส่งข้อมูลเสียง
USB_SOUND_CARD = 'plughw:1,0'
CAPTURE_RATE = "16000"
CAPTURE_FORMAT = "S16_LE"
CHUNK_SIZE = 1024

print("--- Audio Capture Process Starting ---")
print(f"Sending audio stream to UDP {UDP_IP}:{UDP_PORT}")

# สร้าง UDP socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# สร้าง command สำหรับ arecord
command = ['arecord', '-D', USB_SOUND_CARD, '-f', CAPTURE_FORMAT, '-r', CAPTURE_RATE, '-c', '1', '-t', 'raw']

try:
    # เริ่มโปรเซส arecord
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    print(f"arecord process started with PID {process.pid}")

    while True:
        # อ่านข้อมูลเสียงจาก arecord
        audio_chunk = process.stdout.read(CHUNK_SIZE)
        if not audio_chunk:
            print("arecord stream ended. Exiting.")
            break
        
        # ส่งข้อมูลเสียงผ่าน UDP
        sock.sendto(audio_chunk, (UDP_IP, UDP_PORT))

except KeyboardInterrupt:
    print("Capture process stopped by user.")
finally:
    if 'process' in locals() and process.poll() is None:
        process.terminate()
        process.wait()
    print("--- Audio Capture Process Ended ---")
