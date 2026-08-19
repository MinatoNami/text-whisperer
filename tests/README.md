# Tests

```bash
uv run pytest                 # everything (a few seconds)
uv run pytest -m "not slow"   # skip anything that loads a real model
uv run pytest -m slow         # only the real-model round trip
```

The fast suite never loads Whisper — `fake_transcribe` in `conftest.py` stubs
it out, so the bot's control flow can be exercised in milliseconds. `FakeTelegram`
stands in for a Bot API server run with `--local`, meaning `getFile` hands back
an absolute path on disk exactly like the real thing.

What each file guards:

| File | Covers |
| --- | --- |
| `test_formatting.py` | durations, sizes, the progress bar, transcript layout |
| `test_media.py` | which attachments count as audio; disk round-trip |
| `test_config.py` | env parsing, failure modes, loopback-by-default |
| `test_jobstore.py` | atomic writes, corrupt files, concurrent access |
| `test_archive.py` | on-disk layout, index, **path-traversal refusal** |
| `test_transcribe.py` | ffmpeg decode, length limit, the tqdm progress shim |
| `test_bot_e2e.py` | the whole path: acks, progress, upload, archive, access control |
| `test_crash_recovery.py` | a job survives the process dying mid-flight |
| `test_web.py` | the UI's HTTP surface and **download caging** |

Two are worth knowing about specifically:

- `test_crash_recovery.py` restarts the bot with the fake Telegram serving
  *nothing*, proving recovery comes from `pending.json` alone — matching real
  Telegram, which will not resend an update whose offset was acknowledged.
- `test_transcribe.py::TestProgressShim` is a canary. mlx-whisper exposes no
  progress callback, so we swap the module's `tqdm`. If upstream restructures
  that, these fail rather than the bar silently going dead.
