"""Session logs: one file per session, named by id, one flushed JSON line per message."""

import json
from datetime import date

from bridge.contract import SESSION_ID
from bridge.logging import SessionLog

DAY = date(2026, 9, 15)


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_sessions_are_numbered_per_day_and_named_by_id_alone(tmp_path):
    log = SessionLog(tmp_path, clock=lambda: 0.0)
    assert log.start_session(DAY) == "S-20260915-0001"
    assert log.start_session(DAY) == "S-20260915-0002"
    assert log.start_session(date(2026, 9, 16)) == "S-20260916-0001"
    log.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["S-20260915-0001.jsonl", "S-20260915-0002.jsonl", "S-20260916-0001.jsonl"]
    assert all(SESSION_ID.fullmatch(p.stem) for p in tmp_path.iterdir())


def test_two_logs_on_one_directory_never_share_a_session(tmp_path):
    a, b = SessionLog(tmp_path), SessionLog(tmp_path)
    assert a.start_session(DAY) != b.start_session(DAY)
    a.close()
    b.close()


def test_every_kind_of_line(tmp_path):
    now = 1234.56
    log = SessionLog(tmp_path, clock=lambda: now)
    log.start_session(DAY)
    log.message("out", {"type": "beat"})
    log.message("in", {"type": "hello"}, client="c1/spectator", t_engine=99.94)
    log.raw("{nope", "not JSON", client="c2")
    log.event("connected", client="c3")
    assert lines(log.path) == [  # readable before close: every line is flushed
        {"t_engine": 1234.6, "dir": "out", "msg": {"type": "beat"}},
        {"t_engine": 99.9, "dir": "in", "client": "c1/spectator", "msg": {"type": "hello"}},
        {"t_engine": 1234.6, "dir": "in", "client": "c2", "raw": "{nope", "error": "not JSON"},
        {"t_engine": 1234.6, "dir": "event", "event": "connected", "client": "c3"},
    ]
    log.close()


def test_a_new_session_writes_to_a_new_file(tmp_path):
    log = SessionLog(tmp_path, clock=lambda: 0.0)
    log.start_session(DAY)
    first = log.path
    log.message("out", {"n": 1})
    log.start_session(DAY)
    log.message("out", {"n": 2})
    log.close()
    assert [r["msg"]["n"] for r in lines(first)] == [1]
    assert [r["msg"]["n"] for r in lines(log.path)] == [2]


def test_writing_without_a_session_starts_one(tmp_path):
    log = SessionLog(tmp_path, clock=lambda: 0.0)
    log.event("started")
    assert log.session is not None and log.path.exists()
    log.close()
