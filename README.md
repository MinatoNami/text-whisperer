# telegram-stt

A Telegram bot that transcribes voice notes and audio files with **Whisper
large-v3-turbo running on the Apple GPU via MLX**. Audio never leaves the Mac.

It runs against a **local [tdlib Bot API server][bot-api]** rather than
`api.telegram.org`. That matters for two reasons:

- the file download limit goes from **20 MB to 2000 MB**, so long recordings work
- in `--local` mode `getFile` returns an **absolute path on this disk** — the
  audio is already local, so there is no download step at all

```
Telegram  ──MTProto──▶  telegram-bot-api (127.0.0.1:8081, --local)
                              │  getUpdates / getFile → /path/on/disk
                              ▼
                        worker (Python)
                              │  ffmpeg → 16 kHz mono
                              ▼
                        mlx-whisper large-v3-turbo (Metal)
                              │
                              └──▶ sendMessage back to the chat
```

## Layout

| Path | What it is |
| --- | --- |
| [`src/telegram_stt/bot.py`](src/telegram_stt/bot.py) | Poll loop + transcription worker thread |
| [`src/telegram_stt/telegram.py`](src/telegram_stt/telegram.py) | Bot API client (local or cloud) |
| [`src/telegram_stt/transcribe.py`](src/telegram_stt/transcribe.py) | ffmpeg decode + MLX Whisper |
| [`src/telegram_stt/archive.py`](src/telegram_stt/archive.py) | Audio + transcript history on disk |
| [`src/telegram_stt/media.py`](src/telegram_stt/media.py) | Which attachments count as audio |
| [`src/telegram_stt/formatting.py`](src/telegram_stt/formatting.py) | Timestamps and progress bar |
| [`scripts/deploy.sh`](scripts/deploy.sh) | Deploy to the M4 Pro over ssh |
| [`scripts/build-bot-api.sh`](scripts/build-bot-api.sh) | Build telegram-bot-api from source |
| [`scripts/ctl.sh`](scripts/ctl.sh) | launchd service control (runs on target) |

## Setup

### 1. Credentials

```bash
cp .env.example .env && chmod 600 .env
```

Fill in:

- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — from <https://my.telegram.org> →
  *API development tools*. The local server speaks MTProto directly, so it
  needs these on top of the bot token.
- `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather).
- `ALLOWED_CHAT_IDS` — leave empty while testing, then lock it down. Send
  `/status` to the bot to learn a chat's ID.

In BotFather, also run `/setprivacy` → **Disable** if you want the bot to see
audio in group chats.

### 2. Deploy

```bash
./scripts/deploy.sh --bootstrap
```

That syncs the code to `macbook-pro-14-m4-pro:~/apps/telegram-stt`, installs
`uv` and `ffmpeg`, builds `telegram-bot-api` from source (**15–30 min cold** —
there is no Homebrew formula for it), resolves the Python environment, and
installs two launchd agents.

Every deploy after that is just:

```bash
./scripts/deploy.sh
```

Override the target with `REMOTE_HOST=other-mac ./scripts/deploy.sh`.

### 3. Moving the bot to the local server

Telegram only lets a bot live on one Bot API server at a time. If the token has
ever talked to `api.telegram.org`, log it out once:

```bash
./scripts/deploy.sh --logout-cloud
```

Then wait ~10 minutes before the local server will accept it, and
`./scripts/deploy.sh --restart`.

## Operating it

```bash
./scripts/deploy.sh --status     # both agents + Bot API health
./scripts/deploy.sh --logs       # tail ~/Library/Logs/telegram-stt
./scripts/deploy.sh --restart
./scripts/deploy.sh --stop
./scripts/deploy.sh --shell      # ssh in, cd'd to the app dir
```

Logs live in `~/Library/Logs/telegram-stt/` on the target:
`worker.out.log`, `worker.err.log`, `bot-api.log`.

## Behaviour

Send a voice note, audio file, video note, video, or an audio/video document.
Each job runs in visible stages, all edits to one status message so channels
stay quiet:

```
📥 Received voice note (23s)
⬇️ Downloaded 86.7 KB — decoding 23s…
🎧 Transcribing
████████░░░░ 67%
→ [.txt uploaded, status message deleted]
```

The progress bar is real, not a timer: mlx-whisper drives an internal `tqdm`
over audio frames, so it reports actual position. It updates once per
30-second decode window — a 15-minute recording ticks ~32 times, a 20-second
voice note only once. Edits are throttled to `PROGRESS_INTERVAL` (default 4s)
because Telegram flood-limits them.

The uploaded `.txt` holds the transcript and nothing else — one line per
Whisper segment, prefixed with its position in the recording:

```
[00:00] Morning. Did you get a chance to look at the pipeline changes?
[00:04] I did, yes. The caching layer looks solid, but I had one concern.
[00:11] That's fair. What specifically worried you about it?
```

Set `SHOW_TIMESTAMPS=0` to drop the prefixes and get bare text. Run metadata
(model, language, timing) lives in the upload caption and the archive index,
not in the file.

The file is named after the source (`meeting.txt`), falling back to
`transcript-<message_id>.txt` for voice notes, which carry no filename.
Transcripts short enough for Telegram's 1024-character caption limit are also
put in the caption, so they are readable without downloading.

Commands: `/start`, `/help`, `/status`, `/history`.

### Archive

Every job is kept under `data/archive/` (override with `ARCHIVE_DIR`):

```
data/archive/
  2026-08-17/
    20260817-183125-<chat>-<message>.ogg     original audio, as received
    20260817-183125-<chat>-<message>.txt     rendered transcript
    20260817-183125-<chat>-<message>.json    segments with timestamps
  history.jsonl                              one line per job, newest last
