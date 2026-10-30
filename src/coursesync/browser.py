"""A Chrome session (Playwright) with saved cookies for sites behind your university login.

The cookies live in <state_dir>/browser/state.json (only readable by you). When the login has expired, a
normal Chrome window opens on the real login page; you log in there and the run continues. Your password
is never seen, stored or typed by coursesync.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

TOBIRA_QUERY = ("query($p:String!){ realmByPath(path:$p){ blocks { ... on SeriesBlock { series { entries { __typename "
                "... on AuthorizedEvent { title syncedData { startTime } authorizedData { tracks { uri mimetype resolution } "
                "captions { uri } } } } } } } } }")


class Browser:
    def __init__(self, state_dir: Path, channel: str = "chrome"):
        self.state = state_dir / "browser" / "state.json"
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.channel = channel
        self.pw = self.browser = self.ctx = None

    def start(self, headless: bool = True) -> "Browser":
        from playwright.sync_api import sync_playwright
        if not self.pw:
            self.pw = sync_playwright().start()
        if self.browser:
            self.save()
            self.browser.close()
        try:
            self.browser = self.pw.chromium.launch(channel=self.channel, headless=headless)
        except Exception:  # no Chrome installed: Playwright's own Chromium (`playwright install chromium`)
            self.browser = self.pw.chromium.launch(headless=headless)
        self.ctx = self.browser.new_context(storage_state=str(self.state) if self.state.exists() else None)
        return self

    def save(self) -> None:
        if self.ctx:
            self.ctx.storage_state(path=str(self.state))
            os.chmod(self.state, 0o600)

    def close(self) -> None:
        try:
            self.save()
            if self.browser:
                self.browser.close()
        finally:
            if self.pw:
                self.pw.stop()

    # ---------------------------------------------------------------- site checks
    def tobira(self, base: str, path: str) -> dict:
        r = self.ctx.request.post(f"{base}/graphql", data=json.dumps({"query": TOBIRA_QUERY, "variables": {"p": path}}),
                                  headers={"Content-Type": "application/json"})
        return r.json() if r.ok else {}

    def tobira_ok(self, base: str, path: str) -> bool:
        try:
            j = self.tobira(base, path)
        except Exception:
            return False
        entries = [e for b in ((j.get("data") or {}).get("realmByPath") or {}).get("blocks") or []
                   for e in (b.get("series") or {}).get("entries") or []]
        return any(e.get("__typename") == "AuthorizedEvent" for e in entries)

    def moodle_ok(self, base: str) -> bool:
        try:
            r = self.ctx.request.get(f"{base}/my/", max_redirects=0)
        except Exception:
            return False
        return r.status == 200

    def local_storage(self, origin: str, key: str) -> str | None:
        """A value the site keeps in its local storage (e.g. a login token), from the saved session."""
        for o in self.ctx.storage_state().get("origins", []):
            if o["origin"] == origin:
                for item in o.get("localStorage", []):
                    if item["name"] == key:
                        return item["value"].strip('"')
        return None

    def community_ok(self, site: str) -> bool:
        try:
            return bool(self.ctx.request.get(f"{site}/api/auth/me/").json().get("loggedin"))
        except Exception:
            return False

    def amiv_ok(self) -> bool:
        tok = self.local_storage("https://amiv.ethz.ch", "auth:token")
        if not tok:
            return False
        try:
            return self.ctx.request.get("https://api.amiv.ethz.ch/studydocuments?max_results=1",
                                        headers={"Authorization": tok}).ok
        except Exception:
            return False

    def wait_for_logins(self, checks: dict, login_pages: dict, timeout_min: int, on_open=None) -> list[str]:
        """Open a visible window with the login pages of the failing checks; return what is still logged out."""
        missing = [k for k, ok in checks.items() if not ok()]
        if not missing:
            return []
        self.start(headless=False)
        for i, name in enumerate(missing):
            self.ctx.new_page().goto(login_pages[name])
        if on_open:
            on_open(missing)
        deadline = time.time() + timeout_min * 60
        while missing and time.time() < deadline:
            time.sleep(4)
            missing = [k for k in missing if not checks[k]()]
        self.save()
        self.start(headless=True)
        return missing
