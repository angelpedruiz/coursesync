import datetime as dt
import http.server
import threading
from pathlib import Path

import pytest

from coursesync.config import load
from coursesync.ledger import Ledger
from coursesync.run import run
from coursesync.sources.zoom import readme_links, table_zoom_rows
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