```

The index is append-only JSONL, so it survives a crash mid-write and can be
grepped or tailed without parsing the archive. `/history` reports totals and
the five most recent jobs. `KEEP_AUDIO=0` archives transcripts only.

The archive copies the audio *before* `DELETE_MEDIA_AFTER` removes the Bot API
server's copy, so the two settings do not fight. Nothing prunes the archive —
it grows without bound, which is the point, but keep an eye on it.


Tunables in `.env`: `WHISPER_MODEL`, `WHISPER_LANGUAGE` (empty = auto-detect),
`WHISPER_INITIAL_PROMPT` (bias toward names/jargon Whisper keeps mangling),
`MAX_AUDIO_SECONDS`, `DELETE_MEDIA_AFTER`, `LOG_LEVEL`.

Transcription is serialised through a single worker thread — the GPU is the
bottleneck, so running jobs concurrently only makes each one slower. The poll
loop stays responsive while a job runs, and up to 32 jobs can queue.

### Performance

Measured on 15 min of varied speech (M5 Max; the M4 Pro target is slower but
the ratios hold):

| stage | time |
| --- | --- |
| ffmpeg decode | 0.97 s |
| mel spectrogram | 0.02 s |
| inference | 7.29 s — **123× realtime** |

Choices that are already load-bearing, and shouldn't be "optimised" away:

- **Audio is decoded once, in memory.** Passing mlx-whisper a *file path* makes
  it spawn its own ffmpeg to redo the exact same conversion
  (`log_mel_spectrogram` → `load_audio`), so `decode_to_array` does it once and
  passes the waveform. Same peak memory, minus a subprocess and a temp file.
- **The model stays resident.** mlx-whisper's `ModelHolder` caches it across
  calls, so `warmup()` at startup is what keeps every later job off the
  load path.
- **`condition_on_previous_text=False`** measured 23% *faster* than the default
  and avoids Whisper's degenerate repeat loops on long recordings.
- **The temperature fallback ladder is free on clean speech** — it only fires
  when the compression-ratio check trips. Don't pin `temperature=0` to "speed
  it up"; you would only be removing the hallucination guard.

## Running locally instead

```bash
uv sync
uv run python -m telegram_stt
```

Reads the same `.env`. Point `BOT_API_BASE_URL` at `https://api.telegram.org`
to skip the local server entirely — everything still works, but you are back to
the 20 MB download limit.

## Notes and gotchas

- **launchd domain.** `ctl.sh` prefers `gui/$UID` because Metal wants the Aqua
  session, falling back to `user/$UID` over headless ssh. If transcription
  fails with Metal device errors, make sure the Mac is logged in at the
  console. FileVault means it will not be after an unattended reboot.
- **Model weights** (~1.6 GB) download on first use into
  `data/huggingface/`, which is excluded from rsync, so redeploys keep them.
- **Downloaded media** is deleted after each job. The local Bot API server
  never cleans up after itself, so turning `DELETE_MEDIA_AFTER=0` off will fill
  the disk over time.
- **Sleep.** A sleeping laptop does not poll. `sudo pmset -a sleep 0` (or
  `caffeinate`) if you want it always-on with the lid open.

[bot-api]: https://github.com/tdlib/telegram-bot-api
