# telegram-stt

Send a recording to a Telegram bot; get back a timestamped transcript and a
summary. Whisper runs on the Apple GPU via MLX and the LLM is local, so no audio
or text leaves the machine.

Measured on an M4 Pro: a **51-minute meeting transcribes in 29 seconds** (~105×
realtime); summarising it takes about four minutes.

## How it works

```
  Telegram ──MTProto──► telegram-bot-api ──getUpdates──► poll loop
                        (127.0.0.1:8081,  ◄─getFile───┐  (main thread)
                         --local)          path on disk│      │ queue + pending.json
                                                       │      ▼
                                                       │  transcriber ── ffmpeg 16kHz
                                                       │  (1 thread)  ── mlx-whisper (Metal)
                                                       │      │
                                                       │      ▼
                                                       │   archive ──► summariser
                                                       │  data/       (1 thread)
                                                       │  archive/         │
                                                       │      ▲            ▼
                                                       │      │      LM Studio
                                                       │      │      127.0.0.1:1234
                                                       │      │
                                                    web UI ◄──┘
                                                 127.0.0.1:8090
```

**Why a local Bot API server.** Against `api.telegram.org` the download limit is
20 MB. Run tdlib's server with `--local` and it becomes 2000 MB, and `getFile`
returns an **absolute path on disk** — so there is no download step at all.

### One job, end to end

| Stage | What happens |
| --- | --- |
| **Receive** | Poll loop sees the message. A re-send (same `file_unique_id`) returns the existing transcript and stops here. |
| **Accept** | Job written to `data/pending.json` *before* queueing, then acknowledged in the chat. |
| **Transcribe** | ffmpeg decodes once to 16 kHz mono in memory; mlx-whisper reads that array. Progress comes from its real decode position. |
| **Archive** | Audio, transcript, per-segment JSON and an index line written to `data/archive/`. |
| **Deliver** | `.txt` uploaded with a **✨ Summarise this** button; status message deleted. |
| **Summarise** | Automatic over `AUTO_SUMMARIZE_OVER_SECONDS`, else on tap. Long transcripts fold map-reduce. Posted as text plus a Word document. |
| **Title** | The model names and tags the recording from its summary. |

Transcription and summarisation are **one thread each**: the GPU and the LLM are
each a single resource, so concurrency makes both slower.

### State on disk

| Path | Purpose |
| --- | --- |
| `data/archive/history.jsonl` | Append-only index, one line per job |
| `data/archive/<date>/<stem>.{m4a,txt,json}` | Audio, transcript, segments |
| `data/archive/<date>/<stem>.summary.md` | Summary (Word is rendered on demand) |
| `data/archive/<date>/<stem>.meta.json` | Title, tags, deleted — the only mutable file |
| `data/pending.json` | Accepted but unfinished jobs |
| `data/state.json` | Telegram update offset |

The index is append-only so it survives a crash mid-write and greps cleanly.
Edits live in the sidecar, which keeps the archive a directory of files that
rsync backs up.

## Layout

| Path | What it is |
| --- | --- |
| [`bot.py`](src/telegram_stt/bot.py) | Poll loop, worker threads, delivery |
| [`telegram.py`](src/telegram_stt/telegram.py) | Bot API client |
| [`transcribe.py`](src/telegram_stt/transcribe.py) | ffmpeg decode + MLX Whisper |
| [`archive.py`](src/telegram_stt/archive.py) | On-disk history, search, retention |
| [`meta.py`](src/telegram_stt/meta.py) | Titles, tags, deletion |
| [`llm.py`](src/telegram_stt/llm.py) | Summaries and titles via a local LLM |
| [`docx_export.py`](src/telegram_stt/docx_export.py) | Summary → Word |
| [`web.py`](src/telegram_stt/web.py) · [`ui.html`](src/telegram_stt/ui.html) | The web app |
| [`jobstore.py`](src/telegram_stt/jobstore.py) | Crash-durable pending jobs |
| [`cli.py`](src/telegram_stt/cli.py) | Transcribe a file, no Telegram |
| [`scripts/`](scripts/) | deploy, daemons, backup, local dev |

## Setup

