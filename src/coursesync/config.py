"""The configuration file (coursesync.yaml): where your courses publish material and where it should go."""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

LAYOUT_KEYS = {"recordings", "recording_name", "past_exams", "sort"}


class ConfigError(ValueError):
    pass


@dataclass
class Course:
    slug: str
    folder: Path                       # this course's folder, e.g. ~/ETH/PAI
    sources: list[dict]
    labels: list[dict] = field(default_factory=list)   # [{match: regex, week: "{1}" | "{1}+1", name?: "L{week:02}-slides"}]
    name: str = ""
    semester_start: dt.date | None = None
    layout: dict = field(default_factory=dict)   # {recordings, recording_name, past_exams, sort: [{match, to}]}

    @property
    def recordings(self) -> Path:
        return self.folder / self.layout.get("recordings", "Recordings")

    def file_folder(self, base: Path, name: str, exam: bool) -> Path:
        """Where a file goes: the first matching `sort` rule, else the exam folder, else the source's own folder."""
        for rule in self.layout.get("sort") or []:
            if re.search(rule["match"], name, re.I):
                return self.folder / rule["to"]
        if not exam:
            return base
        return self.folder / self.layout["past_exams"] if "past_exams" in self.layout else base / "past_exams"


@dataclass
class Config:
    path: Path
    root: Path
    semester_start: dt.date
    courses: dict[str, Course]
    state_dir: Path
    transcribe: dict
    on_new: str | None = None
    notify: bool = True

    def course(self, slug: str) -> Course:
        if slug not in self.courses:
            raise ConfigError(f"unknown course {slug!r} (have: {', '.join(self.courses)})")
        return self.courses[slug]


def _expand(p: str | Path, base: Path) -> Path:
    p = Path(os.path.expanduser(os.path.expandvars(str(p))))
    return p if p.is_absolute() else (base / p)


def default_path() -> Path:
    env = os.environ.get("COURSESYNC_CONFIG")
    if env:
        return Path(env).expanduser()
    for p in (Path.cwd() / "coursesync.yaml", Path.home() / ".config" / "coursesync" / "coursesync.yaml"):
        if p.exists():
            return p
    return Path.home() / ".config" / "coursesync" / "coursesync.yaml"


def load(path: str | Path | None = None) -> Config:
    path = Path(path).expanduser() if path else default_path()
    if not path.exists():
        raise ConfigError(f"no config at {path}: create one with `coursesync init`")
    raw = yaml.safe_load(path.read_text()) or {}
    base = path.parent
    try:
        root = _expand(raw["root"], base)
        start = dt.date.fromisoformat(str(raw["semester_start"]))
    except KeyError as e:
        raise ConfigError(f"{path}: missing required key {e}") from None
    courses = {}
    for slug, c in (raw.get("courses") or {}).items():
        if not isinstance(c, dict):
            raise ConfigError(f"course {slug}: expected a mapping")
        layout = c.get("layout") or {}
        if unknown := set(layout) - LAYOUT_KEYS:
            raise ConfigError(f"course {slug}: unknown layout keys {sorted(unknown)} (have: {sorted(LAYOUT_KEYS)})")
        courses[slug] = Course(slug=slug, folder=_expand(c.get("folder", slug), root), sources=c.get("sources") or [],
                               labels=c.get("labels") or [], name=c.get("name", slug), layout=layout,
                               semester_start=dt.date.fromisoformat(str(c.get("semester_start", start))))
    state = _expand(raw.get("state_dir", "~/.local/state/coursesync"), base)
    return Config(path=path, root=root, semester_start=start, courses=courses, state_dir=state,
                  transcribe=raw.get("transcribe") or {"enabled": False}, on_new=raw.get("on_new"),
                  notify=raw.get("notify", True))
