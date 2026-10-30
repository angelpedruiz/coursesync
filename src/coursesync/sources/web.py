"""A course website: every linked file matching a pattern (public, or with a login stored in a netrc file)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin

from coursesync.sources.common import EXAM_RE, Ctx, Item
from coursesync.util import download, free_path, get_text, label, safe_name, url_name


def with_tables(page: str, url: str) -> str:
    """ETH's website loads its tables separately as JSON: add their cells to the page."""
    for path in set(re.findall(r'"([^"]+\.tableComp\.json)"', page)):
        table = json.loads(get_text(urljoin(url, path)))
        page += " ".join(c.get("content", "") for part in ("thead", "tbody")
                         for row in (table.get(part) or {}).get("rows", []) for c in row.get("cells", []))
    return page


def sync_web(ctx: Ctx, course, src: dict, fetch=None) -> list[Item]:
    netrc = Path(src["netrc"]).expanduser() if src.get("netrc") else None
    if netrc and not netrc.exists():
        ctx.report.problem(course.slug, f"{src['url']}: no stored login at {netrc} (run `coursesync login {netrc.stem}`)")
        return []
    try:
        page = with_tables(get_text(src["url"]), src["url"])  # the listing is usually public even when files are not
    except OSError as e:
        ctx.report.problem(course.slug, f"{src['url']}: {e}")
        return []
    pattern = re.compile(src.get("match", r"\.pdf$"), re.I)
    urls = list(dict.fromkeys(urljoin(src["url"], h) for h in re.findall(r'href="([^"#]+)"', page)  # keep the page's order
                              if pattern.search(h.split("?")[0])))
    folder = course.folder / src.get("subdir", "Website")
    have = {p.name for p in folder.rglob("*") if p.is_file()} if folder.exists() else set()
    items = []
    for u in urls:
        name = safe_name(url_name(u))
        exam = bool(EXAM_RE.search(u))
        if exam and src.get("exam_prefix_from"):  # e.g. pai-f23_ so same-named exams of other years don't collide
            m = re.search(src["exam_prefix_from"], u)
            name = f"{m[0]}_{name}" if m else name
        if ctx.ledger.has(course.slug, u) or name in have:
            continue
        dest = free_path(course.numbered(course.file_folder(folder, name, exam) / name))  # a sort rule can leave `have`
        week, key = label(name, course.labels)
        if ctx.dry_run:
            items.append(Item(course.slug, "file", str(dest), u, week, name, "web", exam, key=key))
            continue
        try:
            fetch(u, dest) if fetch else download(u, dest, netrc=netrc)
        except OSError as e:
            ctx.report.problem(course.slug, f"{name}: {e}")
            continue
        ctx.ledger.record(course.slug, "web", u, dest, week)
        items.append(Item(course.slug, "file", str(dest), u, week, name, "web", exam, key=key))
        time.sleep(0.3)
    return items


def sync_ethz_web(ctx: Ctx, course, src: dict) -> list[Item]:
    """A public page on ethz.ch whose files need your ETH login: downloaded in the logged-in browser session."""
    def fetch(url: str, dest: Path) -> None:
        r = ctx.browser.ctx.request.get(url, max_redirects=0)
        if r.status != 200:
            raise OSError(f"HTTP {r.status}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.body())

    return sync_web(ctx, course, src, fetch)
