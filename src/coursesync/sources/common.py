from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

EXAM_RE = re.compile(r"exam|final|midterm|retake|pr(ue|ü)fung|klausur", re.I)
SSH_ENV = {**os.environ, "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}


@dataclass
class Item:
    course: str
    kind: str                   # recording | file | git
    path: str                   # where it is now
    url: str = ""
    week: int | None = None
    title: str = ""
    source: str = ""            # tobira | zoom | moodle | web | git
    exam: bool = False
    tutorial: bool = False
    key: str | None = None      # suggested short name, e.g. L07-slides / L07-rec
    transcript: str | None = None   # path of the transcript (json segments) if transcribed
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Ctx:
    """What a source needs: config, ledger, report, and (for login sources) the browser."""
    cfg: object
    ledger: object
    report: object
    browser: object | None = None
    dry_run: bool = False

    @property
    def start(self) -> dt.date:
        return self.cfg.semester_start


def git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=SSH_ENV)
