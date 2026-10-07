#!/usr/bin/env python3

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests


# ============================================================
# CONFIG
# ============================================================

BMO_URL = os.environ.get(
    "BMO_URL",
    "http://192.168.0.240:8765",
)

MIC_DEVICE = os.environ.get(
    "BMO_MIC_DEVICE",
    "plughw:1,0",
)

SPEAKER_DEVICE = os.environ.get(
    "BMO_SPEAKER_DEVICE",
    "plughw:2,0",
)

MIC_GAIN_DB = float(
    os.environ.get("BMO_MIC_GAIN_DB", "18")
)

TEST_DIR = Path.home() / "bmo-full-test"

RAW_MIC = TEST_DIR / "bmo-mic-raw.wav"
BOOSTED_MIC = TEST_DIR / "bmo-mic-boosted.wav"
CAMERA_FILE = TEST_DIR / "bmo-camera.jpg"
TTS_FILE = TEST_DIR / "bmo-speaker.wav"
REPLY_FILE = TEST_DIR / "bmo-reply.wav"

REQUEST_TIMEOUT = 900


# Try likely credential locations.
TOKEN_CANDIDATES = [
    os.environ.get("BMO_TOKEN_FILE"),
    "/etc/bmo/token",
    str(Path.home() / ".config/bmo/token"),
]


# ============================================================
# TERMINAL COLORS
# ============================================================

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def passed(msg):
    print(f"{GREEN}[PASS]{RESET} {msg}")


def failed(msg):
    print(f"{RED}[FAIL]{RESET} {msg}")


def warning(msg):
    print(f"{YELLOW}[WARN]{RESET} {msg}")


def info(msg):
    print(f"{CYAN}[INFO]{RESET} {msg}")


def section(name):
    print()
    print("=" * 68)
    print(name)
    print("=" * 68)


# ============================================================
# COMMAND HELPERS
# ============================================================

def run(cmd, capture=False, timeout=None):
    try:
        result = subprocess.run(
            cmd,
            text=True,
            capture_output=capture,
            timeout=timeout,
        )

        if capture:
            return result.returncode, result.stdout, result.stderr

        return result.returncode, "", ""

    except FileNotFoundError:
        return 127, "", f"Command not found: {cmd[0]}"

    except subprocess.TimeoutExpired:
        return 124, "", "Command timed out"


def exists(cmd):
    return shutil.which(cmd) is not None


# ============================================================
# TOKEN
# ============================================================

def find_token_file():
    for candidate in TOKEN_CANDIDATES:
        if not candidate:
            continue

        path = Path(candidate).expanduser()

        try:
            if path.is_file() and os.access(path, os.R_OK):
                return path
        except OSError:
            pass

    return None


def load_token():
    path = find_token_file()

    if not path:
        failed("No readable physical BMO credential found.")
        print()
        print("Checked:")
        print("  /etc/bmo/token")
        print("  ~/.config/bmo/token")
        print()
        print("Or set:")
        print("  export BMO_TOKEN_FILE=/path/to/token")
        return None

    token = path.read_text().strip()

    if not token:
        failed(f"Token file is empty: {path}")
        return None

    passed(f"Loaded BMO credential from {path}")
    print("Credential contents were NOT printed.")

    return token


def auth(token):
    return {
        "Authorization": f"Bearer {token}"
    }


# ============================================================
# HARDWARE
# ============================================================

def system_test():
    section("1. SYSTEM")

    print("Hostname:", subprocess.getoutput("hostname"))
    print("IP:", subprocess.getoutput("hostname -I"))
    print()

    run(["uname", "-a"])


def usb_test():
    section("2. USB DEVICES")

    run(["lsusb"])


def audio_inventory():
    section("3. AUDIO DEVICES")

    print("Playback:")
    run(["aplay", "-l"])

    print()
    print("Capture:")
    run(["arecord", "-l"])


def configure_microphone():
    section("4. MICROPHONE CONFIGURATION")

    # We already identified the microphone as ALSA card 1.
    run([
        "amixer",
        "-c", "1",
        "sset",
        "Mic",
        "100%",
    ])

    run([
        "amixer",
        "-c", "1",
        "sset",
        "Auto Gain Control",
        "on",
    ])

    print()
    run(["amixer", "-c", "1"])

    passed("Mic set to 100% with AGC enabled")


