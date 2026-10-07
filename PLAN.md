# PLAN: BMO thin-client refactor

This fork turns upstream [brenpoly/be-more-agent](https://github.com/brenpoly/be-more-agent)
(a **local / offline** AI agent: Ollama + whisper.cpp + Piper + Moondream on the Pi) into a
**Raspberry Pi thin client** for a home AI server (`http://192.168.0.240:8765`).
All inference (LLM, vision, Whisper STT, Piper TTS) runs on the server. The Pi only does
microphone, camera, speaker, buttons and the face display.

Reference implementation: `tools/bmo_test.py`, a byte-identical copy of the acceptance test
that already proved the full hardware + server path on this Pi. Production code reproduces its
behaviour and does not invent replacements.

## Target hardware (verified)
| Part | Detail | Known-good device |
|---|---|---|
| Pi 4, Pi OS Lite 64-bit | hostname `bmo-pi`, no X server | – |
| Camera v2 (IMX219) | `rpicam-still --width 640 --height 480 -n` | – |
| USB mic (C-Media "USB PnP Sound Device") | 48 kHz mono S16_LE, `Mic` 100 %, AGC on, +18 dB software | `plughw:1,0` / `plughw:CARD=Device,DEV=0` |
| USB speaker "UACDemoV1.0" (Jieli) | via ALSA plug layer only | `plughw:2,0` / `plughw:CARD=UACDemoV10,DEV=0` |
| Freenove 5" DSI 800x480 | face GUI target | – |
| Feather 32u4 as USB HID keyboard | Up/Down/Left/Right/A/B/Start | not yet wired |

## What stays
- `LICENSE` (MIT, brenpoly) and upstream attribution.
- `faces/` and `sounds/` assets; the BotStates concept (WARMUP, IDLE, LISTENING, THINKING,
  SPEAKING, ERROR, CAPTURING); fullscreen 800x480 Tk face loop; push-to-talk toggle and
  interrupt behaviour; greeting/ack/thinking sounds.
- `wakeword.onnx` and OpenWakeWord as an **optional** extra (disabled by default).
- `upstream` remote, so upstream asset changes can still be pulled.

## What is removed
- Ollama (`ollama` package, warm-up/keep-alive, local system prompt and JSON tool router).
- whisper.cpp subprocess transcription.
- Piper binary, Piper voices, custom BMO `.onnx` voice download.
- Moondream local vision.
- DuckDuckGo web search (`duckduckgo-search`).
- Local `memory.json` chat history. The server owns persona and memory, and the Pi keeps only ephemeral session state.
- `sounddevice` / PortAudio / `numpy` / `scipy`. Audio uses the proven `arecord`/`ffmpeg`/`aplay` path.
- Upstream `config.json` (Ollama models / prompt) → replaced by `config.example.json`.

## What is rewritten
`agent.py` (one 1081-line file) is split into a small package:
```
app/
  main.py, __main__.py   CLI: --headless | --gui | --text | --self-test
  config.py              typed config, defaults, token-file resolution
  controller.py          interaction state machine + interruption/cancellation
  server/                client.py (all /v1 endpoints), reservation.py, errors.py
  hardware/              alsa.py (name-based card discovery), microphone.py, speaker.py,
                         camera.py, input.py (keyboard/HID/evdev → actions)
  audio/processing.py    +18 dB, 48k→16k mono via ffmpeg; WAV/base64 helpers
  ui/                    states.py, gui.py (Tk faces), headless.py (stdin/log)
tools/bmo_test.py        preserved acceptance test
tools/bmo-agent.service  systemd template (not enabled)
```
`setup.sh`, `requirements.txt`, `start_agent.sh` and `README.md` are rewritten for the thin client.

## Behaviour
IDLE → (Start) LISTENING: `arecord` 48 kHz → (Start) stop → +18 dB, 16 kHz mono →
CAPTURING (vision_mode always/manual) → THINKING: reserve/confirm ready →
`POST /v1/bmo/interact` (fields `audio`, `image`, `speak=true`, unique `X-Request-ID`) →
SPEAKING: decode `audio_wav_base64` → `aplay` on the USB speaker → IDLE.

Interrupt (Start during THINKING/SPEAKING): stop playback right away → invalidate the in-flight
interaction so its response is discarded and never replayed → `POST /v1/requests/{id}/cancel`
→ before the next inference, wait for `/v1/status` bmo_ready and re-confirm the reservation.

Reservation: 200 = ready; 202 = poll `/v1/status` until `bmo_ready`, then POST again and require
200. It is renewed on use (lifetime 300 s) and released with DELETE on exit/SIGTERM.

## Migration steps (commits on `thin-client-refactor`)
1. chore: fork baseline and document thin-client architecture
2. refactor: add config and BMO server client
3. refactor: add verified Pi audio and camera hardware adapters
4. refactor: replace local inference with server interaction pipeline
5. refactor: adapt GUI/state machine to thin-client pipeline
6. feat: add interruption and cancellation
7. chore: replace setup and dependencies
8. test: preserve/add physical acceptance tooling
9. docs: rewrite README for this BMO hardware

Tests (`pytest`) run after each phase. Physical validation (camera, mic, +18 dB, speaker, health,
auth, reservation, chat, TTS, Whisper, vision, combined, cancellation, release) happens on the Pi
before anything is called done. Autostart (systemd) is **not** enabled until acceptance passes.

## Risks
- **ALSA card numbers change between boots.** Resolve by card name from `/proc/asound/cards`,
  and fall back to the verified `plughw:1,0` / `plughw:2,0`.
- **The USB speaker has no mixing.** All playback goes through one serialized Speaker, and reply audio
  pre-empts sound effects.
- **A cancel acknowledgement does not mean the GPU is clean.** Re-check readiness before the next request.
- **arecord can be stopped mid-write.** Stop it with SIGINT so the WAV header is finalised, and treat
  very short clips as "heard nothing".
- **There is no display server yet.** The GUI is optional and loaded lazily. The headless mode covers the
  full pipeline, and a kiosk/X setup comes later.
- **The token could leak.** It is only ever read into a header, never logged, printed, put on a command line
  or committed (`.gitignore`).
