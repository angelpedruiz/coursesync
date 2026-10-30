import datetime as dt
import http.server
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from coursesync.browser import Browser
from coursesync.config import ConfigError, load, load_secrets
from coursesync.ledger import Ledger
from coursesync.run import run
from coursesync.sources.common import Ctx
from coursesync.sources.eth import sync_moodle, sync_tobira
from coursesync.sources.web import sync_ethz_web, with_tables
from coursesync.sources.zoom import readme_links, table_zoom_rows
from coursesync.transcribe import from_captions, seconds, transcribe
from coursesync.util import label, parse_date, recording_tag, semester_week

START = dt.date(2026, 9, 14)


def test_dates_and_weeks():
    assert parse_date("Tue 20.10.2026", 2026) == dt.date(2026, 10, 20)
    assert parse_date("1 September 17th Course Introduction", 2026) == dt.date(2026, 9, 17)
    assert parse_date("6 October 22nd", 2026) == dt.date(2026, 10, 22)
    assert parse_date("Oct 22", 2026) == dt.date(2026, 10, 22)
    assert parse_date("2026-11-03", 2026) == dt.date(2026, 11, 3)
    assert parse_date("nothing", 2026) is None
    assert semester_week(dt.date(2026, 9, 17), START) == 1 and semester_week(dt.date(2026, 10, 29), START) == 7


def test_labels():
    rules = [{"match": r"^pai-0?(\d+)-", "week": "{1}", "name": "L{week:02}-slides"},
             {"match": r"tut\w*?0?(\d+)", "week": "{1}+1"}]
    assert label("pai-07-active-learning.pdf", rules) == (7, "L07-slides.pdf")
    assert label("AISE26Tut3.pdf", rules) == (4, None)
    assert label("syllabus.pdf", rules) == (None, None)
    with pytest.raises(ValueError):
        label("x1", [{"match": r"x(\d)", "week": "__import__('os')"}])


def test_recording_tags(tmp_path):
    rec = tmp_path / "Recordings"
    assert recording_tag(rec, 7) == "L07"
    rec.mkdir()
    (rec / "L07.mp4").touch()
    assert recording_tag(rec, 7) == "L07b"
    assert recording_tag(rec, 7, tutorial=True) == "L07-tut"
    (rec / "L07b.mp4").touch()
    assert recording_tag(rec, 7) == "L07c"


def test_recording_name_template(tmp_path):
    day = dt.date(2026, 10, 22)
    assert recording_tag(tmp_path, 7, day=day, template="{date}") == "2026-10-22"
    assert recording_tag(tmp_path, 7, tutorial=True, template="Lecture {week:02}") == "Lecture 07-tut"
    (tmp_path / "2026-10-22.mp4").touch()
    assert recording_tag(tmp_path, 7, day=day, template="{date}") == "2026-10-22b"


def test_course_layout(tmp_path):
    cfg_file = tmp_path / "coursesync.yaml"
    cfg_file.write_text(f"""root: {tmp_path}
semester_start: 2026-09-14
courses:
  plain: {{}}
  custom:
    semester_start: 2026-09-21
    layout:
      recordings: Lectures
      past_exams: Exams
      sort: [{{match: 'slides', to: Slides/Weekly}}]
""")
    cfg = load(cfg_file)
    plain, custom = cfg.course("plain"), cfg.course("custom")
    base = tmp_path / "custom" / "Moodle"
    assert plain.recordings == tmp_path / "plain" / "Recordings"
    assert plain.semester_start == dt.date(2026, 9, 14) and custom.semester_start == dt.date(2026, 9, 21)
    assert plain.file_folder(tmp_path / "plain" / "Moodle", "exam.pdf", True) == tmp_path / "plain" / "Moodle" / "past_exams"
    assert custom.recordings == tmp_path / "custom" / "Lectures"
    assert custom.file_folder(base, "exam.pdf", True) == tmp_path / "custom" / "Exams"
    assert custom.file_folder(base, "Slides-03.pdf", False) == tmp_path / "custom" / "Slides" / "Weekly"
    assert custom.file_folder(base, "exam-slides.pdf", True) == tmp_path / "custom" / "Slides" / "Weekly"  # a rule wins
    assert custom.file_folder(base, "notes.pdf", False) == base
    cfg_file.write_text(f"root: {tmp_path}\nsemester_start: 2026-09-14\ncourses:\n  c: {{layout: {{recording: X}}}}\n")
    with pytest.raises(ConfigError):
        load(cfg_file)