def record_microphone():
    section("5. MICROPHONE RECORDING")

    RAW_MIC.unlink(missing_ok=True)
    BOOSTED_MIC.unlink(missing_ok=True)

    print("Speak clearly for five seconds.")
    print()

    rc, _, _ = run([
        "arecord",
        "-D", MIC_DEVICE,
        "-f", "S16_LE",
        "-r", "48000",
        "-c", "1",
        "-d", "5",
        str(RAW_MIC),
    ])

    if rc != 0 or not RAW_MIC.exists():
        failed("Microphone recording failed")
        return False

    passed("Raw microphone recording created")

    # Convert to Whisper-friendly 16k mono and apply +18 dB gain.
    rc, _, _ = run([
        "ffmpeg",
        "-y",
        "-loglevel", "error",
        "-i", str(RAW_MIC),
        "-af", f"volume={MIC_GAIN_DB}dB",
        "-ar", "16000",
        "-ac", "1",
        str(BOOSTED_MIC),
    ])

    if rc != 0 or not BOOSTED_MIC.exists():
        failed("18 dB microphone boost failed")
        return False

    passed(
        f"Created boosted microphone recording "
        f"(+{MIC_GAIN_DB:g} dB)"
    )

    print()
    print("Raw:", RAW_MIC)
    print("Boosted:", BOOSTED_MIC)

    return True


def playback(path, label):
    if not path.exists():
        failed(f"{label}: file does not exist")
        return False

    rc, _, _ = run([
        "aplay",
        "-D", SPEAKER_DEVICE,
        str(path),
    ])

    if rc == 0:
        passed(label)
        return True

    failed(label)
    return False


def local_audio_test():
    section("6. LOCAL AUDIO LOOPBACK")

    print(
        f"Playing the +{MIC_GAIN_DB:g} dB microphone recording "
        "through the USB speaker."
    )
    print()

    return playback(
        BOOSTED_MIC,
        "Microphone -> USB speaker",
    )


def camera_test():
    section("7. CAMERA")

    CAMERA_FILE.unlink(missing_ok=True)

    if exists("rpicam-hello"):
        run(["rpicam-hello", "--list-cameras"])

    if exists("rpicam-still"):
        command = "rpicam-still"
    elif exists("libcamera-still"):
        command = "libcamera-still"
    else:
        failed("No Raspberry Pi camera capture utility found")
        return False

    print()
    info("Capturing 640x480 JPEG")

    rc, _, _ = run([
        command,
        "--width", "640",
        "--height", "480",
        "-n",
        "-o", str(CAMERA_FILE),
    ], timeout=30)

    if (
        rc == 0
        and CAMERA_FILE.exists()
        and CAMERA_FILE.stat().st_size > 1000
    ):
        passed(f"Camera capture created: {CAMERA_FILE}")
        return True

    failed("Camera capture failed")
    return False


# ============================================================
# SERVER
# ============================================================

def health_test():
    section("8. BMO SERVER HEALTH")

    try:
        r = requests.get(
            f"{BMO_URL}/health",
            timeout=10,
        )

        r.raise_for_status()

        print(r.text)
        passed("BMO server reachable over LAN")
        return True

    except Exception as e:
        failed(f"Health request failed: {e}")
        return False


def discovery_test(token):
    section("9. AUTHENTICATION / DISCOVERY")

    try:
        r = requests.get(
            f"{BMO_URL}/v1/models",
            headers=auth(token),
            timeout=30,
        )
        r.raise_for_status()

        result = r.json()
        print(json.dumps(result, indent=2))

        passed("/v1/models authenticated")

    except Exception as e:
        failed(f"/v1/models failed: {e}")
        return False

    print()

    try:
        r = requests.get(
            f"{BMO_URL}/v1/status",
            headers=auth(token),
            timeout=30,
        )
        r.raise_for_status()

        print(json.dumps(r.json(), indent=2))

        passed("/v1/status authenticated")

    except Exception as e:
        failed(f"/v1/status failed: {e}")
        return False

    return True


