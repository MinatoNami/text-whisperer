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
| [`src/telegram_stt/config.py`](src/telegram_stt/config.py) | Every setting, read from `.env` |
| [`src/telegram_stt/formatting.py`](src/telegram_stt/formatting.py) | Timestamps and progress bar |
| [`src/telegram_stt/cli.py`](src/telegram_stt/cli.py) | Transcribe a local file, no Telegram |
| [`src/telegram_stt/web.py`](src/telegram_stt/web.py) | Monitor UI server + download API |
| [`src/telegram_stt/llm.py`](src/telegram_stt/llm.py) | Summarisation via a local LLM |
| [`src/telegram_stt/docx_export.py`](src/telegram_stt/docx_export.py) | Summary → Word document |
| [`src/telegram_stt/jobstore.py`](src/telegram_stt/jobstore.py) | Crash-durable pending-job record |
| [`scripts/deploy.sh`](scripts/deploy.sh) | Deploy to the M4 Pro over ssh |
| [`scripts/build-bot-api.sh`](scripts/build-bot-api.sh) | Build telegram-bot-api from source |
| [`scripts/ctl.sh`](scripts/ctl.sh) | launchd service control (runs on target) |
| [`scripts/install-daemons.sh`](scripts/install-daemons.sh) | Run as system daemons, no login needed |
| [`scripts/backup-archive.sh`](scripts/backup-archive.sh) | Copy the archive somewhere safe |
| [`scripts/dev.sh`](scripts/dev.sh) | Run the whole stack locally |
| [`tests/`](tests/) | Test suite ([tests/README.md](tests/README.md)) |

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

If the short hostname stops resolving — whatever was serving DNS for it went
away — the script falls back to the mDNS `.local` name and says so, rather than
failing. A name that already contains dots is left alone.

### 3. Make it survive a reboot

`--bootstrap` installs launchd **agents**, which live in a login session. After
a reboot that leaves the Mac at the login window there is no session, the
agents never start, and the bot is silently dead — mine was down nine hours
before anyone noticed.

Convert them to system daemons, which have no such dependency:

```bash
ssh -t macbook-pro-14-m4-pro 'cd ~/apps/telegram-stt && sudo ./scripts/install-daemons.sh'
```

The `-t` matters: it gives `sudo` a terminal to prompt on. The installer removes
the agents first (two copies of the same label would fight over the port and
the bot token), then verifies the worker loaded, the Bot API answers, and that
Metal is reachable from the system domain — Whisper does run on the GPU with
nobody logged in, but a daemon is a different context again, so it checks
rather than assumes.

Undo with `sudo ./scripts/install-daemons.sh --remove`.

The alternative, if you would rather stay on agents, is to enable automatic
login so a reboot always lands in a session.

### 4. Moving the bot to the local server

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
🎧 Got your 51 min recording — starting now…
🎧 Transcribing… 67% · about 10 sec left
[71 Robinson Rd 21.txt]  📝 51 min recording · English
                         [ ✨ Summarise this ]
```

Progress is plain language with a rough time remaining, not a block-character
bar — those read as a rendering glitch in a chat client. The percentage comes
from mlx-whisper's real decode position, and edits are throttled to
`PROGRESS_INTERVAL` (default 4s) because Telegram flood-limits them.

The transcript arrives as a `.txt` with a caption saying what it is —
`📝 51 min recording · English`. Run metadata (model, speed, timings) stays in
the archive index where it belongs, rather than in the chat.

```
[00:00] Morning. Did you get a chance to look at the pipeline changes?
[00:04] I did, yes. The caching layer looks solid, but I had one concern.
```

Set `SHOW_TIMESTAMPS=0` for bare text. Transcripts short enough for Telegram's
1024-character caption limit also appear in the caption, readable without
downloading.

### Summaries in the chat

Every transcript arrives with a **✨ Summarise this** button. Tapping it writes
a summary with your local LLM and posts it as formatted text — bold sections,
real bullets — alongside a Word document.

Recordings over `AUTO_SUMMARIZE_OVER_SECONDS` (default 120) are summarised
without being asked, since that is where a summary earns its keep; a fifteen
second voice note is its own summary. `0` makes it button-only, `-1` always
summarises.

**Every summary comes with a Word document.** Short ones are also posted as
formatted text so they can be read without downloading anything; long ones
arrive as the document alone. The web app offers **Download Word** alongside
the raw Markdown. Markdown stays the stored form — it greps, diffs and re-renders — and the `.docx` is
built from it on demand, so changing how the document looks never means asking
the model again.

The document uses real Word semantics rather than text that merely looks
formatted: `Title` and `Heading` styles, `List Bullet` paragraphs backed by
`numbering.xml`, and bold as character formatting. That means it restyles,
folds into a table of contents, and survives being pasted elsewhere.

Summaries are cached beside the transcript, so tapping the button for something
already summarised costs nothing. The button is removed once tapped so it
cannot be fired twice, and summarisation runs on its own thread — the bot keeps
transcribing while a summary is being written.

Commands: `/start`, `/help`, `/status`, `/history`.

### The web app

<http://127.0.0.1:8090> is a reading app for your recordings, not a dashboard.
Recordings come first as cards showing what each meeting was actually about —
the first line of its summary — with a live strip appearing above only while
something is transcribing.

Opening one gives a **reading view**: the transcript set in a serif column at a
comfortable measure, with a sticky player. Whisper emits a segment every few
seconds, so consecutive segments are joined into paragraphs, broken on a real
pause or on length — a 51-minute meeting goes from 1011 unreadable slivers to
126 paragraphs. Every paragraph is a button that plays from its timestamp, and
the paragraph under the playhead highlights and scrolls itself into view.

Reading views are deep-linkable (`#/t/<id>`), so browser back works and a
moment in a meeting can be bookmarked.

