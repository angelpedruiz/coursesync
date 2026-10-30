"""Small shared helpers: dates and weeks, file names, downloads, week labels, notifications."""

from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlparse

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"


def semester_week(day: dt.date, start: dt.date) -> int:
    return (day - start).days // 7 + 1


def parse_date(text: str, year: int) -> dt.date | None:
    """First date in a piece of text: 20.10.2026, 2026-10-20, October 22nd, 22 Oct, 20.10. (in that order of trust)."""
    t = text.lower()
    tries = [
        (r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})", lambda m: (int(m[3]), int(m[2]), int(m[1]))),
        (r"\b(\d{4})-(\d{2})-(\d{2})\b", lambda m: (int(m[1]), int(m[2]), int(m[3]))),
        (_MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b", lambda m: (year, _MONTHS.index(m[1]) + 1, int(m[2]))),
        (r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON, lambda m: (year, _MONTHS.index(m[2]) + 1, int(m[1]))),
        (r"\b(\d{1,2})\.(\d{1,2})\.(?!\d)", lambda m: (year, int(m[2]), int(m[1]))),
    ]
    for pat, parts in tries:
        for m in re.finditer(pat, t):
            try:
                return dt.date(*parts(m))
            except ValueError:
                continue
    return None


def url_name(url: str) -> str:
    return unquote(Path(urlparse(url).path).name)


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", name).strip()[:180] or "file"


def download(url: str, dest: Path, headers: dict | None = None, netrc: Path | None = None) -> None:
    """Download to dest atomically (a failed download never leaves a half file, never overwrites)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    if shutil.which("curl"):
        cmd = ["curl", "-sSL", "--fail", "--retry", "3", "-A", UA, "-o", str(tmp)]
        for k, v in (headers or {}).items():
            cmd += ["-H", f"{k}: {v}"]
        if netrc:
            cmd += ["--netrc-file", str(netrc)]
        r = subprocess.run(cmd + [url], capture_output=True, text=True)
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise OSError(r.stderr.strip()[:300] or f"curl exit {r.returncode}")
    else:
        if netrc:
            raise OSError("netrc auth needs curl")
        req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
        with urllib.request.urlopen(req, timeout=300) as resp, tmp.open("wb") as f:
            shutil.copyfileobj(resp, f)
    tmp.replace(dest)


def get_text(url: str, netrc: Path | None = None) -> str:
    if netrc and shutil.which("curl"):
        r = subprocess.run(["curl", "-sSL", "--fail", "-A", UA, "--netrc-file", str(netrc), url], capture_output=True, text=True)
        if r.returncode != 0:
            raise OSError(r.stderr.strip()[:300])
        return r.stdout
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode(errors="replace")


def free_path(dest: Path) -> Path:
    """dest, or dest with -2, -3 … if a different file already has that name."""
    if not dest.exists():
        return dest
    i = 2
    while (p := dest.with_name(f"{dest.stem}-{i}{dest.suffix}")).exists():
        i += 1
    return p


def recording_tag(folder: Path, week: int, tutorial: bool = False, day: dt.date | None = None,
                  template: str | None = None) -> str:
    """L07 for the week's first recording; L07b, L07c … for more sessions; L07-tut for a tutorial.

    A template such as '{date}' or 'Lecture {week:02}' replaces the L07 part."""
    stem = safe_name(template.format(week=week, date=day)) if template else f"L{week:02d}"
    base = stem + ("-tut" if tutorial else "")
    stems = {p.stem for p in folder.glob("*")} if folder.exists() else set()
    taken = lambda t: any(s == t or s.startswith(t + "-") or s.startswith(t + ".") for s in stems)  # noqa: E731
    if not taken(base):
        return base
    for letter in "bcdefgh":
        if not taken(base + letter) and not (not tutorial and taken(f"{stem}{letter}")):
            return base + letter
    raise ValueError(f"too many recordings in week {week}")


def label(name: str, rules: list[dict]) -> tuple[int | None, str | None]:
    """(week, new name) from the course's label rules; rules: {match, week: '{1}' or '{1}+1', name: 'L{week:02}-slides'}."""
    for r in rules:
        m = re.search(r["match"], name, re.I)
        if not m:
            continue
        spec = re.fullmatch(r"\s*\{(\d+)\}\s*(?:([+-])\s*(\d+))?\s*", str(r.get("week", "{1}")))
        if not spec:
            raise ValueError(f"bad week spec {r.get('week')!r}: use '{{1}}' or '{{1}}+1'")
        week = int(m[int(spec[1])]) + (int(spec[3]) * (1 if spec[2] == "+" else -1) if spec[2] else 0)
        new = r.get("name")
        return week, (new.format(week=week) + Path(name).suffix) if new else None
    return None, None


def notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "coursesync", title, body], capture_output=True)