def reserve(token):
    section("10. BMO RESERVATION")

    try:
        r = requests.post(
            f"{BMO_URL}/v1/bmo/reservation",
            headers=auth(token),
            timeout=REQUEST_TIMEOUT,
        )

    except Exception as e:
        failed(f"Reservation request failed: {e}")
        return False

    print("Reservation HTTP:", r.status_code)

    try:
        print(json.dumps(r.json(), indent=2))
    except Exception:
        print(r.text)

    if r.status_code == 200:
        passed("BMO already ready")
        return True

    if r.status_code != 202:
        failed(f"Unexpected reservation response: {r.status_code}")
        return False

    info("BMO workers are restoring")

    for attempt in range(1, 101):

        try:
            status = requests.get(
                f"{BMO_URL}/v1/status",
                headers=auth(token),
                timeout=30,
            )

            status.raise_for_status()

            data = status.json()
            ready = data.get("bmo_ready") is True

            print(
                f"\rReadiness check {attempt}/100 "
                f"bmo_ready={ready}",
                end="",
                flush=True,
            )

            if ready:
                print()

                confirm = requests.post(
                    f"{BMO_URL}/v1/bmo/reservation",
                    headers=auth(token),
                    timeout=REQUEST_TIMEOUT,
                )

                if confirm.status_code == 200:
                    passed("Reservation active and BMO ready")
                    return True

                failed(
                    "BMO reported ready, but reservation "
                    f"returned HTTP {confirm.status_code}"
                )
                return False

        except Exception as e:
            warning(f"\nReadiness poll error: {e}")

        time.sleep(3)

    print()
    failed("Timed out waiting for BMO")
    return False


# ============================================================
# TEXT
# ============================================================

