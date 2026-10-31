"""One sync run over all (or some) courses: sources without login first, then those behind the login."""

from __future__ import annotations

import datetime as dt
import json
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from coursesync.config import Config, ConfigError, load_secrets
from coursesync.ledger import Ledger
from coursesync.sources.common import Ctx, Item
from coursesync.sources.collections import sync_amiv, sync_community
from coursesync.sources.eth import sync_moodle, sync_moodle_zoom, sync_tobira
from coursesync.sources.gitrepo import sync_git
from coursesync.sources.web import sync_ethz_web, sync_web
from coursesync.sources.zoom import sync_zoom_readme
from coursesync.util import notify

NO_LOGIN = {"git": sync_git, "zoom_readme": sync_zoom_readme, "web": sync_web}
LOGIN = {"tobira": sync_tobira, "moodle": sync_moodle, "moodle_zoom": sync_moodle_zoom, "ethz_web": sync_ethz_web,
         "vis": sync_community, "amiv": sync_amiv}
LOGIN_SITE = {"tobira": "video portal", "moodle": "Moodle", "moodle_zoom": "Moodle", "ethz_web": "ETH website",
              "vis": "exam collection", "amiv": "AMIV"}


@dataclass
class Manifest:
    started: str
    items: list[Item] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    logins_needed: list[str] = field(default_factory=list)
    path: str | None = None

    def problem(self, course: str, msg: str) -> None:
        self.problems.append(f"{course}: {msg}")
        print(f"  ! {course}: {msg}", flush=True)

    def to_dict(self) -> dict:
        return {"started": self.started, "items": [i.to_dict() for i in self.items], "problems": self.problems,
                "logins_needed": self.logins_needed}

    def markdown(self) -> str:
        lines = [f"# coursesync {self.started[:16].replace('T', ' ')}", ""]
        by: dict[str, list[Item]] = {}
        for it in self.items:
            by.setdefault(it.course, []).append(it)
        for c in sorted(by):
            lines.append(f"**{c}**")
            for it in by[c]:
                what = ("tutorial recording" if it.tutorial else "recording") if it.kind == "recording" else \
                       ("past exam" if it.exam else it.source)
                wk = f"week {it.week}, " if it.week else ""
                lines.append(f"- {what}: {wk}{it.title or it.path.rsplit('/', 1)[-1]}")
            lines.append("")
        if not self.items:
            lines += ["Nothing new.", ""]
        if self.logins_needed:
            lines += [f"**Login needed** for {', '.join(self.logins_needed)}: run `coursesync run` and log in in the "
                      "window that opens.", ""]
        if self.problems:
            lines += ["**Problems**", *[f"- {p}" for p in self.problems], ""]
        return "\n".join(lines)