def test_numbered_folders_sort_in_course_order_and_never_refetch(tmp_path):
    cfg_file = tmp_path / "coursesync.yaml"
    cfg_file.write_text(f"root: {tmp_path}\nsemester_start: 2026-09-14\ncourses: {{c: {{layout: {{number: [slides]}}}}}}\n")
    course = load(cfg_file).course("c")
    slides, other = tmp_path / "c" / "slides", tmp_path / "c" / "other"
    slides.mkdir(parents=True)
    other.mkdir()
    led = Ledger(tmp_path / "l.db")
    for name in ("intro.pdf", "advanced.pdf"):
        (slides / name).touch()
        led.record("c", "moodle", f"https://x/{name}", slides / name)
    (other / "z.pdf").touch()
    led.record("c", "moodle", "https://x/z.pdf", other / "z.pdf")
    assert [new.name for _, new in led.renumber(course)] == ["01-intro.pdf", "02-advanced.pdf"]
    assert sorted(p.name for p in slides.iterdir()) == ["01-intro.pdf", "02-advanced.pdf"]
    assert led.renumber(course) == []  # already done
    assert led.has("c", "https://x/intro.pdf")
    assert led.con.execute("SELECT path FROM fetched WHERE url='https://x/intro.pdf'").fetchone()[0] == str(slides / "01-intro.pdf")
    assert course.numbered(slides / "new.pdf").name == "03-new.pdf"  # the next new file continues
    assert course.numbered(other / "new.pdf").name == "new.pdf"


def test_readme_and_table_parsing():
    md = """* [C++ Crash Course](https://ethz.zoom.us/rec/share/AAA)
  * [Week 03 (01.10.2026)](https://ethz.zoom.us/rec/share/BBB)
    [Introduction to classes for the Penna exercise](https://ethz.zoom.us/rec/share/CCC)
  * [Week 07 (29.10.2026)](https://ethz.zoom.us/rec/share/DDD)"""
    links = readme_links(md, START)
    assert [(l["url"][-3:], l["week"], l["extra_clip"]) for l in links] == [("BBB", 3, False), ("CCC", 3, True), ("DDD", 7, False)]
    page = """<h3>Lectures</h3><table><tr><th>#</th><th>Date</th></tr>
    <tr><td>6</td><td>October 22nd</td><td><a href="https://ethz.zoom.us/rec/share/X?startTime=1">Recording</a></td></tr></table>
    <h3>Tutorials</h3><table><tr><td>2</td><td>September 28th</td><td><a href="https://ethz.zoom.us/rec/share/Y">R</a></td></tr></table>"""
    rows = table_zoom_rows(page, 2026)
    assert [(r["date"], r["tutorial"]) for r in rows] == [(dt.date(2026, 10, 22), False), (dt.date(2026, 9, 28), True)]


