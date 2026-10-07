import math
import struct
import wave


def make_wav(path, rate=48000, secs=0.5, amp=1000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(amp * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(int(rate * secs))))


def peak(path):
    with wave.open(str(path), "rb") as w:
        d = w.readframes(w.getnframes())
    return max(abs(x) for x in struct.unpack(f"<{len(d)//2}h", d))
