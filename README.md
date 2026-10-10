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

Removed compared to upstream: Ollama, whisper.cpp, Piper and its voices, Moondream, DuckDuckGo search, the local-model chat memory, and `sounddevice`/`numpy`/`scipy`. The monolithic `agent.py` is now the `app/` package. Faces, sounds, the state machine concept and OpenWakeWord (optional, off by default) are kept. See `PLAN.md` for the full refactor plan.

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
| `audio_stream` | `false` | Play the reply audio in pieces while the server is still making it (speech starts sooner, songs play without gaps); `false` waits for the whole WAV. The env var `BMO_AUDIO_STREAM=1` (or `0`) overrides it. Each turn logs `Turn audio: asked=... got=...` and when its audio started |
| `token_file` | `/etc/bmo/token` | Credential file |
| `model` | `bmo-qwen3-vl-8b` | Server model name |
| `microphone.device` / `.match` / `.fallback_device` | `auto` / `USB PnP Sound Device` / `plughw:1,0` | Mic selection |
| `microphone.gain_db` | `18` | Software gain applied before upload |
| `speaker.device` / `.match` / `.fallback_device` | `auto` / `UACDemoV1.0` / `plughw:2,0` | Speaker selection |
| `camera.enabled`, `.rotation` | `true`, `0` | Camera on/off, rotation (0/90/180/270) |
| `camera.vision_mode` | `manual` | `off`, `always` or `manual` |
| `speaker.volume` | `50%` | Speaker volume set at startup |
| `ui.enabled`, `.fullscreen` | `true`, `true` | Face GUI |
| `ui.face_style`, `.faces_svg_dir` | `svg`, `faces_svg` | `svg` = animated vector face; `png` = the old still frames in `faces/` |
| `ui.text_only` | `false` | Mute: replies and errors are text only, nothing is played |
| `web.enabled`, `.port`, `.pin_file` | `true`, `8080`, `~/.config/bmo/web_pin` | Local web page |
| `web.tls`, `.tls_port`, `.http_redirect` | `off`, `8443`, `true` | `self_signed` (cert made on the Pi) or `files` (`.cert_file`/`.key_file`, e.g. Let's Encrypt). With TLS on, port 8080 only redirects |
| `web.trusted_proxies`, `.public_origins`, `.tls_hostnames` | `[]` | For serving behind a reverse proxy / a domain later |
| `sounds.*` | all `true` | Greeting, ack and thinking sounds |
| `input.evdev_enabled`, `.evdev_device` | `true`, `auto` | Read HID keyboards from `/dev/input` |
| `wake_word.enabled`, `.model`, `.custom_model`, `.threshold` | `false`, `hey_jarvis`, `models/hey_bmo.onnx`, `0.5` | Hands-free wake word (openWakeWord, runs on the Pi). The custom model is used when the file exists |
| `microphone.backend` | `auto` | `stream` = one always-on capture shared by wake word, auto-stop and recording (auto picks it when the wake-word packages are installed); `file` = the old press-to-start/press-to-stop recording |
| `listen.end_silence_seconds`, `.no_speech_timeout` | `0.9`, `6` | Auto-stop after this much silence; give up if nobody speaks |
| `listen.followup`, `.followup_seconds` | `true`, `5` | After BMO answers out loud (or you cut it off mid-reply), listen again briefly without the wake word |
| `listen.followup_min_speech_seconds` | `0.15` | In a follow-up, sounds shorter than this (clicks, echo) are ignored instead of ending the turn |
| `memory.enabled`, `.max_messages`, `.max_chars`, `.session_idle_minutes` | `true`, `10`, `6000`, `30` | Conversation memory: recent exchanges (saved to `runtime/memory.json`) are sent with each turn, and the server's long-term recall is used. Saying or typing "forget everything" wipes both |

Settings changed from the web page (volume, mute, camera mode, sound effects, mic boost, memory, wake word, follow-up) are saved to `runtime/settings.json` and override `config.json` at the next start.

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

## Conversation memory

BMO remembers the last few exchanges. After every successful turn the Pi saves the user's words and BMO's reply (text only, never images) to `runtime/memory.json`, keeping the newest 10 messages (`memory.max_messages`, within `memory.max_chars`). Every turn sends them to the server as the `history` form field, so "what did I just ask?" works, also after a restart. Interrupted and failed turns are not saved.

* **Long-term memory** (facts that outlive the last 10 messages) lives on the server, which recalls and stores them itself. The Pi only lists, deletes and wipes them through `/v1/bmo/memories`. Until the server has those routes the page says it is not set up yet, and nothing breaks.
* **Sessions.** The full conversation is also kept as a session in `runtime/session.json` (up to 80 messages). When the session ends, the Pi sends it to the server (`POST /v1/bmo/memories/consolidate`) to be turned into long-term memories, then starts a fresh one (empty short-term memory and chat). A session ends after `memory.session_idle_minutes` (default 30, 0 = never) without activity, when you press **New session** on the web page, or when you say or type "new session", "new conversation", "start over" or "new chat". Saying goodbye ("bye BMO", "thanks, that's all for now", "good night") lets BMO answer, then ends the session the same way and skips the follow-up listening, so BMO goes quiet until the wake word. A session left over from before a restart is saved at startup if it is stale. If the server is unreachable the session waits in `runtime/pending_sessions/` (newest 20) and is retried later; if the server has no consolidate route it is dropped. Nothing is saved at shutdown, and "forget everything" discards the session and any pending ones.
* **Forgetting.** Typing "forget everything" or "reset memory" (also with "BMO" first or "please" last) clears the Pi's history and asks the server to wipe long-term memory, without sending the phrase to the model. Saying it is handled by the server, which answers with `memory_reset` so the Pi clears its history too. The web page has a "Forget everything" button.
* **Off switch.** `memory.enabled` (web page toggle) stops sending history and tells the server not to recall or store anything.
* The server side is specified in `docs/bmo-memory-plan.md` in the server repo (`~/BMO/.worktrees/bmo-dual-gpu` on `3070server`).

## Web page

While BMO runs (`--headless` or `--gui`), it serves a page from any device on the home network. With `web.tls` on it is `https://192.168.0.218:8443` (port 8080 redirects there); otherwise `http://192.168.0.218:8080`. It offers:

* **Talk:** a mic button (tap, speak, it stops by itself when you pause) and a text box. Browsers only allow the mic over HTTPS, so turn on `web.tls`.
* **Reply audio, per device:** This device / BMO / Both / Text only, remembered by each browser. Wake-word replies never play on phones.
* **Progress indicator:** what BMO is actually doing (listening, looking, thinking, getting the GPU ready, talking, playing here).
* **New session (header button):** saves the conversation to long-term memory and clears the chat; a line shows whether it was saved.
* **Settings sheet (gear):** reply audio, BMO's speaker, listening (wake word, follow-up, mic boost), camera, memory (session size and idle save time, list, delete, forget everything), server status, log out.

**HTTPS.** `"tls": "self_signed"` makes a certificate in `~/.config/bmo/tls/` naming this Pi's hostnames and addresses (remade if the address changes). Each browser shows a warning once; accept it. A real certificate (e.g. for a joeyspace.dev name) goes in with `"tls": "files"`.

**PIN.** The first start creates a 6-digit PIN in `~/.config/bmo/web_pin` (mode 600). Read it with `cat ~/.config/bmo/web_pin`; delete the file to get a new one. After 5 wrong PINs a device is locked out for 5 minutes; 30 wrong PINs in an hour from anywhere lock logins for 15 minutes.

**What it exposes.** The page talks only to the Pi; the server credential never reaches the browser. Anyone logged in can talk through BMO's speaker, use its camera and read its memories, so keep it on your home network (or behind an extra login such as Cloudflare Access) and use a long passphrase as the PIN before putting it on the internet. Turn it off with `"web": {"enabled": false}`.

## Hands-free listening

With `wake_word.enabled` (web page toggle) BMO listens for the wake phrase ("Hey Jarvis" until `models/hey_bmo.onnx` exists), records until you stop talking, answers, then listens about 5 s for a follow-up without the wake phrase. It never listens to itself: detection pauses while BMO thinks or plays any sound. Start still works and also stops on silence. Tune the threshold with `./venv/bin/python tools/wake_test.py` (stop the service first; only one program can use the mic). Packages: `requirements-wakeword.txt`.

## Device detection

Mic and speaker `device: "auto"` looks through `/proc/asound/cards` for a card whose name contains `match`, and uses `plughw:CARD=<id>,DEV=0`. If nothing matches it logs a warning and uses `fallback_device` (`plughw:1,0` for the mic, `plughw:2,0` for the speaker). ALSA card numbers can change between boots, so names are used instead of numbers. Setting `microphone.device`, `speaker.device` or the `BMO_*_DEVICE` variables bypasses detection.

## Server API

Endpoints used: `GET /v1/status`, `GET /v1/models`, `POST /v1/bmo/reservation`, `DELETE /v1/bmo/reservation`, `POST /v1/bmo/interact` (fields `audio`, `image`, `text`, `speak`, plus `history` and `memory` when conversation memory is on), `GET`/`DELETE /v1/bmo/memories` and `DELETE /v1/bmo/memories/{id}` (long-term memory; a 404 means the server does not have them yet), `POST /v1/audio/transcriptions`, `POST /v1/audio/speech`, `POST /v1/chat/completions`, `POST /v1/requests/{id}/cancel`. Every request carries the credential and a unique `X-Request-ID`.

**Reservation.** Before inference the client reserves the GPU. A `200` means ready. A `202` means the server is restoring, so the client polls `/v1/status` until `bmo_ready`, then POSTs again and requires `200`. The reservation lasts 300 s and is renewed on use (the client renews after 240 s idle). It is released with `DELETE` on exit or SIGTERM.

**Interruption.** Pressing Start (or `i`, or Stop on the web page) while thinking or speaking:

1. stops the speaker immediately,
2. marks the in-flight interaction stale so its response is dropped and never played,
3. cancels it on the server via `POST /v1/requests/{id}/cancel` using its `X-Request-ID`,
4. before the next request, waits for `/v1/status` `bmo_ready` and re-confirms the reservation, because a cancel acknowledgement does not mean the GPU is clean.

If you cut BMO off while it was speaking on the Pi (not a web turn), it then listens for a follow-up as after a finished reply, so you can just talk; right after a cut-off reply the wake word alone was missed for many seconds. Sending a new message, starting a new session or shutting down also interrupts, but opens no follow-up.

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

The face is drawn live on the screen from the SVG expressions in `faces_svg/` (800x480 viewBox; circle, ellipse and path with M L H V Q T C S Z; solid colours; part ids `eye-left`, `eye-right`, `mouth`, `brow-*`, `blush-*`, `tongue`, `teeth`). Files are named `NN_name.svg`. BMO morphs between expressions, blinks, lets its eyes wander, and moves its mouth to the loudness of the reply (`mouth_closed/small/open/wide/o`). Each state has an expression (idle `neutral`, listening `listening`, looking `surprised`, thinking `thinking`, error `error`, start-up and long idle `sleepy`). `TkUI.set_emotion("happy")` shows any expression on the idle and speaking face for a few seconds; nothing sets it yet, because the server does not send an emotion. `venv/bin/python tools/face_preview.py out.png` renders every face to a PNG (`--morph a b` shows a transition). If the SVGs fail to load, BMO falls back to the PNG faces in `faces/<state>/`, which are still the older format. Sounds are `.wav` files in `sounds/<category>/`. Replace them to give the robot a new look; one sound is picked at random per category.

## License

This project is dual-licensed:

* **Software / Code:** All source code is licensed under the [MIT License](LICENSE).
* **Hardware / 3D Models:** The `.obj`, `.stl`, and other 3D modeling files associated with the physical case are licensed under the [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International License](https://creativecommons.org/licenses/by-nc-sa/4.0/)

## Legal Disclaimer
Disclaimer: Fan Project
This repository and the associated voice model are a non-commercial, open-source fan project. "BMO" and Adventure Time are registered trademarks and copyrights of Cartoon Network and Warner Bros. Discovery. This project is not affiliated with, endorsed by, or sponsored by Cartoon Network or its parent companies.

Voice Model Attribution
The text-to-speech capabilities of this project are powered by Piper. The custom voice model was fine-tuned locally using Piper's base "Amy" model (en_US-amy-medium). The original Piper engine and base models are developed by the Rhasspy project and distributed under the MIT License.
