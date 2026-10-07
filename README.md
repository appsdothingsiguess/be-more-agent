# BMO Thin Client

A Raspberry Pi front end for a BMO robot. The Pi handles the microphone, camera, speaker, buttons and face display. A home AI server does all the thinking.

This is a fork of [brenpoly/be-more-agent](https://github.com/brenpoly/be-more-agent). Upstream is a fully local, offline agent that runs Ollama, whisper.cpp, Piper and Moondream on the Pi itself. This fork turns it into a thin client for a home AI server. **The Pi holds no models.** LLM, vision, speech-to-text and text-to-speech all run on the server. Many thanks to the upstream author, brenpoly, for the original project, the faces and the sounds.

## Architecture

```text
Raspberry Pi 4 (bmo-pi)                          Home AI server
+-----------------------------+                  +-----------------------------+
| USB mic  -> arecord 48 kHz  |                  | /v1/status, /v1/models      |
| Camera   -> rpicam-still    |   HTTP + token   | /v1/bmo/reservation         |
| Buttons  -> HID keyboard    | ---------------> | /v1/bmo/interact            |
| Face display (Tk, 800x480)  |                  |   LLM + vision + Whisper    |
| USB speaker <- aplay        | <--------------- |   + TTS (WAV in the reply)  |
+-----------------------------+                  | /v1/requests/{id}/cancel    |
                                                 +-----------------------------+
```

Removed compared to upstream: Ollama, whisper.cpp, Piper and its voices, Moondream, DuckDuckGo search, local chat memory, and `sounddevice`/`numpy`/`scipy`. The monolithic `agent.py` is now the `app/` package. Faces, sounds, the state machine concept and OpenWakeWord (optional, off by default) are kept. See `PLAN.md` for the full refactor plan.

## Hardware

* Raspberry Pi 4, Raspberry Pi OS Lite 64-bit (a bare X server is added by `tools/setup_display.sh`)
* Camera Module v2 (IMX219), captured with `rpicam-still`
* USB microphone: C-Media "USB PnP Sound Device"
* USB speaker: "UACDemoV1.0"
* Freenove 5" 800x480 DSI display
* Adafruit Feather 32u4 acting as a USB HID keyboard for the 7 buttons (Up, Down, Left, Right, A, B, Start). Planned, not wired yet. A normal USB keyboard works in the meantime.

## Setup

```bash
git clone https://github.com/appsdothingsiguess/be-more-agent.git
cd be-more-agent
git checkout thin-client-refactor
./setup.sh
```

`setup.sh` installs a few apt packages (`python3-venv python3-tk ffmpeg alsa-utils rpicam-apps`), creates a venv, installs `requirements.txt`, creates `runtime/`, copies `config.example.json` to `config.json` if it is missing, and checks for the credential file. It downloads no models.

| Flag | Effect |
|---|---|
| `--venv PATH` | Virtualenv location (default `./venv`) |
| `--skip-apt` | Do not install system packages |
| `--dev` | Also install `requirements-dev.txt` (pytest) |
| `--with-wakeword` | Also install `requirements-wakeword.txt` |
| `--install-service` | Install `/etc/systemd/system/bmo-agent.service` (not enabled or started) |
| `--dry-run` | Print each command instead of running it |

### Credential

The physical BMO needs its own server credential, stored in a file that is read at runtime. It is searched in this order:

1. `$BMO_TOKEN_FILE`
2. `token_file` in `config.json` (default `/etc/bmo/token`)
3. `/etc/bmo/token`
4. `~/.config/bmo/token`

Never commit, print or paste this file. The client only puts it in a request header and never logs it. Keep the file outside the repo.

### Configuration

Edit `config.json` (copied from `config.example.json`). Any subset of keys overrides the defaults.

| Key | Default | Meaning |
|---|---|---|
| `server_url` | `http://192.168.0.240:8765` | Home server |
| `token_file` | `/etc/bmo/token` | Credential file |
| `model` | `bmo-qwen3-vl-8b` | Server model name |
| `microphone.device` / `.match` / `.fallback_device` | `auto` / `USB PnP Sound Device` / `plughw:1,0` | Mic selection |
| `microphone.gain_db` | `18` | Software gain applied before upload |
| `speaker.device` / `.match` / `.fallback_device` | `auto` / `UACDemoV1.0` / `plughw:2,0` | Speaker selection |
| `camera.enabled`, `.rotation` | `true`, `0` | Camera on/off, rotation (0/90/180/270) |
| `camera.vision_mode` | `manual` | `off`, `always` or `manual` |
| `speaker.volume` | `50%` | Speaker volume set at startup |
| `ui.enabled`, `.fullscreen` | `true`, `true` | Face GUI |
| `ui.text_only` | `false` | Mute: replies and errors are text only, nothing is played |
| `web.enabled`, `.port`, `.pin_file` | `true`, `8080`, `~/.config/bmo/web_pin` | Local web page |
| `sounds.*` | all `true` | Greeting, ack and thinking sounds |
| `input.evdev_enabled`, `.evdev_device` | `true`, `auto` | Read HID keyboards from `/dev/input` |
| `wake_word.enabled` | `false` | Optional OpenWakeWord |
| `memory.enabled`, `.max_messages`, `.max_chars` | `true`, `10`, `6000` | Conversation memory: recent exchanges (saved to `runtime/memory.json`) are sent with each turn, and the server's long-term recall is used. Saying or typing "forget everything" wipes both |

Settings changed from the web page (volume, mute, camera mode, sound effects, mic boost, memory) are saved to `runtime/settings.json` and override `config.json` at the next start.

Environment overrides: `BMO_SERVER_URL` (or `BMO_URL`), `BMO_MIC_DEVICE`, `BMO_SPEAKER_DEVICE`, `BMO_MIC_GAIN_DB`.

## Running

```bash
./venv/bin/python -m app --self-test      # physical acceptance diagnostics
./venv/bin/python -m app --headless       # console + HID buttons
./venv/bin/python -m app --text "Hello"   # one text message, speak the reply, exit
./venv/bin/python -m app --gui            # Tk face GUI (needs X/Wayland)
```

Other flags: `--config FILE`, `--no-speak` (with `--text`: print only), `-v`. `./start_agent.sh [args]` runs `python -m app` from the venv (set `BMO_VENV` to use another one). On Pi OS Lite, `tools/setup_display.sh` installs a bare X server (no desktop) and switches the service to `--gui` on the panel; `tools/setup_display.sh --remove` goes back to headless. With no mode flag, the GUI is used if a display is available and headless otherwise.

Headless console commands:

| Input | Action |
|---|---|
| Enter | Talk / stop talking / interrupt (depending on state) |
| `i` | Interrupt |
| `a`, `b` | Press button A or B |
| `t <text>` | Send a text message |
| `q` | Quit |

### systemd service

```bash
./setup.sh --skip-apt --install-service
```

This installs the unit but does not enable it. Enable it only after the acceptance tests pass:

```bash
sudo systemctl enable --now bmo-agent
```

The service runs `python -m app --headless` as your user, with the `audio`, `video` and `input` groups, and releases its server reservation on SIGTERM.

## Buttons

Default key mapping (override with `input.keymap` in `config.json`; actions are `up`, `down`, `left`, `right`, `a`, `b`, `start`, `quit`):

| Keys | Action |
|---|---|
| Enter, Space | Start: talk, stop talking, or interrupt when busy |
| Up / Down / Left / Right | up / down / left / right |
| A, Z | a |
| B, X | b |

Esc does not quit (an accidental press used to leave the service running but deaf). Quit with `q` in the console or stop the service.

`camera.vision_mode`:

* `off`: never send a picture.
* `always`: capture and send a photo with every spoken turn.
* `manual` (default): press B to arm the camera for the next turn only. Press B again to disarm.

## Errors and mute

Every failure has a short message and a next step, for example "I can't reach my brain server. Check that the computer is on and on the network, then press Start to try again." BMO shows it on the console, the screen and the web page, and plays a pre-recorded clip of it from `sounds/errors/` (so it still works when the server is down). With `ui.text_only` (mute) on, nothing is played. Server-side refusals (a large model running, GPU switching, BMO restoring) use the server's own message. While the server is getting BMO ready, BMO says once that it is waiting.

The clips are generated with the server voice: `./venv/bin/python tools/make_error_clips.py [--force]` after editing `app/notify.py`.

## Web page

While BMO runs (`--headless` or `--gui`), it serves a page on port 8080: `http://<pi-address>:8080` (this Pi: `http://192.168.0.218:8080`) from any device on the home network. It offers:

* a text chat with BMO, with a "Speak reply" toggle that follows the mute setting
* live state, errors and "waiting for the server" notices
* settings: volume, mute (text only), camera mode, sound effects, mic boost
* a server panel: reachable, ready, mode, queue, last error

**PIN.** The first start creates a 6-digit PIN in `~/.config/bmo/web_pin` (mode 600). Read it with `cat ~/.config/bmo/web_pin`; delete the file to get a new one. After 5 wrong PINs, that device is locked out for 5 minutes.

**What it exposes.** The page talks only to the Pi; the server credential never reaches the browser. There is no HTTPS, so keep it on your home network. Turn it off with `"web": {"enabled": false}`.

## Device detection

Mic and speaker `device: "auto"` looks through `/proc/asound/cards` for a card whose name contains `match`, and uses `plughw:CARD=<id>,DEV=0`. If nothing matches it logs a warning and uses `fallback_device` (`plughw:1,0` for the mic, `plughw:2,0` for the speaker). ALSA card numbers can change between boots, so names are used instead of numbers. Setting `microphone.device`, `speaker.device` or the `BMO_*_DEVICE` variables bypasses detection.

## Server API

Endpoints used: `GET /v1/status`, `GET /v1/models`, `POST /v1/bmo/reservation`, `DELETE /v1/bmo/reservation`, `POST /v1/bmo/interact` (fields `audio`, `image`, `speak`), `POST /v1/audio/transcriptions`, `POST /v1/audio/speech`, `POST /v1/chat/completions`, `POST /v1/requests/{id}/cancel`. Every request carries the credential and a unique `X-Request-ID`.

**Reservation.** Before inference the client reserves the GPU. A `200` means ready. A `202` means the server is restoring, so the client polls `/v1/status` until `bmo_ready`, then POSTs again and requires `200`. The reservation lasts 300 s and is renewed on use (the client renews after 240 s idle). It is released with `DELETE` on exit or SIGTERM.

**Interruption.** Pressing Start (or `i`) while thinking or speaking:

1. stops the speaker immediately,
2. marks the in-flight interaction stale so its response is dropped and never played,
3. cancels it on the server via `POST /v1/requests/{id}/cancel` using its `X-Request-ID`,
4. before the next request, waits for `/v1/status` `bmo_ready` and re-confirms the reservation, because a cancel acknowledgement does not mean the GPU is clean.

## Testing

```bash
./setup.sh --skip-apt --dev     # or: pip install -r requirements-dev.txt
./venv/bin/python -m pytest
```

Unit tests use a fake server. `tools/bmo_test.py` is the reference acceptance test that proved the hardware and server path on this Pi. `python -m app --self-test` runs the app's own diagnostics.

## Troubleshooting

* **No sound:** use the `plughw:` layer, not raw `hw:`. The USB speaker only works through ALSA's plug layer. List cards with `aplay -l` and `arecord -l`, check the names against `speaker.match` and `microphone.match`, or set `BMO_SPEAKER_DEVICE` / `BMO_MIC_DEVICE` explicitly.
* **Mic too quiet:** raise `microphone.gain_db` (or `BMO_MIC_GAIN_DB`), and check capture level and AGC with `amixer -c <card> contents` / `alsamixer`. The app sets capture to 100% and enables AGC by default.
* **401 Unauthorized:** the wrong credential. Use the physical BMO credential, not the general one, and check which file is picked up (see the Credential section).
* **202 / long wait on first request:** the server is restoring the BMO model. The client waits (up to `readiness_timeout`, 300 s) and retries on its own.
* **No display:** without X, `--gui` exits with "No display available". Run `tools/setup_display.sh`, or use `--headless`. The DSI panel needs `dtoverlay=vc4-kms-dsi-7inch` in `/boot/firmware/config.txt`.
* **Shutdown noise:** `alsa_snd_pcm_mmap_begin` messages on Ctrl+C are harmless.

## Customizing the character

Faces are PNG sequences in `faces/<state>/` and sounds are `.wav` files in `sounds/<category>/`. Replace them to give the robot a new look; one sound is picked at random per category.

## License

This project is dual-licensed:

* **Software / Code:** All source code is licensed under the [MIT License](LICENSE).
* **Hardware / 3D Models:** The `.obj`, `.stl`, and other 3D modeling files associated with the physical case are licensed under the [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International License](https://creativecommons.org/licenses/by-nc-sa/4.0/)

## Legal Disclaimer
Disclaimer: Fan Project
This repository and the associated voice model are a non-commercial, open-source fan project. "BMO" and Adventure Time are registered trademarks and copyrights of Cartoon Network and Warner Bros. Discovery. This project is not affiliated with, endorsed by, or sponsored by Cartoon Network or its parent companies.

Voice Model Attribution
The text-to-speech capabilities of this project are powered by Piper. The custom voice model was fine-tuned locally using Piper's base "Amy" model (en_US-amy-medium). The original Piper engine and base models are developed by the Rhasspy project and distributed under the MIT License.
