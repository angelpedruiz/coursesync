"""coursesync command line."""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer
import yaml

from coursesync.config import ConfigError, default_path, load, secrets_path

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Keep your course material in sync: recordings, Moodle, course websites, git repos.")
ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="Config file (default: ./coursesync.yaml or "
                                                "~/.config/coursesync/coursesync.yaml).")]


def _cfg(path):
    try:
        return load(path)
    except ConfigError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1)


@app.command()
def run(courses: Annotated[list[str] | None, typer.Argument(help="Only these courses (default: all).")] = None,
        config: ConfigOpt = None,
        no_window: Annotated[bool, typer.Option("--no-window", help="Never open a login window (for timers).")] = False,
        no_login: Annotated[bool, typer.Option("--no-login", help="Skip sources that need a login.")] = False,
        dry_run: Annotated[bool, typer.Option("--dry-run", help="Only list what's new.")] = False,
        as_json: Annotated[bool, typer.Option("--json", help="Print the manifest as JSON.")] = False):
    """Fetch everything new. If your login has expired, a browser window opens on the login page."""
    from coursesync.run import run as do_run
    man = do_run(_cfg(config), only=courses or None, browser=not no_login, interactive=not no_window, dry_run=dry_run)
    typer.echo(json.dumps(man.to_dict(), indent=1) if as_json else "\n" + man.markdown())


@app.command()
def init(path: Annotated[Path | None, typer.Argument(help="Where to write the config.")] = None):
    """Write an example config to edit."""
    path = path or default_path()
    if path.exists():
        typer.secho(f"{path} already exists", fg="yellow")
        raise typer.Exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((Path(__file__).parent / "example.yaml").read_text())
    typer.echo(f"wrote {path}: edit it, then `coursesync run --dry-run`")


@app.command()
def status(config: ConfigOpt = None):
    """What the last run brought in, and how much is tracked per course."""
    from coursesync.ledger import Ledger
    cfg = _cfg(config)
    last = cfg.state_dir / "runs" / "last.md"
    typer.echo(last.read_text() if last.exists() else "no run yet")
    led = Ledger(cfg.state_dir / "ledger.db")
    for r in led.con.execute("SELECT course, kind, count(*) FROM fetched GROUP BY course, kind ORDER BY course"):
        typer.echo(f"{r[0]:<12} {r[1]:<8} {r[2]:>4}")


@app.command()
def login(name: Annotated[str, typer.Argument(help="A name for this login, e.g. pai (→ ~/.config/coursesync/pai.netrc).")],
          host: Annotated[str, typer.Option(help="Website host, e.g. las.inf.ethz.ch")]):
    """Store a website's (course) password once, for `web` sources with `netrc:`. You type it; it stays on this computer."""
    f = Path.home() / ".config" / "coursesync" / f"{name}.netrc"
    user = typer.prompt("user")
    pw = getpass.getpass("password: ")
    f.parent.mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        f.write_text(f"machine {host}\nlogin {user}\npassword {pw}\n")
    finally:
        os.umask(old)
    f.chmod(0o600)
    typer.echo(f"saved to {f}; use `netrc: {f}` in the web source")


@app.command()
def autologin():
    """Store your ETH login for unattended runs (`auto_login: true`). You type it; it stays on this computer."""
    f = secrets_path()
    user = typer.prompt("ETH username")
    pw = getpass.getpass("ETH password: ")
    f.parent.mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        f.write_text(yaml.safe_dump({"username": user, "password": pw}))
    finally:
        os.umask(old)
    f.chmod(0o600)
    typer.echo(f"saved to {f}; set `auto_login: true` in your config to use it")


@app.command()
def renumber(config: ConfigOpt = None):
    """Give files already fetched the NN- prefix of their course's `layout: number` folders. Nothing is fetched again."""
    from coursesync.ledger import Ledger
    cfg = _cfg(config)
    led = Ledger(cfg.state_dir / "ledger.db")
    for course in cfg.courses.values():
        for old, new in led.renumber(course):
            typer.echo(f"{course.slug}: {old.name} → {new.name}")


@app.command("import")
def import_ledger(file: Annotated[Path, typer.Argument(help="JSON lines: {url, course, kind, path?, week?}")],
                  config: ConfigOpt = None):
    """Mark things you already have as fetched (e.g. when switching from another tool), so they aren't downloaded again."""
    from coursesync.ledger import Ledger
    cfg = _cfg(config)
    led = Ledger(cfg.state_dir / "ledger.db")
    n = 0
    for line in file.read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            led.record(d["course"], d["kind"], d["url"], Path(d["path"]) if d.get("path") else None, d.get("week"))
            n += 1
    typer.echo(f"imported {n} entries")


@app.command()
def timer(action: Annotated[str, typer.Argument(help="install | remove | status")] = "status",
          at: Annotated[str, typer.Option(help="Daily time (systemd OnCalendar), e.g. 19:00")] = "19:00",
          command: Annotated[str | None, typer.Option(help="Command to run (default: coursesync run --no-window).")] = None,
          config: ConfigOpt = None):
    """A daily background sync (systemd user timer, Linux). It never opens a window: it notifies you if a login is needed."""
    units = Path.home() / ".config" / "systemd" / "user"
    svc, tmr = units / "coursesync.service", units / "coursesync.timer"
    if action == "remove":
        subprocess.run(["systemctl", "--user", "disable", "--now", "coursesync.timer"], capture_output=True)
        svc.unlink(missing_ok=True)
        tmr.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"])
        typer.echo("removed")
        return
    if action == "install":
        cfg_path = (config or default_path()).resolve()
        exe = command or f"{sys.executable} -m coursesync.cli run --no-window --config {cfg_path}"
        units.mkdir(parents=True, exist_ok=True)
        svc.write_text(f"[Unit]\nDescription=coursesync: fetch new course material\nAfter=network-online.target\n\n"
                       f"[Service]\nType=oneshot\nEnvironment=PATH={os.environ.get('PATH', '/usr/bin:/bin')}\n"
                       f"ExecStart={exe}\n")
        tmr.write_text(f"[Unit]\nDescription=coursesync daily\n\n[Timer]\nOnCalendar=*-*-* {at}\nPersistent=true\n"
                       f"RandomizedDelaySec=10min\n\n[Install]\nWantedBy=timers.target\n")
        subprocess.run(["systemctl", "--user", "daemon-reload"])
        subprocess.run(["systemctl", "--user", "enable", "--now", "coursesync.timer"])
    subprocess.run(["systemctl", "--user", "list-timers", "coursesync.timer", "--no-pager"])


if __name__ == "__main__":
    app()
