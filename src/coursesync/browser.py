"""A Chrome session (Playwright) with saved cookies for sites behind your university login.

The cookies live in <state_dir>/browser/state.json (only readable by you). When the login has expired, a
normal Chrome window opens on the real login page; you log in there and the run continues. Your password
is never seen, stored or typed by coursesync, unless you opt in with `auto_login: true` and `coursesync autologin`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

WAYF_HOST = "wayf.switch.ch"
ETH_IDP = "https://aai-logon.ethz.ch/idp/shibboleth"
LOGIN_FORMS = {"access.ethz.ch": ("/", "input[autocomplete=username]"),  # the only pages a password is typed on
               "ethz.ch": ("/login/", "input[name=j_username]")}         # (host: path, username field)


class LoginRejected(Exception):
    """Raised at once, so a wrong stored password is never tried again on another site and locks the account."""


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

    def file_ok(self, url: str) -> bool:
        """A login-protected file answers directly instead of redirecting to the login page."""
        try:
            return self.ctx.request.get(url, max_redirects=0).status == 200
        except Exception:
            return False

    def moodle_ok(self, base: str) -> bool:
        try:
            r = self.ctx.request.get(f"{base}/my/", max_redirects=0)
        except Exception:
            return False
        return r.status == 200

    def auto_login(self, url: str, username: str, password: str) -> None:
        """Log in with stored credentials: typed only on ETH's own login forms, never on any other page."""
        page = self.ctx.new_page()
        try:
            page.goto(url, wait_until="networkidle")
            if urlsplit(page.url).hostname == WAYF_HOST:  # the university picker keeps its real list hidden
                page.eval_on_selector("#userIdPSelection", f"e => {{ e.value = '{ETH_IDP}' }}")
                page.eval_on_selector("#IdPList", "f => { const s = document.createElement('input'); s.type = 'hidden';"
                                                  " s.name = 'Select'; s.value = 'Select'; f.appendChild(s); f.submit() }")
                page.wait_for_url(lambda u: urlsplit(u).hostname != WAYF_HOST)
                page.wait_for_load_state("networkidle")  # single sign-on redirects pass through other hosts
            where = urlsplit(page.url)
            path, username_field = LOGIN_FORMS.get(where.hostname, ("", ""))
            if not path or not where.path.startswith(path) or not page.query_selector("input[type=password]"):
                return  # signed in, or a page we don't know or a consent page: not one to type a password into
            page.fill(username_field, username)
            page.fill("input[type=password]", password)
            page.press("input[type=password]", "Enter")
            page.wait_for_load_state("networkidle")
            after = urlsplit(page.url)
            try:  # a login that leads straight to a file (ethz.ch) keeps the login URL, so the form decides
                form_back = bool(page.query_selector("input[type=password]"))
            except Exception:
                form_back = False
            if after.hostname == where.hostname and after.path.startswith(where.path) and form_back:
                raise LoginRejected("ETH did not accept the stored login: run `coursesync autologin` again")
        finally:
            page.close()

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
