import json
import threading

from telegram_stt.jobstore import JobStore


def record(chat=-100, msg=1):
    return {"chat_id": chat, "message_id": msg, "placeholder_id": msg + 1,
            "media": {"file_id": f"F{msg}", "kind": "voice", "duration": 7,
                      "file_name": None, "file_size": 100}}


def test_empty_store(tmp_path):
    s = JobStore(tmp_path / "p.json")
    assert s.pending() == [] and s.count() == 0


def test_add_remove_and_persist(tmp_path):
    path = tmp_path / "p.json"
    s = JobStore(path)
    s.add(record(msg=1)); s.add(record(msg=2))
    assert s.count() == 2
    s.remove(-100, 1)
    assert s.count() == 1 and s.pending()[0]["message_id"] == 2
    # a fresh instance sees the same state -- this is what survives a restart
    assert JobStore(path).count() == 1


def test_removing_something_absent_is_a_no_op(tmp_path):
    s = JobStore(tmp_path / "p.json")
    s.add(record(msg=1))
    s.remove(-100, 1)
    s.remove(-100, 1)
    assert s.count() == 0


def test_same_job_is_not_duplicated(tmp_path):
    s = JobStore(tmp_path / "p.json")
    s.add(record(msg=5)); s.add(record(msg=5))
    assert s.count() == 1


def test_corrupt_file_does_not_wedge_startup(tmp_path):
    path = tmp_path / "p.json"
    path.write_text("{ not json at all")
    s = JobStore(path)
    assert s.pending() == []      # degrade, do not raise
    s.add(record(msg=9))
    assert s.count() == 1


def test_missing_file_is_treated_as_empty(tmp_path):
    assert JobStore(tmp_path / "never-written.json").pending() == []


def test_concurrent_writers_keep_the_file_valid(tmp_path):
    """The poller adds while the worker removes; neither may corrupt it."""
    path = tmp_path / "p.json"
    s = JobStore(path)
    threads = [threading.Thread(target=lambda b=b: [s.add(record(b, i)) for i in range(60)])
               for b in (1, 2)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert s.count() == 120
    json.loads(path.read_text())          # still parseable

    threads = [threading.Thread(target=lambda b=b: [s.remove(b, i) for i in range(60)])
               for b in (1, 2)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert s.count() == 0
    json.loads(path.read_text())


def test_write_is_atomic_leaving_no_partial_file(tmp_path):
    path = tmp_path / "p.json"
    s = JobStore(path)
    s.add(record(msg=1))
    assert not list(tmp_path.glob("*.tmp")), "temp file was left behind"
