from __future__ import annotations

import datetime as dt
import shutil
import uuid
from pathlib import Path
from typing import Any

from .common import AfkModeError, is_within, json_dump, now_utc, run_command, slugify
from .run_state import clear_active_run, load_run, register_active_run, save_run


def build_run_id(repo_name: str) -> str:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return f"{timestamp}-{slugify(repo_name)}-{uuid.uuid4().hex[:8]}"


def create_run_artifacts(run_root: Path, discovery: dict[str, Any]) -> tuple[str, Path]:
    run_id = build_run_id(discovery["repo_name"])
    run_dir = run_root / run_id
    while run_dir.exists():
        run_id = build_run_id(discovery["repo_name"])
        run_dir = run_root / run_id
    for name in ("logs", "patches", "worktrees"):
        (run_dir / name).mkdir(parents=True, exist_ok=True)
    json_dump(run_dir / "discovery.json", discovery)
    return run_id, run_dir


def save_run_and_register(
    run_root: Path,
    repo_root: Path,
    run_dir: Path,
    run_payload: dict[str, Any],
) -> None:
    save_run(run_dir, run_payload)
    try:
        register_active_run(run_root, repo_root, run_dir, run_payload["run_id"])
    except AfkModeError:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise


def create_slice_worktree(
    run_dir: Path,
    run_payload: dict[str, Any],
    slice_id: str,
    ordinal: int,
    slug: str | None,
) -> tuple[str, Path, str]:
    safe_slug = slugify(slug or slice_id)
    branch = f"afk/{run_payload['run_id']}/{ordinal:02d}-{safe_slug}"
    worktree = run_dir / "worktrees" / f"{ordinal:02d}-{safe_slug}"
    repo_root = Path(run_payload["repo_root"])
    base_ref = run_payload["git_baseline"]["head"]
    run_command(
        ["git", "-C", str(repo_root), "worktree", "add", "-b", branch, str(worktree), base_ref]
    )
    return branch, worktree, safe_slug


def finish_run(run_dir: Path, status_name: str, summary: str) -> dict[str, Any]:
    payload = load_run(run_dir)
    if payload.get("active_slice"):
        raise AfkModeError("Cannot finish a run while an active slice is still open.")
    payload["status"] = status_name
    payload["finished_at"] = now_utc()
    payload["summary"] = summary
    save_run(run_dir, payload)
    clear_active_run(run_dir.parent, Path(payload["repo_root"]), payload["run_id"])
    return payload


def _validate_patch_capture(
    run_dir: Path | None,
    repo_root: Path,
    output: Path,
) -> None:
    if run_dir is None:
        raise AfkModeError(
            "save-patch requires --run-dir so the active slice worktree and patch "
            "destination can be verified."
        )
    payload = load_run(run_dir)
    active = payload.get("active_slice")
    if active is None:
        raise AfkModeError("Cannot save a patch because the run has no active slice.")
    expected_worktree = Path(active["worktree"]).resolve()
    if repo_root.resolve() != expected_worktree:
        raise AfkModeError(
            f"Patch capture must target the active slice worktree: {expected_worktree}"
        )
    patches_dir = (run_dir / "patches").resolve()
    if not is_within(output, patches_dir):
        raise AfkModeError(
            f"Patch output must stay under the run patches directory: {patches_dir}"
        )


def save_patch(
    repo_root: Path,
    output: Path,
    include_untracked: bool,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    _validate_patch_capture(run_dir, repo_root, output)
    if include_untracked:
        run_command(["git", "-C", str(repo_root), "add", "--intent-to-add", "--all"])
    diff = run_command(["git", "-C", str(repo_root), "diff", "--binary", "HEAD"]).stdout
    if not diff:
        raise AfkModeError("No changes found to save as a patch.")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(diff, encoding="utf-8")
    return {
        "output": str(output),
        "bytes": output.stat().st_size,
        "include_untracked": include_untracked,
        "run_dir": str(run_dir) if run_dir else None,
    }


def cleanup_run(run_dir: Path) -> dict[str, Any]:
    payload = load_run(run_dir)
    repo_root = Path(payload["repo_root"])
    worktrees_dir = run_dir / "worktrees"
    if not worktrees_dir.exists():
        return {"removed": [], "skipped": [], "failed": []}
    active_worktree = None
    if payload.get("active_slice"):
        active_worktree = Path(payload["active_slice"]["worktree"])

    removed: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, Any]] = []
    for worktree in sorted(worktrees_dir.iterdir()):
        if not worktree.exists():
            continue
        if active_worktree and worktree == active_worktree:
            skipped.append(str(worktree))
            continue
        result = run_command(
            ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(worktree)],
            check=False,
        )
        if result.returncode == 0:
            removed.append(str(worktree))
            continue
        if worktree.exists():
            failed.append(
                {
                    "worktree": str(worktree),
                    "reason": result.stderr.strip() or result.stdout.strip() or "unknown",
                }
            )
    return {"removed": removed, "skipped": skipped, "failed": failed}