It is built for a phone as much as a laptop, and audited as one: cards stack,
tap targets are 42px, the player stays pinned, dialogs go full-screen, and
safe-area insets are respected. Verified from 320px (the narrowest phone still
in use) through tablet, in both orientations, with no horizontal overflow at
any width.

Two things are measured at runtime rather than guessed, because a hardcoded
value is wrong the moment the viewport, the text scale, or the live strip
changes:

- `--top-h` — the sticky header's real height, so the player pins flush beneath
  it instead of leaving a gap the page scrolls through.
- `--sticky-h` — header plus player, used as `scroll-margin-top` on every
  paragraph. Without it, the line highlighted during playback scrolls to
  exactly where the player covers it.

A landscape phone has very little height, so the header and player shrink below
480px tall rather than eating a third of the screen.

`WEB_ENABLED=0` turns it off, `WEB_PORT` moves it.

> **It binds to `127.0.0.1` and has no authentication.** It serves transcripts
> of private conversations, so do not bind it to `0.0.0.0`.

Once deployed, the app runs on the target machine, so reaching it from your
laptop means forwarding the port rather than opening one:

```bash
./scripts/deploy.sh --ui
```

That opens an ssh tunnel and the browser at <http://127.0.0.1:8090>. It refuses
to stack a second tunnel if one is already up, and tells you how to close it.
The equivalent by hand is
`ssh -N -L 8090:127.0.0.1:8090 macbook-pro-14-m4-pro`.

#### Over Tailscale, without a tunnel

If the machine is on a tailnet, Tailscale Serve is nicer — a real URL that
works from a phone, with no tunnel to remember. Run once on the target:

```bash
tailscale serve --bg 8090
```

which gives `https://<host>.<tailnet>.ts.net/`, proxied to `127.0.0.1:8090`.

This is better than pointing `WEB_HOST` at the Tailscale IP: the app stays
bound to loopback and never listens on a network interface, Tailscale
terminates real HTTPS so nothing crosses the wire in plaintext, and access is
limited to the tailnet rather than to anyone who can route to the machine.

Two things to know. It is **Serve, not Funnel** — Funnel would publish it to the
public internet, which is emphatically not what you want for meeting
transcripts. And the app still has no login of its own, so *everyone on the
tailnet* can read every transcript, including nodes shared in from another
tailnet; restrict it with a Tailscale ACL if that is not what you want.

Serve config belongs to a node identity, so reinstalling Tailscale or
re-registering the machine drops it and it needs running again. The ssh tunnel
above keeps working regardless, which is why both are documented.

Download paths are taken from the archive index and re-checked against the
archive root, so a crafted URL cannot read files outside it.

#### Summarising in bulk

Every card has a checkbox. **Select N without a summary** picks exactly the
backlog — the common case, since re-summarising something finished costs
minutes and changes nothing — or **Select all** takes everything shown, which
respects the current search so you can summarise just what a query matched.

The selection bar shows how many will actually run and roughly how long, since
a queue of hour-long meetings is a coffee break rather than a moment.

Summaries run **one at a time**. The LLM is a single resource; ten concurrent
requests would be slower than ten sequential ones and thrash the model's
context. A bar reports which one is running and how far in, each card is
labelled `queued` or `summarising 40%` as it moves, and a failure marks that
one and moves on rather than stalling the rest.

It is one shared queue, so summaries triggered from Telegram — the button, or
the automatic ones — appear in it too.

#### Search

Searching looks **inside** every transcript, ANDing whitespace-separated terms.
Each hit shows the line and its position in the recording, and clicking it
opens the reading view playing from that moment. Search reads the per-segment
JSON, falling back to the flat transcript if an archive predates it.

#### Playback

`/api/audio/<id>` honours HTTP Range. Without 206 responses a browser has to
fetch the whole recording before it can play and cannot seek at all, so this is
what makes jumping to a timestamp work rather than an optimisation. Audio MIME
types are pinned explicitly because Python's `mimetypes` reports `.m4a` as
`audio/mp4a-latm`, which browsers refuse to play.

#### Summaries

Any transcript can be summarised by a local OpenAI-compatible server — LM
Studio, Ollama, llama.cpp.

