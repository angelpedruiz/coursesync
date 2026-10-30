"""Student exam collections: Community Solutions (exams.vis.ethz.ch, exams.vmp.ethz.ch …) and AMIV study documents."""

from __future__ import annotations

import time
from pathlib import Path

from coursesync.sources.common import Ctx, Item
from coursesync.util import download, safe_name

AMIV_API = "https://api.amiv.ethz.ch"
SKIP_EXT = {"aux", "log", "synctex", "gz", "out", "toc", "fls", "fdb_latexmk", "bbl", "blg", "ds_store"}


# ---------------------------------------------------------------- Community Solutions (VIS / VMP)

def _answers_markdown(ctx: Ctx, site: str, exam: dict, top: int) -> str:
    req = ctx.browser.ctx.request
    cuts = req.get(f"{site}/api/exam/cuts/{exam['filename']}/").json().get("value", {})
    lines = [f"# {exam['category_displayname']} {exam['displayname']}: community answers", "",
             f"From {site} (Community Solutions). Answers are by students, sorted by votes: check them, they can be wrong.", ""]
    q = 0
    for page in sorted(cuts, key=int):
        for cut in cuts[page]:
            q += 1
            sec = req.get(f"{site}/api/exam/answersection/{cut['oid']}/").json().get("value", {})
            answers = sorted(sec.get("answers", []), key=lambda a: (-(a.get("expertvotes") or 0), -(a.get("upvotes") or 0)))
            title = cut.get("name") or f"question {q}"
            lines.append(f"## Page {page}, {title}")
            if not answers:
                lines += ["(no answer yet)", ""]
                continue
            for a in answers[:top]:
                votes = f"{a.get('upvotes', 0)} votes" + (f", {a['expertvotes']} expert" if a.get("expertvotes") else "")
                lines += [f"**Answer ({votes})**", "", (a.get("text") or "").strip(), ""]
            time.sleep(0.05)
    return "\n".join(lines)


def sync_community(ctx: Ctx, course, src: dict) -> list[Item]:
    """{type: vis, site: https://exams.vis.ethz.ch, category: <slug>, exclude: [HS24, …] (PDFs you have), answers: 2}"""
    site = src.get("site", "https://exams.vis.ethz.ch").rstrip("/")
    req = ctx.browser.ctx.request
    r = req.get(f"{site}/api/category/listexams/{src['category']}/")
    if not r.ok:
        ctx.report.problem(course.slug, f"{site} {src['category']}: HTTP {r.status}")
        return []
    folder = course.folder / src.get("subdir", "Exams") / site.split("//")[1].split(".")[1].upper()
    exclude = set(src.get("exclude", []))
    items = []
    for ex in r.json().get("value", []):
        if not ex.get("canView", True):
            continue
        name = safe_name(f"{ex['displayname']} {ex.get('remark') or ''}".strip() + f" ({ex['filename'][:-4]})")
        files = [] if ex["displayname"] in exclude else [("exam", folder / f"{name}.pdf")]
        if ex.get("has_solution") and ex["displayname"] not in exclude:
            files.append(("solution", folder / f"{name} official solution.pdf"))
        for kind, dest in files:
            url = f"{site}/api/exam/pdf/{kind}/{ex['filename']}/"
            if ctx.ledger.has(course.slug, url) or dest.exists():
                continue
            if not ctx.dry_run:
                try:
                    download(req.get(url).json()["value"], dest)
                except Exception as e:
                    ctx.report.problem(course.slug, f"{name} {kind}: {e}")
                    continue
                ctx.ledger.record(course.slug, "collection", url, dest)
            items.append(Item(course.slug, "file", str(dest), url, None, f"{ex['displayname']} {kind}", "collection", exam=True))
        top = int(src.get("answers", 2))
        # answers grow over time: refetch when the number of answered questions changed
        akey = f"{site}/exam/{ex['filename']}#answers={ex.get('count_answered')}"
        dest = folder / f"{name} community answers.md"
        if top and ex.get("count_answered") and not ctx.ledger.has(course.slug, akey):
            if not ctx.dry_run:
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(_answers_markdown(ctx, site, ex, top))
                except Exception as e:
                    ctx.report.problem(course.slug, f"{name} answers: {e}")
                    continue
                ctx.ledger.record(course.slug, "collection", akey, dest)
            items.append(Item(course.slug, "file", str(dest), akey, None, f"{ex['displayname']} community answers",
                              "collection", exam=True, extra={"answers": True}))
    return items


# ---------------------------------------------------------------- AMIV study documents

def sync_amiv(ctx: Ctx, course, src: dict) -> list[Item]:
    """{type: amiv, lecture: '<regex on the lecture name>', types: [exams, cheat sheets, …]}"""
    import json
    import urllib.parse
    token = ctx.browser.local_storage("https://amiv.ethz.ch", "auth:token")
    if not token:
        ctx.report.problem(course.slug, "AMIV: not logged in")
        return []
    where = {"lecture": {"$regex": "(?i)" + src["lecture"]}}
    if src.get("types"):
        where["type"] = {"$in": src["types"]}
    r = ctx.browser.ctx.request.get(f"{AMIV_API}/studydocuments?max_results=200&where=" + urllib.parse.quote(json.dumps(where)),
                                    headers={"Authorization": token})
    if not r.ok:
        ctx.report.problem(course.slug, f"AMIV: HTTP {r.status}")
        return []
    items = []
    for doc in r.json().get("_items", []):
        exam = doc.get("type") == "exams"
        sub = Path(src.get("subdir", "AMIV")) / ("past_exams" if exam else safe_name(doc.get("type") or "documents"))
        for f in doc.get("files", []):
            if f["name"].rsplit(".", 1)[-1].lower() in SKIP_EXT:
                continue  # LaTeX/editor leftovers some uploads contain
            url = AMIV_API + f["file"]
            dest = course.folder / sub / safe_name(f"{doc.get('course_year') or ''} {f['name']}".strip())
            if ctx.ledger.has(course.slug, url) or dest.exists():
                continue
            if not ctx.dry_run:
                try:
                    download(url, dest, headers={"Authorization": token})
                except OSError as e:
                    ctx.report.problem(course.slug, f"AMIV {f['name']}: {e}")
                    continue
                ctx.ledger.record(course.slug, "collection", url, dest)
            items.append(Item(course.slug, "file", str(dest), url, None, f"{doc.get('title')} ({doc.get('type')})",
                              "collection", exam=exam))
            time.sleep(0.3)
    return items
