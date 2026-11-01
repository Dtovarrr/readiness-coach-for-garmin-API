"""App logging: errors (with tracebacks) go to logs/readiness_coach.log, never with secrets in them.

The Dock launcher already sends stderr to logs/readiness_coach.log; when the app runs from a terminal,
stderr is the terminal, so the file is added too (it's only added when stderr isn't already that file,
so nothing is written twice). Every record passes through a filter that blanks API keys and tokens.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

LOG_FILE = Path(__file__).parent / "logs" / "readiness_coach.log"
SECRET_ENV = ("GARMIN_API_KEY", "ANTHROPIC_API_KEY", "INTERVALS_API_KEY")
TOKEN_RE = re.compile(r"(?i)\b(bearer|basic|apikey|x-api-key)([\s:=\"']+)[A-Za-z0-9._\-+/=]{8,}")


def redact(text: str) -> str:
    for name in SECRET_ENV:
        v = os.getenv(name)
        if v and len(v) >= 8:
            text = text.replace(v, f"[{name} redacted]")
    return TOKEN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)


class Redact(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:  # render the traceback now, so it can be redacted too
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def _same_file(stream, path: Path) -> bool:
    try:
        return path.exists() and os.path.samestat(os.fstat(stream.fileno()), path.stat())
    except (OSError, ValueError, AttributeError):
        return False


def setup() -> logging.Logger:
    log = logging.getLogger("garmin_coach")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers = [logging.StreamHandler(sys.stderr)]
    if not _same_file(sys.stderr, LOG_FILE):
        try:
            LOG_FILE.parent.mkdir(exist_ok=True)
            handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
        except OSError:
            pass
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(Redact())
        log.addHandler(h)
    return log
