"""Sources behind the university login: Tobira video portals (video.ethz.ch) and Moodle."""

from __future__ import annotations

import datetime as dt
import html
import re
import time
from urllib.parse import unquote, urljoin

from coursesync.sources.common import EXAM_RE, Ctx, Item
from coursesync.sources.zoom import fetch_recording, table_zoom_rows
from coursesync.util import download, free_path, label, recording_tag, safe_name, semester_week

FILE_EXT = {".pdf", ".zip", ".ipynb", ".py", ".m", ".jl", ".cpp", ".hpp", ".h", ".tex", ".txt", ".md", ".pptx", ".docx",
            ".csv", ".mat", ".npy", ".npz", ".png", ".jpg"}


def sync_tobira(ctx: Ctx, course, src: dict) -> list[Item]:
    base = src.get("base", "https://video.ethz.ch")
    try:
        j = ctx.browser.tobira(base, src["path"])
    except Exception as e:
        ctx.report.problem(course.slug, f"{base}{src['path']}: {e}")
        return []
    seen = ctx.ledger.basenames(course.slug)
    folder = course.recordings
    items = []
    for blk in ((j.get("data") or {}).get("realmByPath") or {}).get("blocks") or []:
        for e in sorted((blk.get("series") or {}).get("entries") or [], key=lambda e: (e.get("syncedData") or {}).get("startTime") or ""):
            if e.get("__typename") != "AuthorizedEvent":
                continue
            tracks = [t for t in e["authorizedData"]["tracks"] if t["mimetype"] == "video/mp4"]
            if not tracks or all("/presenter-" in t["uri"] for t in tracks):
                continue  # camera-only duplicate of a slides+camera recording
            want = int(src.get("resolution", 720))
            pick = next((t for t in tracks if (t.get("resolution") or [0, 0])[1] == want), tracks[0])
            if pick["uri"].rsplit("/", 1)[-1] in seen:
                continue
            day = dt.date.fromisoformat(e["syncedData"]["startTime"][:10])
            week = semester_week(day, course.semester_start)
            tag = recording_tag(folder, week, day=day, template=course.layout.get("recording_name"))
            dest = folder / f"{tag}.mp4"
            it = Item(course.slug, "recording", str(dest), pick["uri"], week, e.get("title") or str(day), "tobira",
                      key=f"{tag}-rec", extra={"date": str(day)})
            if ctx.dry_run:
                items.append(it)
                continue
            print(f"  {course.slug}: downloading recording of {day} (week {week})…", flush=True)
            try:
                download(pick["uri"], dest)
                caps = e["authorizedData"].get("captions") or []
                if caps:
                    download(caps[0]["uri"], dest.with_suffix(".vtt"))
            except OSError as err:
                ctx.report.problem(course.slug, f"recording {day}: {err}")
                continue
            ctx.ledger.record(course.slug, "tobira", pick["uri"], dest, week)
            items.append(it)
    return items


def _links(page: str) -> list[str]:
    return [html.unescape(u) for u in re.findall(r'href="([^"]+)"', page)]


def moodle_files(ctx: Ctx, base: str, course_id: int) -> dict[str, str]:
    """{file url without query: file name} for every file of a Moodle course (sections, labels, resources, folders)."""
    req = ctx.browser.ctx.request
    out: dict[str, str] = {}
    queue = [f"{base}/course/view.php?id={course_id}", f"{base}/course/resources.php?id={course_id}"]
    done: set[str] = set()

    def add(u: str):
        clean = u.split("?")[0]
        if "/pluginfile.php/" not in clean or re.search(
                r"/(user|theme|core|core_admin|course/overviewfiles|course/summary|block_\w+)/|favicon|logo", clean, re.I):
            return
        name = unquote(clean.rsplit("/", 1)[-1])
        if "." + name.rsplit(".", 1)[-1].lower() in FILE_EXT:
            out[clean] = name

    while queue:
        url = queue.pop(0)
        if url in done:
            continue
        done.add(url)
        r = req.get(url)
        if not r.ok:
            continue
        for u in _links(r.text()):
            u = urljoin(base, u).split("#")[0]
            if "/pluginfile.php/" in u:
                add(u)
            elif re.search(r"/mod/folder/view\.php\?id=\d+$", u):
                queue.append(u)
            elif re.search(r"/mod/resource/view\.php\?id=\d+$", u) and u not in done:
                done.add(u)
                rr = req.get(u + "&redirect=1", max_redirects=0)
                loc = rr.headers.get("location") or ""
                if "/pluginfile.php/" in loc:
                    add(urljoin(base, loc))
                elif rr.ok:
                    for v in _links(rr.text()):
                        if "/pluginfile.php/" in v:
                            add(urljoin(base, v))
    return out


def sync_moodle(ctx: Ctx, course, src: dict) -> list[Item]:
    base = src.get("base", "https://moodle-app2.let.ethz.ch")
    try:
        files = moodle_files(ctx, base, src["id"])
    except Exception as e:
        ctx.report.problem(course.slug, f"Moodle {src['id']}: {e}")
        return []
    folder = course.folder / src.get("subdir", "Moodle")
    have = {p.name for p in folder.rglob("*") if p.is_file()} if folder.exists() else set()
    seen = ctx.ledger.bases(course.slug)
    items = []
    for u, name in files.items():
        name = safe_name(name)
        if u in seen or name in have:
            continue
        exam = bool(EXAM_RE.search(name))
        dest = free_path(course.file_folder(folder, name, exam) / name)
        week, key = label(name, course.labels)
        it = Item(course.slug, "file", str(dest), u, week, name, "moodle", exam, key=key)
        if ctx.dry_run:
            items.append(it)
            continue
        r = ctx.browser.ctx.request.get(u + "?forcedownload=1")
        if not r.ok:
            ctx.report.problem(course.slug, f"Moodle {name}: HTTP {r.status}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.body())
        ctx.ledger.record(course.slug, "moodle", u, dest, week)
        items.append(it)
        time.sleep(0.5)  # one file at a time, politely
    return items


def sync_moodle_zoom(ctx: Ctx, course, src: dict) -> list[Item]:
    base = src.get("base", "https://moodle-app2.let.ethz.ch")
    r = ctx.browser.ctx.request.get(f"{base}/course/view.php?id={src['id']}")
    if not r.ok:
        ctx.report.problem(course.slug, f"Moodle page {src['id']}: HTTP {r.status}")
        return []
    items = []
    for row in table_zoom_rows(r.text(), course.semester_start.year):
        week = semester_week(row["date"], course.semester_start)
        it = fetch_recording(ctx, course, row["url"], week, tutorial=row["tutorial"], title=row["title"], day=row["date"])
        if it:
            items.append(it)
    return items
