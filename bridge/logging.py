"""Session logs: every message on the link, both directions, one JSONL file per session.

Files are named by session id and nothing else: ``logs/S-20260915-0042.jsonl``. Nothing in a
log names a person. The session id is a date and a counter, clients identify as a kind of
screen, and no field in the message contract carries a name.

This is ``bridge.logging``, not the standard library's ``logging``. Run bridge code as
modules from the repo root (``python -m bridge.server``) so the ``bridge`` directory is never
on ``sys.path``, where this file would shadow the standard library.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import IO, Any

from bridge.clock import t_engine_ms

LOG_DIR = Path("logs")
RAW_LIMIT = 2000  # characters of an unparseable frame kept in the log


class SessionLog:
    """Appends one JSON line per message or event to the current session's file.

    ``{"t_engine": 1234.5, "dir": "out", "msg": {...}}``
        a message the laptop sent
    ``{"t_engine": 1234.5, "dir": "in", "client": "c2/task-screen", "msg": {...}}``
        a valid message a client sent
    ``{"t_engine": 1234.5, "dir": "in", "client": "c2", "raw": "...", "error": "..."}``
        a frame that broke the contract, as received
    ``{"t_engine": 1234.5, "dir": "event", "event": "connected", "client": "c2"}``
        something that happened on the link

    t_engine is when the laptop sent or received it. Each line is flushed as it is written,
    so a crash loses at most the line in progress. Use from one thread.
    """

    def __init__(
        self, directory: str | Path = LOG_DIR, clock: Callable[[], float] = t_engine_ms
    ) -> None:
        self.directory = Path(directory)
        self.clock = clock
        self.session: str | None = None
        self.path: Path | None = None
        self._file: IO[str] | None = None

    def start_session(self, today: date | None = None) -> str:
        """Close the current file, take the next free session id for today, create its file.

        The file is created exclusively, so two processes logging to the same directory
        never share an id.
        """
        self.close()
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = (today or date.today()).strftime("%Y%m%d")
        for n in range(1, 10_000):
            session = f"S-{stamp}-{n:04d}"
            path = self.directory / f"{session}.jsonl"
            try:
                # backslashreplace: a log line can always be written, whatever a string holds.
                self._file = path.open(
                    "x", encoding="utf-8", errors="backslashreplace", newline="\n"
                )
            except FileExistsError:
                continue
            self.session, self.path = session, path
            return session
        raise RuntimeError(f"all 9999 session ids for {stamp} are taken in {self.directory}")

    def message(
        self, direction: str, msg: dict, client: str | None = None, t_engine: float | None = None
    ) -> None:
        record: dict[str, Any] = {"t_engine": self._t(t_engine), "dir": direction}
        if client is not None:
            record["client"] = client
        record["msg"] = msg
        self._write(record)

    def raw(
        self, text: str, error: str, client: str | None = None, t_engine: float | None = None
    ) -> None:
        record: dict[str, Any] = {"t_engine": self._t(t_engine), "dir": "in"}
        if client is not None:
            record["client"] = client
        record["raw"] = text[:RAW_LIMIT]
        record["error"] = error
        self._write(record)

    def event(self, name: str, *, t_engine: float | None = None, **fields: Any) -> None:
        self._write({"t_engine": self._t(t_engine), "dir": "event", "event": name, **fields})

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def _t(self, t_engine: float | None) -> float:
        return round(self.clock() if t_engine is None else t_engine, 1)

    def _write(self, record: dict) -> None:
        if self._file is None:
            self.start_session()
        self._file.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
        self._file.flush()
