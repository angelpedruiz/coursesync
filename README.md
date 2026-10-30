# coursesync

Keep your university course material in sync, automatically: lecture recordings, Moodle files,
course websites and git repositories end up in one tidy folder per course, named by semester week,
optionally transcribed. Built for ETH Zürich (video.ethz.ch, Moodle, Zoom, GitLab), but every source
type is generic.

```
~/Documents/Uni/
  ML/Recordings/L07.mp4  L07.vtt  L07.transcript.md
  ML/Moodle/exercise07.pdf   ML/Moodle/past_exams/exam2024.pdf
  ML/Website/slides-07.pdf
  Sim/lecture/            ← the course git repo, pulled
```

## What it does
- **Only new things.** A ledger remembers everything fetched; nothing is downloaded twice (also when a
  Zoom link is re-shared with another `?startTime=`). Existing files are never overwritten.
- **Your login, never your password.** Sources behind the university login run in a Chrome session
  (Playwright) whose cookies are saved locally. When the login has expired, a normal browser window
  opens on the real login page; you log in, and the run continues by itself. coursesync never sees,
  stores or types your password.
- **Named by week.** Recordings become `L07.mp4` (`L07b` for a second session, `L07-tut` for a tutorial)
  from their date and your `semester_start`. Files get a week from optional label rules.
- **Past exams** (`exam|final|midterm|…` in the name) go to `past_exams/`.
- **Hands-off.** `coursesync timer install` runs it daily in the background (never opens a window; it
  notifies you when a login is needed).
- **Scriptable.** Every run writes a JSON manifest of what's new, and can call your own command with it
  (`on_new:`), e.g. to import into a notes or study tool.

## Sources
| `type` | What | Login |
|---|---|---|
| `tobira` | a video.ethz.ch (Tobira) series page: the slides+camera recording of each lecture, with captions | university |
| `moodle` | every file of a Moodle course: sections, labels, resources, folders | university |
| `moodle_zoom` | Zoom recording links in a Moodle page's tables (lecture and tutorial tables) | university |
| `web` | every file linked from a web page matching a pattern | none, or a stored course password (`coursesync login`) |
| `git` | a course repository, cloned and pulled | your SSH key |
| `vis` | past exams, official solutions and the community answers (as Markdown) from Community Solutions: exams.vis.ethz.ch, exams.vmp.ethz.ch | university |
| `amiv` | AMIV study documents for a lecture: past exams, summaries, cheat sheets | AMIV (ETH account) |
| `zoom_readme` | a repo whose README lists `[Week 03 (01.10.2026)](https://…zoom.us/rec/share/…)` | your SSH key; Zoom links are public |

## Install
```bash
uv tool install "git+https://github.com/moubipoch/coursesync"            # or: pipx install …
uv tool install "coursesync[transcribe] @ git+https://github.com/moubipoch/coursesync"   # with transcription
```
It uses your installed Google Chrome; without Chrome run `playwright install chromium` once.

## Use
```bash
coursesync init                 # writes ~/.config/coursesync/coursesync.yaml: edit it
coursesync run --dry-run        # what would be fetched
coursesync run                  # fetch (a login window opens if needed)
coursesync status               # last run + what's tracked
coursesync login ml --host example.ethz.ch   # store a course-website password once (netrc, 600)
coursesync timer install --at 19:00          # daily background sync (systemd user timer)
coursesync import have.jsonl    # mark things you already have, when switching from another setup
```
See [`src/coursesync/example.yaml`](src/coursesync/example.yaml) for every option.

## Good citizenship
Downloads go one at a time with pauses; it fetches only what you can already access with your own
account, for your own studying. Respect your university's and lecturers' rules about recordings, and
don't redistribute course material. If a site shows a bot check, solve it in the window yourself:
coursesync doesn't try to get around it.

## License
MIT