**1. Credentials.** `cp .env.example .env && chmod 600 .env`, then fill in
`TELEGRAM_API_ID` / `TELEGRAM_API_HASH` (from <https://my.telegram.org>, needed
because the local server speaks MTProto) and `TELEGRAM_BOT_TOKEN` from
[@BotFather](https://t.me/BotFather). Set `ALLOWED_CHAT_IDS` once you know the
chat — send `/status` to learn it.

**2. Deploy.** `./scripts/deploy.sh --bootstrap` — installs `uv` and `ffmpeg`,
builds `telegram-bot-api` from source (15–30 min cold; no Homebrew formula
exists), and installs launchd agents. After that, `./scripts/deploy.sh`.

**3. Survive a reboot.** Agents need a login session, so a reboot that stops at
the login window leaves the bot silently dead. Convert to system daemons:

```bash
ssh -t macbook-pro-14-m4-pro 'cd ~/apps/telegram-stt && sudo ./scripts/install-daemons.sh'
```

`-t` gives `sudo` a terminal. The installer removes the agents first, then
verifies the worker loaded, the API answers, and Metal is reachable from the
system domain. Undo with `--remove`.

**4. One server per token.** Telegram allows a bot on one Bot API server at a
time. If the token has used `api.telegram.org`, run
`./scripts/deploy.sh --logout-cloud`, wait ~10 minutes, then `--restart`.

## Operating

```bash
./scripts/deploy.sh --status    # services + health
./scripts/deploy.sh --logs      # tail remote logs
./scripts/deploy.sh --ui        # tunnel the web app here and open it
./scripts/deploy.sh --backup    # pull the archive to this Mac
./scripts/deploy.sh --restart | --stop | --shell
```

Logs: `~/Library/Logs/telegram-stt/` on the target. If the short hostname stops
resolving, the script falls back to the mDNS `.local` name.

**Reaching the web app.** It binds to `127.0.0.1` and has **no authentication**,
because it serves transcripts of private conversations. Use `--ui` for an ssh
tunnel, or Tailscale Serve on the target for a real URL that works from a phone:

```bash
tailscale serve --bg 8090
```

That keeps the app on loopback and lets Tailscale terminate HTTPS and enforce
tailnet identity — better than binding to `0.0.0.0`. It is **Serve, not
Funnel**; Funnel would publish it to the internet. Everyone on the tailnet can
read everything, so use an ACL if that is not what you want. Serve config
belongs to a node identity and is lost if the machine re-registers.

## In the chat

```
🎧 Got your 51 min recording — starting now…
🎧 Transcribing… 67% · about 10 sec left
[71 Robinson Rd 21.txt]  📝 51 min recording · English
                         [ ✨ Summarise this ]
```

Progress is plain language, not a block-character bar — those read as a
rendering glitch in a chat client. The `.txt` holds the transcript and nothing
else, one line per Whisper segment prefixed with its position
(`SHOW_TIMESTAMPS=0` for bare text). Every summary comes with a Word document;
short ones are also posted as formatted text.

Commands: `/start`, `/help`, `/status`, `/history`.

## The web app

<http://127.0.0.1:8090> — a reading app, not a dashboard. Recordings first, live
status only while something is running.

- **Read & listen.** Consecutive Whisper segments are joined into paragraphs
  (1011 → 126 for a 51-minute meeting) and set in a serif column. Every
  paragraph plays from its timestamp; the line under the playhead highlights.
  Deep-linkable at `#/t/<id>`.
- **Search** looks *inside* every transcript, ANDing terms, and each hit opens
  the reader playing from that moment.
- **Bulk summarise.** Checkboxes, with shortcuts for "the ones without a
  summary" and "re-summarise these". One queue, one at a time, cancellable —
  a queued job stops immediately, a running one at the next part boundary.
- **Manage.** Click a title to rename it; tags are coloured by name and filter
  the list. `✕` moves a recording to a deleted view where it can be restored or
  erased for good.

`/api/audio/<id>` honours HTTP Range — without 206 responses a browser cannot
seek at all. Audio MIME types are pinned because Python reports `.m4a` as
`audio/mp4a-latm`, which browsers refuse to play.

## Settings

All in `.env`; see [`.env.example`](.env.example) for the full list.

| Setting | Default | Effect |
| --- | --- | --- |
| `WHISPER_MODEL` | `…/whisper-large-v3-turbo` | `-q4` measured 1.2× faster, 0.998 similarity on clean speech |
| `WHISPER_LANGUAGE` | — | Forcing it also skips language detection |
| `WHISPER_INITIAL_PROMPT` | — | Whisper copies its *style*: a punctuated prompt took sentence breaks from 1 per 41 words to 1 per 12 |
| `AUTO_SUMMARIZE_OVER_SECONDS` | `120` | `0` = button only, `-1` = always |
| `PRUNE_AUDIO_AFTER_DAYS` | `0` | Drops audio, keeps transcripts. Audio is ~99% of the archive by size |
| `SKIP_DUPLICATES` | `1` | Recognise a re-sent file |
| `LLM_BASE_URL` | `127.0.0.1:1234` | Any OpenAI-compatible server |
| `WEB_HOST` | `127.0.0.1` | Do not change |

## Running locally

```bash
./scripts/build-bot-api.sh   # once
./scripts/dev.sh             # whole stack, ctrl-c stops both
```

A token can only be polled from one place, so stop the deployment first or use
a second BotFather token. To trigger a job without Telegram at all:

```bash
uv run python -m telegram_stt.cli recording.m4a
```

## Tests

```bash
uv run pytest                 # 280 tests, ~2 min
uv run pytest -m "not slow"   # skip anything loading a real model
```

The fast suite stubs Whisper out. See [tests/README.md](tests/README.md).

## Things that will bite

- **`Bootstrap failed: 5`** means nobody is logged in at the console, so there is
  no session for an agent. Use daemons (setup step 3); `ctl.sh` explains it.
- **Two copies fighting.** Agents and daemons share label names. `ctl.sh`
  detects daemons and refuses to create agents beside them.
- **The archive is not backed up by anything.** Run `--backup` on a schedule.
- **LM Studio has no restart mechanism.** After a reboot the bot returns and
  summaries silently stop until it is started.
- **Telegram keeps undelivered updates for 24 hours.** A sleeping Mac catches up
  on wake within that window; past it they are gone, and a bot cannot read chat
  history to recover them.
- **Model weights** (~1.6 GB) download on first use to `data/huggingface/`,
  which rsync excludes, so redeploys keep them.