@pytest.fixture
def zoom_course(tmp_path, monkeypatch):
    """A zoom_readme course with one recording, downloading and transcribing faked; returns a config writer."""
    repo = tmp_path / "recordings"
    repo.mkdir()
    (repo / "README.md").write_text("[Week 07 (29.10.2026)](https://ethz.zoom.us/rec/share/AAA)\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "-C", str(repo), "add", "."], check=True)
    subprocess.run([*git, "-C", str(repo), "commit", "-qm", "x"], check=True)

    def fake_download(url, dest):
        dest.write_bytes(b"video")
        return {}

    monkeypatch.setattr("coursesync.sources.zoom.zoom_download", fake_download)
    monkeypatch.setattr("coursesync.sources.zoom.zoom_transcript", lambda url: None)

    def write(extra: str, source_extra: str = "", enabled: bool = True):
        f = tmp_path / "coursesync.yaml"
        f.write_text(f"""root: {tmp_path / 'out'}
semester_start: 2026-09-14
state_dir: {tmp_path / 'state'}
notify: false
transcribe: {{enabled: {str(enabled).lower()}}}
courses:
  c1:
    folder: C1
    {extra}
    sources: [{{type: zoom_readme, repo: {repo}{source_extra}}}]
""")
        return load(f)
    return write


def fake_transcribe(path, model, language, batch_size):
    out = path.with_name(path.stem + ".transcript.json")
    out.write_text("{}")
    out.with_suffix(".md").write_text("# Transcript")
    return out


def test_transcript_only_removes_the_video(zoom_course, tmp_path, monkeypatch):
    monkeypatch.setattr("coursesync.transcribe.transcribe", fake_transcribe)
    man = run(zoom_course("videos: false"), browser=False)
    rec = tmp_path / "out" / "C1" / "Recordings"
    assert sorted(p.name for p in rec.iterdir()) == ["L07.transcript.json", "L07.transcript.md"]
    assert [Path(i.path).name for i in man.items] == ["L07.transcript.md"]
    assert run(zoom_course("videos: false"), browser=False).items == []  # not fetched again


def test_zoom_transcript_replaces_the_video(zoom_course, tmp_path, monkeypatch):
    monkeypatch.setattr("coursesync.sources.zoom.zoom_transcript",
                        lambda url: ([[6.26, 24.74, "Welcome"]], {"topic": "AISE"}))
    man = run(zoom_course("videos: false", enabled=False), browser=False)
    rec = tmp_path / "out" / "C1" / "Recordings"
    assert sorted(p.name for p in rec.iterdir()) == ["L07.transcript.json", "L07.transcript.md"]
    assert [Path(i.path).name for i in man.items] == ["L07.transcript.md"]
    assert run(zoom_course("videos: false", enabled=False), browser=False).items == []


def test_source_can_override_videos(zoom_course, tmp_path, monkeypatch):
    monkeypatch.setattr("coursesync.transcribe.transcribe", fake_transcribe)
    run(zoom_course("videos: false", ", videos: true"), browser=False)
    assert (tmp_path / "out" / "C1" / "Recordings" / "L07.mp4").exists()


def test_failed_transcription_keeps_the_video_and_retries(zoom_course, tmp_path, monkeypatch):
    def broken(*a):
        raise RuntimeError("no model")

    monkeypatch.setattr("coursesync.transcribe.transcribe", broken)
    man = run(zoom_course("videos: false"), browser=False)
    video = tmp_path / "out" / "C1" / "Recordings" / "L07.mp4"
    assert video.exists() and "retried next run" in man.problems[0]
    monkeypatch.setattr("coursesync.transcribe.transcribe", fake_transcribe)
    assert run(zoom_course("videos: false"), browser=False).problems == []
    assert not video.exists() and video.with_name("L07.transcript.md").exists()


def test_no_videos_and_no_transcripts_fetches_nothing_until_enabled(zoom_course, tmp_path, monkeypatch):
    monkeypatch.setattr("coursesync.transcribe.transcribe", fake_transcribe)
    assert run(zoom_course("videos: false", enabled=False), browser=False).items == []
    assert not (tmp_path / "out").exists()
    assert len(run(zoom_course("videos: false"), browser=False).items) == 1


class FakeResponse:
    def __init__(self, text="", location=None):
        self.ok, self.status, self._text = True, 200, text
        self.headers = {"location": location} if location else {}

    def text(self):
        return self._text


class FakeRequest:
    """A Moodle course whose two resources share one file name, plus a file in a folder (no title of its own)."""
    base = "https://moodle.test"

    def get(self, url, **kw):
        files = {"1": "content/1/02_handout.pdf", "2": "content/2/02_handout.pdf"}
        if url.endswith("/course/view.php?id=7"):
            return FakeResponse(f'<a href="{self.base}/pluginfile.php/9/mod_folder/content/0/01 [Tutorial] Games.pdf">x</a>')
        if url.endswith("/course/resources.php?id=7"):
            return FakeResponse("".join(f'<a href="{self.base}/mod/resource/view.php?id={i}" class="activityname">{t}</a>'
                                        for i, t in (("1", "Recitation 02"), ("2", "Exercise 02: Lösung"))))
        if "redirect=1" in url:
            i = url.split("id=")[1].split("&")[0]
            return FakeResponse(location=f"{self.base}/pluginfile.php/{i}/mod_resource/{files[i]}")
        return FakeResponse()


@pytest.mark.parametrize("names, expected", [
    (None, ["01 [Tutorial] Games.pdf", "02_handout.pdf", "02_handout.pdf"]),
    ("title", ["01-tutorial-games.pdf", "recitation-02.pdf", "exercise-02-losung.pdf"])])
def test_moodle_names(tmp_path, names, expected):
    cfg_file = tmp_path / "coursesync.yaml"
    cfg_file.write_text(f"root: {tmp_path}\nsemester_start: 2026-09-14\ncourses: {{c: {{}}}}\n")
    cfg = load(cfg_file)
    browser = SimpleNamespace(ctx=SimpleNamespace(request=FakeRequest()))
    ctx = Ctx(cfg, Ledger(tmp_path / "l.db"), None, browser=browser, dry_run=True)
    items = sync_moodle(ctx, cfg.course("c"), {"id": 7, "base": FakeRequest.base, "names": names})
    assert [Path(i.path).name for i in items] == expected


def test_transcribe_falls_back_to_cpu_when_the_gpu_fails_late(tmp_path, monkeypatch):
    def lazy_segments(device):
        if device == "cuda":
            raise RuntimeError("Library libcublas.so.12 is not found or cannot be loaded")
        yield SimpleNamespace(start=0.0, end=1.0, text=" hello ")

    class FakeModel:
        def __init__(self, model, device, compute_type):
            self.model, self.device = model, device

        def transcribe(self, audio, **kw):  # like the real one: loads fine, fails while iterating
            return lazy_segments(self.device), SimpleNamespace(duration=1.0, language="en")

    class FakeBatched:
        def __init__(self, model):
            self.model = model

        def transcribe(self, audio, batch_size, **kw):
            return self.model.transcribe(audio, **kw)

    monkeypatch.setitem(sys.modules, "faster_whisper",
                        SimpleNamespace(WhisperModel=FakeModel, BatchedInferencePipeline=FakeBatched))
    monkeypatch.setattr("coursesync.transcribe._audio", lambda path: [])
    monkeypatch.setattr("coursesync.transcribe._load_cuda_libs", lambda: None)
    out = transcribe(tmp_path / "L01.mp4", "large-v3-turbo", "en")
    saved = json.loads(out.read_text())
    assert (saved["device"], saved["model"], saved["segments"]) == ("cpu", "medium", [[0.0, 1.0, "hello"]])


def test_with_tables_adds_cells_of_json_tables(monkeypatch):
    table = {"thead": {"rows": [{"cells": [{"content": "Topic"}]}]},
             "tbody": {"rows": [{"cells": [{"content": '<a href="/files/lecture-01.pdf">Slides</a>'}]}]}}
    monkeypatch.setattr("coursesync.sources.web.get_text", lambda url: json.dumps(table))
    page = with_tables('<div data-url="/x/par/table.tableComp.json"></div>', "https://example.test/x.html")
    assert 'href="/files/lecture-01.pdf"' in page
    assert with_tables("<p>no tables</p>", "https://example.test/x.html") == "<p>no tables</p>"


def test_ethz_web_refuses_a_login_redirect(tmp_path, monkeypatch):
    monkeypatch.setattr("coursesync.sources.web.get_text", lambda url: '<a href="https://ethz.ch/a.pdf">a</a> '
                                                                       '<a href="https://ethz.ch/b.pdf">b</a>')
    responses = {"https://ethz.ch/a.pdf": SimpleNamespace(status=200, body=lambda: b"%PDF"),
                 "https://ethz.ch/b.pdf": SimpleNamespace(status=302, body=lambda: b"<html>login</html>")}
    request = SimpleNamespace(get=lambda url, **kw: responses[url])
    cfg_file = tmp_path / "coursesync.yaml"
    cfg_file.write_text(f"root: {tmp_path}\nsemester_start: 2026-09-14\ncourses: {{c: {{}}}}\n")
    cfg = load(cfg_file)
    problems = []
    report = SimpleNamespace(problem=lambda course, msg: problems.append(msg))
    ctx = Ctx(cfg, Ledger(tmp_path / "l.db"), report, browser=SimpleNamespace(ctx=SimpleNamespace(request=request)))
    items = sync_ethz_web(ctx, cfg.course("c"), {"url": "https://ethz.ch/page.html", "subdir": "files"})
    assert [Path(i.path).name for i in items] == ["a.pdf"]
    assert (tmp_path / "c" / "files" / "a.pdf").read_bytes() == b"%PDF"
    assert problems == ["b.pdf: HTTP 302"] and not (tmp_path / "c" / "files" / "b.pdf").exists()


def test_timestamps():
    assert seconds("00:00:06.262") == 6.26 and seconds("01:02:03,500") == 3723.5 and seconds("00:00:00.-304") == 0.3


def test_from_captions_makes_the_transcript(tmp_path):
    (tmp_path / "L01.vtt").write_text("WEBVTT\n\nNOTE made by a machine\n\n00:00:06.396 --> 00:00:10.716 align:start\n"
                                      "Welcome to the <b>class</b>.\nMy name is\n\n1\n01:00:00.000 --> 01:00:01.500\nBye\n")
    out = from_captions(tmp_path / "L01.mp4")
    assert json.loads(out.read_text())["segments"] == [[6.4, 10.72, "Welcome to the class. My name is"], [3600.0, 3601.5, "Bye"]]
    assert "[00:00:06] Welcome to the class." in out.with_suffix(".md").read_text()


@pytest.mark.parametrize("captions, videos, enabled, got", [
    (True, False, True, ["L01.transcript.json", "L01.transcript.md", "L01.vtt"]),  # no video, no Whisper
    (True, False, False, ["L01.transcript.json", "L01.transcript.md", "L01.vtt"]),
    (False, False, False, []),
    (True, True, False, ["L01.mp4", "L01.vtt"])])
def test_tobira_captions_replace_the_video(tmp_path, monkeypatch, captions, videos, enabled, got):
    def fake_download(url, dest, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n" if dest.suffix == ".vtt" else "video")

    monkeypatch.setattr("coursesync.sources.eth.download", fake_download)
    event = {"__typename": "AuthorizedEvent", "title": "x", "syncedData": {"startTime": "2026-09-16T10:00:00"},
             "authorizedData": {"tracks": [{"uri": "https://v.test/presentation-720.mp4", "mimetype": "video/mp4",
                                            "resolution": [1280, 720]}],
                                "captions": [{"uri": "https://v.test/c.vtt"}] if captions else []}}
    tree = {"data": {"realmByPath": {"blocks": [{"series": {"entries": [event]}}]}}}
    cfg_file = tmp_path / "coursesync.yaml"
    cfg_file.write_text(f"root: {tmp_path}\nsemester_start: 2026-09-14\ntranscribe: {{enabled: {str(enabled).lower()}}}\n"
                        f"courses: {{c: {{videos: {str(videos).lower()}}}}}\n")
    cfg = load(cfg_file)
    ctx = Ctx(cfg, Ledger(tmp_path / "l.db"), None, browser=SimpleNamespace(tobira=lambda base, path: tree))
    items = sync_tobira(ctx, cfg.course("c"), {"path": "/x"})
    rec = tmp_path / "c" / "Recordings"
    assert sorted(p.name for p in rec.iterdir() if p.name != ".") == got if got else not rec.exists()
    assert len(items) == (1 if got else 0)
    assert sync_tobira(ctx, cfg.course("c"), {"path": "/x"}) == []  # not fetched again


class FakePage:
    def __init__(self, url):
        self.url, self.filled, self.closed = url, [], False

    def goto(self, url, **kw):
        pass

    def fill(self, selector, value):
        self.filled.append(value)

    def click(self, selector):
        pass

    def wait_for_load_state(self, state):
        pass

    def close(self):
        self.closed = True


@pytest.mark.parametrize("url, typed", [("https://access.ethz.ch/idpauthapp/", ["me", "pw"]),
                                        ("https://access.ethz.ch.evil.test/idpauthapp/", []),
                                        ("https://moodle-app2.let.ethz.ch/my/", [])])
def test_auto_login_types_only_on_the_eth_login_page(tmp_path, url, typed):
    page = FakePage(url)
    b = Browser(tmp_path)
    b.ctx = SimpleNamespace(new_page=lambda: page)
    b.auto_login("https://video.ethz.ch/~session", "me", "pw")
    assert page.filled == typed and page.closed


def test_secrets_must_be_private(tmp_path):
    f = tmp_path / "secrets.yaml"
    f.write_text("username: me\npassword: pw\n")
    f.chmod(0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        load_secrets(f)
    f.chmod(0o600)
    assert load_secrets(f) == ("me", "pw")
    with pytest.raises(ConfigError, match="autologin"):
        load_secrets(tmp_path / "missing.yaml")


def test_ledger_ignores_zoom_starttime(tmp_path):
    led = Ledger(tmp_path / "l.db")
    led.record("c", "zoom", "https://ethz.zoom.us/rec/share/X", None)
    assert led.has("c", "https://ethz.zoom.us/rec/share/X?startTime=123")
    assert not led.has("c", "https://ethz.zoom.us/rec/share/Y")


def test_web_source_end_to_end(tmp_path):
    site = tmp_path / "site"
    (site / "exam").mkdir(parents=True)
    (site / "index.html").write_text('<a href="slides-01.pdf">1</a> <a href="exam/2024.pdf">e</a> <a href="x.html">x</a>')
    (site / "slides-01.pdf").write_bytes(b"%PDF-1.4 one")
    (site / "exam" / "2024.pdf").write_bytes(b"%PDF-1.4 exam")
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(site), **k)  # noqa: E731
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cfg_file = tmp_path / "coursesync.yaml"
        cfg_file.write_text(f"""root: {tmp_path / 'out'}
semester_start: 2026-09-14
state_dir: {tmp_path / 'state'}
notify: false
courses:
  c1:
    folder: C1
    labels: [{{match: 'slides-0?(\\d+)', week: '{{1}}', name: 'L{{week:02}}-slides'}}]
    sources: [{{type: web, url: 'http://127.0.0.1:{srv.server_port}/index.html'}}]
""")
        cfg = load(cfg_file)
        man = run(cfg, browser=False)
        got = {Path(i.path).relative_to(tmp_path / "out"): (i.week, i.exam, i.key) for i in man.items}
        assert got == {Path("C1/Website/slides-01.pdf"): (1, False, "L01-slides.pdf"),
                       Path("C1/Website/past_exams/2024.pdf"): (None, True, None)}
        assert run(cfg, browser=False).items == []  # idempotent: nothing new the second time
        assert (cfg.state_dir / "runs" / "last.md").read_text().count("Nothing new") == 1
    finally:
        srv.shutdown()
