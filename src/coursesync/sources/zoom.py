"""Zoom cloud recordings shared by link (no Zoom account needed), and README / Moodle tables listing them."""

from __future__ import annotations

import html
import json
import re
import subprocess
import tempfile
from pathlib import Path

from coursesync.sources.common import Ctx, Item, git
from coursesync.transcribe import save_transcript, seconds
from coursesync.util import UA, parse_date, recording_tag, semester_week


def _curl(args: list[str], jar: str) -> subprocess.CompletedProcess:
    return subprocess.run(["curl", "-sSL", "--fail", "--retry", "3", "-A", UA, "-c", jar, "-b", jar, *args],
                          capture_output=True, text=True)


def _play_info(share_url: str, jar: str) -> tuple[str, dict]:
    """(host, what the web player gets for a shared recording)."""
    m = re.search(r"/rec/(share|play)/([^?]+)", share_url)
    if not m:
        raise OSError("not a Zoom recording link")
    kind, rid = m.groups()
    host = re.match(r"https://[^/]+", share_url).group(0)
    _curl([share_url, "-o", "/dev/null"], jar)
    if kind == "share":
        r = _curl([f"{host}/nws/recording/1.0/play/share-info/{rid}"], jar)
        redirect = ((json.loads(r.stdout or "{}").get("result") or {}).get("redirectUrl")) or ""
        rid = redirect.rsplit("/", 1)[-1].split("?")[0]
        if not rid:
            raise OSError("no play id (link expired, or the recording needs a login)")
        _curl([f"{host}/rec/play/{rid}", "-o", "/dev/null"], jar)
    r = _curl(["-H", f"Referer: {host}/rec/play/{rid}",
               f"{host}/nws/recording/1.0/play/info/{rid}?canPlayFromShare=true&from=share_recording_detail"
               "&continueMode=true&componentName=rec-play"], jar)
    return host, json.loads(r.stdout or "{}").get("result") or {}


def zoom_transcript(share_url: str) -> tuple[list, dict] | None:
    """(segments, meta) of the transcript Zoom keeps with a shared recording, if it has one."""
    with tempfile.NamedTemporaryFile(suffix=".jar") as jar:
        _, res = _play_info(share_url, jar.name)
    segs = [[seconds(t["ts"]), seconds(t["end_ts"]), t["text"].strip()] for t in res.get("transcriptList") or []]
    meet = res.get("meet") or {}
    return (segs, {"topic": meet.get("topic"), "start": meet.get("meetingStartTimeStr")}) if segs else None


def zoom_download(share_url: str, dest: Path) -> dict:
    """Download a shared Zoom recording (the same steps the web player takes). Returns its meta (topic, start)."""
    with tempfile.NamedTemporaryFile(suffix=".jar") as jar:
        host, res = _play_info(share_url, jar.name)
        mp4 = res.get("viewMp4Url")
        if not mp4:
            raise OSError("no video url (link expired, or downloads disabled)")
        part = dest.with_name(dest.name + ".part")
        r = _curl(["-H", f"Referer: {host}/", "-o", str(part), mp4], jar.name)
        if r.returncode != 0:
            part.unlink(missing_ok=True)
            raise OSError(r.stderr.strip()[:300] or "download failed")
        part.replace(dest)
    meet = res.get("meet") or {}
    return {"topic": meet.get("topic"), "start": meet.get("meetingStartTimeStr")}


