"""A course git repository (slides, exercises …), cloned or pulled into the course folder."""

from __future__ import annotations

from pathlib import Path

from coursesync.sources.common import Ctx, Item, git


def sync_git(ctx: Ctx, course, src: dict) -> list[Item]:
    dest = course.folder / src.get("dest", Path(src["repo"]).stem.removesuffix(".git"))
    if ctx.dry_run:
        return []
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = git(["clone", "-q", src["repo"], str(dest)])
        if r.returncode != 0:
            ctx.report.problem(course.slug, f"git clone {src['repo']}: {r.stderr.strip()[-200:]}")
            return []
        return [Item(course.slug, "git", str(dest), src["repo"], source="git", title="cloned the repository")]
    before = git(["rev-parse", "HEAD"], dest).stdout.strip()
    r = git(["pull", "--ff-only", "-q"], dest)
    if r.returncode != 0:
        ctx.report.problem(course.slug, f"git pull {dest.name}: failed (offline, local changes, or SSH key locked)")
        return []
    after = git(["rev-parse", "HEAD"], dest).stdout.strip()
    if before == after:
        return []
    changed = [f for f in git(["diff", "--name-only", before, after], dest).stdout.splitlines() if (dest / f).is_file()]
    return [Item(course.slug, "git", str(dest / f), src["repo"], source="git", title=f) for f in changed]