def text_chat_test(token):
    section("11. TEXT CHAT")

    payload = {
        "model": "bmo-qwen3-vl-8b",
        "messages": [
            {
                "role": "user",
                "content": "Hello BMO. Identify yourself in one short sentence."
            }
        ],
        "max_tokens": 80,
    }

    try:
        r = requests.post(
            f"{BMO_URL}/v1/chat/completions",
            headers={
                **auth(token),
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )

        r.raise_for_status()

        result = r.json()

        print(json.dumps(result, indent=2))

        passed("Text BMO interaction succeeded")
        return True

    except Exception as e:
        failed(f"Text chat failed: {e}")
        return False


# ============================================================
# TTS
# ============================================================

def tts_test(token):
    section("12. SERVER TTS -> PHYSICAL SPEAKER")

    TTS_FILE.unlink(missing_ok=True)

    try:
        r = requests.post(
            f"{BMO_URL}/v1/audio/speech",
            headers={
                **auth(token),
                "Content-Type": "application/json",
            },
            json={
                "input":
                    "Hello! I am BMO. "
                    "My server connection and speaker are working."
            },
            timeout=REQUEST_TIMEOUT,
        )

        r.raise_for_status()

        TTS_FILE.write_bytes(r.content)

        passed("Piper returned WAV")

    except Exception as e:
        failed(f"TTS request failed: {e}")
        return False

    print()
    return playback(
        TTS_FILE,
        "Server Piper -> physical BMO speaker",
    )


# ============================================================
# WHISPER
# ============================================================

def transcription_test(token):
    section("13. PHYSICAL MICROPHONE -> WHISPER")

    if not BOOSTED_MIC.exists():
        failed("Boosted microphone file missing")
        return False

    try:
        with BOOSTED_MIC.open("rb") as audio:
            r = requests.post(
                f"{BMO_URL}/v1/audio/transcriptions",
                headers=auth(token),
                files={
                    "file": (
                        "bmo-mic.wav",
                        audio,
                        "audio/wav",
                    )
                },
                timeout=REQUEST_TIMEOUT,
            )

        r.raise_for_status()

        result = r.json()

        print()
        print("Whisper transcript:")
        print()
        print(result.get("text"))
        print()

        (
            TEST_DIR / "transcription.json"
        ).write_text(json.dumps(result, indent=2))

        passed("Physical microphone -> Whisper")
        return True

    except Exception as e:
        failed(f"Transcription failed: {e}")
        return False


# ============================================================
# VISION
# ============================================================

def vision_test(token):
    section("14. PHYSICAL CAMERA -> QWEN VISION")

    if not CAMERA_FILE.exists():
        failed("Camera image missing")
        return False

    try:
        with CAMERA_FILE.open("rb") as image:

            r = requests.post(
                f"{BMO_URL}/v1/bmo/interact",
                headers=auth(token),
                data={
                    "text":
                        "Briefly describe what the BMO camera can see.",
                    "speak": "false",
                },
                files={
                    "image": (
                        "bmo-camera.jpg",
                        image,
                        "image/jpeg",
                    )
                },
                timeout=REQUEST_TIMEOUT,
            )

        r.raise_for_status()

        result = r.json()

        print()
        print("Vision response:")
        print()
        print(result.get("text"))
        print()

        (
            TEST_DIR / "vision.json"
        ).write_text(json.dumps(result, indent=2))

        passed("Camera -> BMO vision")
        return True

    except Exception as e:
        failed(f"Vision request failed: {e}")
        return False


# ============================================================
# FULL BMO
# ============================================================

def full_interaction(token):
    section("15. FULL PHYSICAL BMO INTERACTION")

    if not BOOSTED_MIC.exists():
        failed("Boosted microphone file missing")
        return False

    if not CAMERA_FILE.exists():
        failed("Camera image missing")
        return False

    REPLY_FILE.unlink(missing_ok=True)

    try:
        with (
            BOOSTED_MIC.open("rb") as audio,
            CAMERA_FILE.open("rb") as image,
        ):

            r = requests.post(
                f"{BMO_URL}/v1/bmo/interact",
                headers=auth(token),
                data={
                    "speak": "true",
                },
                files={
                    "audio": (
                        "bmo-mic.wav",
                        audio,
                        "audio/wav",
                    ),
                    "image": (
                        "bmo-camera.jpg",
                        image,
                        "image/jpeg",
                    ),
                },
                timeout=REQUEST_TIMEOUT,
            )

        r.raise_for_status()

        result = r.json()

        (
            TEST_DIR / "bmo-interaction.json"
        ).write_text(json.dumps(result, indent=2))

        print()
        print("Transcript:")
        print(result.get("transcript"))
        print()

        print("BMO response:")
        print(result.get("text"))
        print()

        audio_b64 = result.get("audio_wav_base64")

        if not audio_b64:
            failed("BMO returned no response audio")
            return False

        REPLY_FILE.write_bytes(
            base64.b64decode(
                audio_b64,
                validate=True,
            )
        )

        passed("Full multimodal server interaction succeeded")

    except Exception as e:
        failed(f"Full interaction failed: {e}")
        return False

    print()

    return playback(
        REPLY_FILE,
        "BMO response -> physical speaker",
    )


# ============================================================
# RELEASE
# ============================================================

def release(token):
    section("16. RELEASE RESERVATION")

    try:
        r = requests.delete(
            f"{BMO_URL}/v1/bmo/reservation",
            headers=auth(token),
            timeout=30,
        )

        r.raise_for_status()

        passed("BMO reservation released")
        return True

    except Exception as e:
        failed(f"Reservation release failed: {e}")
        return False


# ============================================================
# SUMMARY
# ============================================================

def summary(results):
    section("FINAL RESULTS")

    for test, value in results.items():

        label = test.replace("_", " ").title()

        if value:
            passed(label)
        else:
            failed(label)

    print()
    print("Artifacts:")
    print(f"  {TEST_DIR}")
    print()

    if TEST_DIR.exists():
        for p in sorted(TEST_DIR.iterdir()):
            print(f"  {p.name}")

    print()
    print("Manually confirm:")
    print("  • boosted mic recording was clearly audible")
    print("  • Whisper understood your speech")
    print("  • camera description matched reality")
    print("  • Piper played from the physical speaker")
    print("  • final BMO reply was audible")
    print()


# ============================================================
# MAIN
# ============================================================

def main():

    TEST_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    print()
    print("############################################################")
    print("#                                                          #")
    print("#             BMO FULL PHYSICAL ACCEPTANCE TEST            #")
    print("#                                                          #")
    print("############################################################")
    print()
    print("Server:       ", BMO_URL)
    print("Microphone:   ", MIC_DEVICE)
    print("Speaker:      ", SPEAKER_DEVICE)
    print("Mic boost:    ", f"+{MIC_GAIN_DB:g} dB")
    print("Artifacts:    ", TEST_DIR)

    results = {}

    system_test()
    usb_test()
    audio_inventory()

    configure_microphone()

    results["microphone"] = record_microphone()

    if results["microphone"]:
        results["local_audio"] = local_audio_test()
    else:
        results["local_audio"] = False

    results["camera"] = camera_test()

    results["server_health"] = health_test()

    if not results["server_health"]:
        summary(results)
        return 1

    token = load_token()

    if not token:
        summary(results)
        return 1

    results["authentication"] = discovery_test(token)

    if not results["authentication"]:
        summary(results)
        return 1

    reserved = False

    try:

        results["reservation"] = reserve(token)
        reserved = results["reservation"]

        if not reserved:
            summary(results)
            return 1

        results["text_chat"] = text_chat_test(token)

        results["tts_speaker"] = tts_test(token)

        results["whisper"] = transcription_test(token)

        results["vision"] = vision_test(token)

        results["full_bmo"] = full_interaction(token)

    finally:

        if reserved:
            results["release"] = release(token)

    summary(results)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())

    except KeyboardInterrupt:
        print()
        warning("Interrupted")
        sys.exit(130)