def fetch_recording(ctx: Ctx, course, url: str, week: int, *, tutorial=False, title="", tag: str | None = None,
                    day=None, keep_video=True) -> Item | None:
    if ctx.ledger.has(course.slug, url):
        return None
    folder = course.recordings
    tag = tag or recording_tag(folder, week, tutorial, day, course.layout.get("recording_name"))
    dest = folder / f"{tag}.mp4"
    if dest.exists():  # never overwrite an existing recording
        ctx.report.problem(course.slug, f"{dest.name} already exists; skipped {url[:60]}…")
        return None
    if ctx.dry_run:
        return Item(course.slug, "recording", str(dest), url, week, title, "zoom", tutorial=tutorial, key=f"{tag}-rec")
    try:
        found = None if keep_video else zoom_transcript(url)
        if not found and not ctx.wanted(keep_video):
            return None
        folder.mkdir(parents=True, exist_ok=True)
        if found:  # already a transcript: no video, no Whisper
            segs, meta = found
            out = save_transcript(dest, segs, {"duration": segs[-1][1], "language": None, "model": "zoom", "device": None})
            ctx.ledger.record(course.slug, "zoom", url, out, week)
            return Item(course.slug, "recording", str(out.with_suffix(".md")), url, week, title or meta.get("topic") or "",
                        "zoom", tutorial=tutorial, key=f"{tag}-rec", transcript=str(out), extra=meta)
        meta = zoom_download(url, dest)
    except OSError as e:
        ctx.report.problem(course.slug, f"recording week {week} ({title or url[:40]}): {e}")
        return None
    ctx.ledger.record(course.slug, "zoom", url, dest, week)
    ctx.fetched_recording(course, dest, keep_video)
    return Item(course.slug, "recording", str(dest), url, week, title or meta.get("topic") or "", "zoom",
                tutorial=tutorial, key=f"{tag}-rec", extra=meta)


def readme_links(text: str, start) -> list[dict]:
    """Zoom links in a markdown list like '[Week 07 (29.10.2026)](https://…zoom.us/rec/share/…)'.

    Further links right under a week entry (extra clips) belong to that week."""
    out, week, day = [], None, None
    for line in text.splitlines():
        for label, link in re.findall(r"\[([^\]]+)\]\((https://[^)]*zoom\.us/rec/[^)]+)\)", line):
            d = parse_date(label, start.year)
            if d and re.search(r"week|lecture|session", label, re.I):
                week, day = semester_week(d, start), d
                out.append({"url": link, "week": week, "title": label, "extra_clip": False, "date": day})
            elif week is not None:
                out.append({"url": link, "week": week, "title": label, "extra_clip": True, "date": day})
    return out


def sync_zoom_readme(ctx: Ctx, course, src: dict) -> list[Item]:
    repo = ctx.cfg.state_dir / "repos" / f"{course.slug}-{Path(src['repo']).stem}"
    r = git(["-C", str(repo), "pull", "--ff-only", "-q"]) if (repo / ".git").exists() else git(["clone", "-q", src["repo"], str(repo)])
    readme = repo / src.get("file", "README.md")
    if r.returncode != 0 or not readme.exists():
        ctx.report.problem(course.slug, f"recordings repo {src['repo']}: git failed (offline or SSH key locked)")
        return []
    items = []
    for link in readme_links(readme.read_text(), course.semester_start):
        tag = None
        if link["extra_clip"]:
            slug = re.sub(r"[^a-z0-9]+", "-", link["title"].lower()).strip("-")[:30]
            tag = f"L{link['week']:02d}-{slug}"
        it = fetch_recording(ctx, course, link["url"], link["week"], title=link["title"], tag=tag, day=link["date"],
                             keep_video=src.get("videos", course.videos))
        if it:
            items.append(it)
    return items


def table_zoom_rows(page_html: str, year: int) -> list[dict]:
    """Zoom links in an HTML page's tables: [{date, url, tutorial}] (tutorial if under a tutorial/exercise heading)."""
    rows = []
    for tm in re.finditer(r"<table.*?</table>", page_html, re.S | re.I):
        before = re.sub(r"<[^>]+>", " ", page_html[max(0, tm.start() - 600):tm.start()])
        first = re.search(r"<tr.*?</tr>", tm.group(0), re.S | re.I)
        head = re.sub(r"<[^>]+>", " ", first.group(0)) if first else ""
        last = (re.findall(r"(lecture|tutorial|exercise)", before, re.I) or [""])[-1].lower()
        tut_table = last in ("tutorial", "exercise") or bool(re.search(r"tutorial|exercise", head, re.I))
        for row in re.findall(r"<tr.*?</tr>", tm.group(0), re.S | re.I):
            links = re.findall(r'href="([^"]*zoom\.us/rec/[^"]+)"', row)
            if not links:
                continue
            text = html.unescape(re.sub(r"<[^>]+>", " ", row))
            d = parse_date(text, year)
            if d:
                rows.append({"date": d, "url": html.unescape(links[0]), "title": " ".join(text.split())[:80],
                             "tutorial": tut_table or bool(re.search(r"tutorial|exercise", text, re.I))})
    return rows