```
LLM_BASE_URL=http://127.0.0.1:1234   # loopback: transcripts never leave the machine
LLM_MODEL=                           # blank = whatever is loaded
LLM_TIMEOUT=600
```

Long meetings are folded map-reduce style — notes per part, then merged — so an
hour of speech needn't fit one context window. Summarisation runs on a worker
thread and the UI polls `/api/summary-status`, so it reports **"part 3 of 4"**
against a real progress bar rather than an indeterminate spinner for minutes.

Summaries are stored beside their transcript as `<stem>.summary.md`, so
re-opening one is instant and costs nothing. Reasoning models' `<think>` blocks
are stripped; a reply containing *only* reasoning is reported as a failure
rather than presented as a summary.

### Crash durability

The poll loop advances its Telegram offset when a message is *queued*, not when
it is transcribed — otherwise a long job would make the bot re-fetch the same
updates forever. That leaves a gap: a job in the queue when the process dies is
lost, and Telegram will not resend it.

So each accepted job is written to `data/pending.json` before being queued and
removed only on a terminal outcome. Anything still there at startup is
re-queued, and the user sees `🔄 Resumed after restart`. Failed jobs are cleared
too, so a permanently broken one cannot retry-loop forever.

This matters because the laptop sleeps. Telegram holds undelivered updates for
**24 hours** — within that window a sleeping Mac catches up on wake by itself;
past it they are gone. A bot cannot read chat history to recover them, so
anything older is unrecoverable without a user-account MTProto client.

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

**Nothing backs it up on its own, and it is the only copy of your recordings.**

```bash
./scripts/backup-archive.sh ~/Backups/telegram-stt   # or set BACKUP_DEST
./scripts/deploy.sh --backup                         # pull the M4 Pro's archive here
```

Both are append-only — `--delete` is deliberately not passed, so a mishap at
the source can never erase transcripts already copied. Re-running is cheap and
idempotent. A local path, an iCloud or Dropbox folder, and an rsync remote all
work as destinations. Note the two machines keep **separate** archives that
never sync, so back up whichever one actually receives your recordings.


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

## Running locally

Everything works on any Apple-silicon Mac; the M4 Pro is just where it is
deployed. First time only, build the Bot API server here too — `vendor/` is
excluded from rsync, so a local checkout does not have it:

```bash
./scripts/build-bot-api.sh
```

Then run the whole stack in one terminal:

```bash
./scripts/dev.sh
```

That starts the local Bot API server (logging to `data/dev-bot-api.log`), waits
for it to bind, and runs the worker in the foreground. Ctrl-C stops both.

> **A bot token can only be polled from one place.** Two `getUpdates` loops
> steal each other's updates and trade 409s. Either stop the deployment while
> working locally (`./scripts/deploy.sh --stop`), or — better — make a second
> bot with BotFather and point your local `.env` at that token. Then local work
> never touches production.

### Triggering a job

**Through Telegram** — post a voice note, audio file, or video to a chat in
`ALLOWED_CHAT_IDS`. In a channel the bot must be an administrator to receive
posts at all; in a group it needs privacy mode disabled (`/setprivacy` in
BotFather). Send `/status` in any chat to learn its ID.

**Without Telegram** — run a file straight through the same decode → transcribe
→ render → archive path. No bot token, so it never competes with the deployed
worker:

```bash
uv run python -m telegram_stt.cli recording.m4a
```

The transcript goes to stdout, progress and timing to stderr, so it pipes
cleanly. `-o out.txt` writes to a file instead, `--no-archive` skips the
archive, `--no-timestamps` drops the `[MM:SS]` prefixes. This is the fastest
loop for testing prompt or model changes.

## Tests

```bash
uv run pytest                 # 120 tests, ~30s
uv run pytest -m "not slow"   # skip anything that loads a real model
```

The fast suite stubs Whisper out, so it exercises control flow in
milliseconds rather than loading 1.6 GB of weights. See
[tests/README.md](tests/README.md) for what each file guards; the two worth
knowing about are `test_crash_recovery.py` (restarts the bot with Telegram
serving nothing, proving recovery comes from disk alone) and the
path-traversal cases in `test_archive.py` and `test_web.py`.

## Notes and gotchas

- **`Bootstrap failed: 5: Input/output error`** almost always means nobody is
  logged in at the console, so there is no session for a launchd agent to live
  in. `ctl.sh` now says that instead of leaving you with the raw errno. See
  [Setup step 3](#3-make-it-survive-a-reboot) — daemons avoid it entirely.
- **Model weights** (~1.6 GB) download on first use into
  `data/huggingface/`, which is excluded from rsync, so redeploys keep them.
- **Downloaded media** is deleted after each job. The local Bot API server
  never cleans up after itself, so turning `DELETE_MEDIA_AFTER=0` off will fill
  the disk over time.
- **Sleep.** A sleeping laptop does not poll. `sudo pmset -a sleep 0` (or
  `caffeinate`) if you want it always-on with the lid open.

[bot-api]: https://github.com/tdlib/telegram-bot-api