def run(cfg: Config, *, only: list[str] | None = None, browser: bool = True, interactive: bool = True,
        dry_run: bool = False, login_timeout_min: int = 15) -> Manifest:
    man = Manifest(dt.datetime.now().isoformat(timespec="seconds"))
    ledger = Ledger(cfg.state_dir / "ledger.db")
    courses = [cfg.course(s) for s in only] if only else list(cfg.courses.values())
    ctx = Ctx(cfg, ledger, man, dry_run=dry_run)

    def each(kinds: dict):
        for c in courses:
            for src in c.sources:
                fn = kinds.get(src.get("type"))
                if fn:
                    try:
                        man.items += fn(ctx, c, src)
                    except Exception as e:  # one broken source never stops the run
                        man.problem(c.slug, f"{src.get('type')}: {e}")

    print("Sources without login…", flush=True)
    each(NO_LOGIN)
    login_sources = [(c, s) for c in courses for s in c.sources if s.get("type") in LOGIN]
    LOGIN_SITE_DYNAMIC: dict = {}
    if browser and login_sources:
        from coursesync.browser import Browser, LoginRejected
        print("Sources behind your login…", flush=True)
        b = Browser(cfg.state_dir).start(headless=True)
        ctx.browser = b
        try:
            checks, pages = {}, {}
            for c, s in login_sources:
                site = LOGIN_SITE[s["type"]]
                if s["type"] == "tobira":
                    base = s.get("base", "https://video.ethz.ch")
                    checks.setdefault(site, lambda base=base, p=s["path"]: b.tobira_ok(base, p))
                    pages.setdefault(site, s.get("login", f"{base}/~session"))  # the portal's own Login button
                elif s["type"] == "ethz_web":
                    checks.setdefault(site, lambda url=s["check"]: b.file_ok(url))
                    pages.setdefault(site, s.get("login", s["check"]))  # redirects to the login page
                elif s["type"] == "vis":
                    base = s.get("site", "https://exams.vis.ethz.ch").rstrip("/")
                    site = f"exam collection ({base.split('//')[1]})"
                    LOGIN_SITE_DYNAMIC[(c.slug, id(s))] = site
                    checks.setdefault(site, lambda base=base: b.community_ok(base))
                    pages.setdefault(site, f"{base}/login")
                elif s["type"] == "amiv":
                    checks.setdefault(site, b.amiv_ok)
                    pages.setdefault(site, "https://amiv.ethz.ch/en/studies/documents")
                else:
                    base = s.get("base", "https://moodle-app2.let.ethz.ch")
                    checks.setdefault(site, lambda base=base: b.moodle_ok(base))
                    pages.setdefault(site, s.get("login", f"{base}/auth/shibboleth/index.php"))
            if cfg.auto_login and (expired := [k for k, ok in checks.items() if not ok()]):
                try:
                    username, password = load_secrets()
                    for site in expired:
                        b.auto_login(pages[site], username, password)
                except Exception as e:  # only the type: never risk a password in a report
                    msg = str(e) if isinstance(e, (ConfigError, LoginRejected)) else f"{type(e).__name__} on the login page"
                    man.problem("login", f"automatic login failed: {msg}")
            if interactive:
                def opened(missing):
                    msg = f"Log in to {', '.join(missing)} in the browser window that opened (tick 'keep me signed in')."
                    print(f"\n>>> {msg} The sync continues by itself afterwards.\n", flush=True)
                    if cfg.notify:
                        notify("coursesync: login needed", msg)
                missing = b.wait_for_logins(checks, pages, login_timeout_min, opened)
            else:
                missing = [k for k, ok in checks.items() if not ok()]
            man.logins_needed = missing
            ready = {k for k in checks if k not in missing}
            for c in courses:  # per source, so one site being logged out doesn't block the others
                for s in c.sources:
                    t = s.get("type")
                    if t in LOGIN and LOGIN_SITE_DYNAMIC.get((c.slug, id(s)), LOGIN_SITE[t]) in ready:
                        try:
                            man.items += LOGIN[t](ctx, c, s)
                        except Exception as e:
                            man.problem(c.slug, f"{t}: {e}")
        finally:
            b.close()
    if ctx.transcribing and not dry_run:
        from coursesync.transcribe import transcribe
        new = {it.path: it for it in man.items}
        for course, path, keep_video in ledger.transcripts_due():  # also what an earlier run didn't finish
            video = Path(path)
            if not video.exists():  # removed by hand: nothing left to transcribe
                ledger.transcript_done(path)
                continue
            print(f"  transcribing {path}…", flush=True)
            try:
                out = transcribe(video, cfg.transcribe.get("model", "large-v3-turbo"), cfg.transcribe.get("language"),
                                 cfg.transcribe.get("batch_size", 8))
            except Exception as e:  # the video stays until its transcript exists
                man.problem(course, f"transcription of {path} (retried next run): {e}")
                continue
            if not keep_video:
                video.unlink()
            ledger.transcript_done(path)
            if it := new.get(path):
                it.transcript = str(out)
                it.path = path if keep_video else str(out.with_suffix(".md"))
    # manifest + hook + notification
    mdir = cfg.state_dir / "runs"
    mdir.mkdir(parents=True, exist_ok=True)
    p = mdir / f"{man.started.replace(':', '')}.json"
    p.write_text(json.dumps(man.to_dict(), indent=1))
    (mdir / "last.json").write_text(p.read_text())
    (mdir / "last.md").write_text(man.markdown())
    man.path = str(p)
    if cfg.on_new and man.items and not dry_run:
        print("Running on_new hook…", flush=True)
        r = subprocess.run(shlex.split(cfg.on_new.replace("{manifest}", shlex.quote(str(p)))) if "{manifest}" in cfg.on_new
                           else shlex.split(cfg.on_new) + [str(p)], text=True)
        if r.returncode != 0:
            man.problems.append(f"on_new hook exited with {r.returncode}")
    if cfg.notify and not dry_run and (man.items or man.logins_needed):
        courses_new = sorted({i.course for i in man.items})
        notify("coursesync", (f"{len(man.items)} new: {', '.join(courses_new)}" if man.items else "nothing new")
               + (f" · login needed: {', '.join(man.logins_needed)}" if man.logins_needed else ""))
    return man
