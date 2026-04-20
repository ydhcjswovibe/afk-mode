from __future__ import annotations

import datetime as dt
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .common import (
    VERIFICATION_SOURCE_FALLBACK,
    AfkModeError,
    VERIFICATION_SOURCE_NONE,
    VERIFICATION_SOURCE_REPO_OWNED,
    WRITE_MODE_FALLBACK,
    is_within,
    json_dump,
    json_load,
    now_utc,
    relative_to,
    run_command,
    sha256_text,
    slugify,
    unique_strings,
    write_text_atomic,
    yaml,
)
from .run_state import (
    load_run,
    load_run_policy_source,
    load_run_verification_route,
    load_run_verification_source,
    load_run_write_mode,
)


FALLBACK_BLOCKED_EXACT_NAMES = {
    "AGENTS.md",
    "Dockerfile",
    "Jenkinsfile",
    "Makefile",
    "Cargo.lock",
    "Cargo.toml",
    "Pipfile",
    "Pipfile.lock",
    "bun.lockb",
    "go.mod",
    "go.sum",
    "package-lock.json",
    "package.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "pyproject.toml",
    "requirements-dev.txt",
    "requirements.txt",
    "uv.lock",
    "yarn.lock",
}
FALLBACK_BLOCKED_PATH_PREFIXES = (
    ".agents/",
    ".codex/",
    ".github/",
    ".gitlab/",
    ".circleci/",
    "deploy/",
    "deployment/",
    "docs/",
    "infra/",
    "k8s/",
    "ops/",
    "skills/",
)
FALLBACK_BLOCKED_SEGMENTS = {
    ".codex",
    ".github",
    ".gitlab",
    ".circleci",
    "deploy",
    "deployment",
    "infra",
    "k8s",
    "ops",
}
FALLBACK_ALLOWED_SUFFIXES = {
    ".cjs",
    ".js",
    ".json",
    ".mjs",
    ".py",
    ".sh",
    ".yaml",
    ".yml",
}
VERIFY_COMMAND_TIMEOUT_SECONDS = 1200


def verification_artifact_dir(run_dir: Path, slice_id: str) -> Path:
    return run_dir / "logs" / "verification" / f"{slugify(slice_id)}-{sha256_text(slice_id)[:8]}"


def verification_result_path(run_dir: Path, slice_id: str) -> Path:
    return verification_artifact_dir(run_dir, slice_id) / "verification_result.json"


def _clean_status_lines(worktree_path: Path) -> list[str]:
    return [
        line
        for line in run_command(["git", "-C", str(worktree_path), "status", "--short"]).stdout.splitlines()
        if line.strip()
    ]


def _diff_name_status(worktree_path: Path, base_ref: str) -> list[dict[str, Any]]:
    output = run_command(
        ["git", "-C", str(worktree_path), "diff", "--name-status", "--find-renames", f"{base_ref}..HEAD"]
    ).stdout
    entries: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        parts = raw_line.split("\t")
        status_token = parts[0].strip()
        status = status_token[:1]
        if status in {"R", "C"}:
            if len(parts) < 3:
                raise AfkModeError(f"Unexpected diff format while validating fallback writes: {raw_line}")
            entries.append(
                {
                    "status": status,
                    "status_token": status_token,
                    "old_path": parts[1],
                    "path": parts[2],
                }
            )
            continue
        if len(parts) < 2:
            raise AfkModeError(f"Unexpected diff format while validating fallback writes: {raw_line}")
        entries.append(
            {
                "status": status,
                "status_token": status_token,
                "path": parts[1],
            }
        )
    return entries


def _fallback_path_violation(relative_path: str) -> str | None:
    normalized = relative_path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    path = Path(normalized)
    if path.name in FALLBACK_BLOCKED_EXACT_NAMES:
        return f"fallback write mode does not allow modifying {path.name}"
    lower_normalized = normalized.lower()
    if lower_normalized.startswith(".env"):
        return "fallback write mode does not allow modifying environment files"
    for prefix in FALLBACK_BLOCKED_PATH_PREFIXES:
        if lower_normalized.startswith(prefix.lower()):
            return f"fallback write mode does not allow modifying {prefix.rstrip('/')}"
    if any(segment.lower() in FALLBACK_BLOCKED_SEGMENTS for segment in path.parts[:-1]):
        return f"fallback write mode does not allow modifying protected path {normalized}"
    if path.name.startswith(".") and path.suffix.lower() in {".json", ".yaml", ".yml"}:
        return "fallback write mode does not allow modifying dot-config files"
    if ".config." in path.name.lower():
        return "fallback write mode does not allow modifying build or tool config files"
    if path.suffix.lower() not in FALLBACK_ALLOWED_SUFFIXES:
        return (
            "fallback write mode only allows editing existing source or test files with known "
            f"code-like suffixes, not {relative_path}"
        )
    return None


def validate_fallback_worktree(run_payload: dict[str, Any], worktree_path: Path) -> list[str]:
    status_lines = _clean_status_lines(worktree_path)
    if status_lines:
        raise AfkModeError(
            "Fallback write verification requires a clean worktree so proof matches committed slice state."
        )

    base_ref = str((run_payload.get("git_baseline") or {}).get("head") or "").strip()
    if not base_ref:
        raise AfkModeError("Run is missing a git baseline head for fallback verification.")
    diff_entries = _diff_name_status(worktree_path, base_ref)
    if not diff_entries:
        raise AfkModeError("Fallback write verification found no committed changes in the active slice.")

    changed_paths: list[str] = []
    for entry in diff_entries:
        status = entry["status"]
        changed_path = str(entry["path"])
        if status != "M":
            raise AfkModeError(
                "Fallback write mode only allows modifications to existing files; "
                f"found status {entry['status_token']} for {changed_path}."
            )
        violation = _fallback_path_violation(changed_path)
        if violation is not None:
            raise AfkModeError(violation)
        changed_paths.append(changed_path)
    return unique_strings(changed_paths)


def _checker_for_path(relative_path: str) -> str | None:
    suffix = Path(relative_path).suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in {".js", ".mjs", ".cjs"}:
        return "node"
    if suffix == ".sh":
        return "shell"
    if suffix == ".json":
        return "json"
    if suffix in {".yaml", ".yml"}:
        return "yaml"
    return None


def _generic_file_check_commands(changed_paths: list[str]) -> list[str]:
    grouped: dict[str, list[str]] = {}
    unsupported: list[str] = []
    for relative_path in changed_paths:
        checker = _checker_for_path(relative_path)
        if checker is None:
            unsupported.append(relative_path)
            continue
        grouped.setdefault(checker, []).append(relative_path)

    if unsupported:
        raise AfkModeError(
            "Fallback write mode could not build deterministic verification for changed files: "
            + ", ".join(sorted(unsupported))
        )

    commands: list[str] = []
    if grouped.get("python"):
        quoted = " ".join(shlex.quote(path) for path in sorted(grouped["python"]))
        commands.append(f"python3 -m py_compile {quoted}")
    for path in sorted(grouped.get("node", [])):
        commands.append(f"node --check {shlex.quote(path)}")
    for path in sorted(grouped.get("shell", [])):
        commands.append(f"bash -n {shlex.quote(path)}")
    for path in sorted(grouped.get("json", [])):
        commands.append(
            "python3 -c "
            + shlex.quote(
                "import json, pathlib, sys; json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))"
            )
            + " "
            + shlex.quote(path)
        )
    for path in sorted(grouped.get("yaml", [])):
        commands.append(
            "python3 -c "
            + shlex.quote(
                "import pathlib, sys, yaml; yaml.safe_load(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))"
            )
            + " "
            + shlex.quote(path)
        )
    if not commands:
        raise AfkModeError(
            "Fallback write mode could not find a deterministic verification route for the changed files."
        )
    return commands


def load_verification_policy(
    run_payload: dict[str, Any],
    *,
    worktree_path: Path | None = None,
    fallback_changed_paths: list[str] | None = None,
) -> dict[str, Any]:
    verification_source = load_run_verification_source(run_payload)
    commands = load_run_verification_route(run_payload)
    sources = (run_payload.get("repo_context") or {}).get("sources") or []
    if verification_source == VERIFICATION_SOURCE_REPO_OWNED:
        if not commands:
            raise AfkModeError("Run is missing an authoritative verification route.")
        return {
            "commands": commands,
            "policy_source": load_run_policy_source(run_payload),
            "sources": sources,
            "verification_source": verification_source,
            "mode": "command_route",
            "changed_paths": [],
        }
    if verification_source == VERIFICATION_SOURCE_FALLBACK:
        if commands:
            return {
                "commands": commands,
                "policy_source": load_run_policy_source(run_payload),
                "sources": sources,
                "verification_source": verification_source,
                "mode": "command_route",
                "changed_paths": list(fallback_changed_paths or []),
            }
        if worktree_path is None:
            raise AfkModeError("Generic file-level verification requires a worktree path.")
        changed_paths = list(fallback_changed_paths or [])
        if not changed_paths:
            raise AfkModeError(
                "Generic file-level verification requires changed files to build deterministic checks."
            )
        return {
            "commands": _generic_file_check_commands(changed_paths),
            "policy_source": load_run_policy_source(run_payload),
            "sources": sources,
            "verification_source": verification_source,
            "mode": "changed_files_only",
            "changed_paths": changed_paths,
        }
    if verification_source == VERIFICATION_SOURCE_NONE:
        raise AfkModeError("Run is missing a deterministic verification route.")
    raise AfkModeError(f"Unsupported verification source: {verification_source}")


def verify_slice(
    run_dir: Path,
    slice_id: str,
    worktree: str | None = None,
) -> dict[str, Any]:
    payload = load_run(run_dir)
    if payload["status"] != "running":
        raise AfkModeError(f"Cannot verify a slice on a run with status '{payload['status']}'.")
    active = payload.get("active_slice")
    if worktree is None:
        if active is None or active.get("slice_id") != slice_id:
            raise AfkModeError(
                "verify-slice requires the target slice to be active or an explicit --worktree path."
            )
        worktree_path = Path(active["worktree"]).resolve()
    else:
        worktree_path = Path(worktree).resolve()
        if not worktree_path.exists():
            raise AfkModeError(f"Verification worktree was not found: {worktree_path}")
        worktrees_root = (run_dir / "worktrees").resolve()
        if not is_within(worktree_path, worktrees_root):
            raise AfkModeError(
                f"Verification worktree must stay under the run worktrees directory: {worktrees_root}"
            )

    fallback_changed_paths: list[str] = []
    if load_run_write_mode(payload) == WRITE_MODE_FALLBACK:
        fallback_changed_paths = validate_fallback_worktree(payload, worktree_path)

    policy = load_verification_policy(
        payload,
        worktree_path=worktree_path,
        fallback_changed_paths=fallback_changed_paths,
    )
    verified_head = run_command(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"]
    ).stdout.strip()
    verified_branch_result = subprocess.run(
        ["git", "-C", str(worktree_path), "branch", "--show-current"],
        capture_output=True,
        text=True,
    )
    verified_branch = verified_branch_result.stdout.strip() or None
    artifact_dir = verification_artifact_dir(run_dir, slice_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    all_passed = True
    for index, command in enumerate(policy["commands"], start=1):
        started_at = dt.datetime.now(dt.timezone.utc)
        timed_out = False
        exit_code: int | None
        stdout = ""
        stderr = ""
        try:
            completed = subprocess.run(
                ["bash", "-lc", command],
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
                timeout=VERIFY_COMMAND_TIMEOUT_SECONDS,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            completed = None
            timed_out = True
            exit_code = None
            stdout = exc.stdout or ""
            stderr = (exc.stderr or "") + (
                f"\nTimed out after {VERIFY_COMMAND_TIMEOUT_SECONDS} seconds."
            )
        ended_at = dt.datetime.now(dt.timezone.utc)
        stdout_path = artifact_dir / f"{index:02d}-stdout.log"
        stderr_path = artifact_dir / f"{index:02d}-stderr.log"
        write_text_atomic(stdout_path, stdout)
        write_text_atomic(stderr_path, stderr)
        duration_seconds = max(0.0, (ended_at - started_at).total_seconds())
        passed = exit_code == 0 and not timed_out
        all_passed = all_passed and passed
        results.append(
            {
                "ordinal": index,
                "command": command,
                "status": "passed" if passed else "failed",
                "exit_code": exit_code,
                "timed_out": timed_out,
                "duration_seconds": round(duration_seconds, 3),
                "stdout_log": relative_to(stdout_path, run_dir),
                "stderr_log": relative_to(stderr_path, run_dir),
                "started_at": started_at.replace(microsecond=0).isoformat(),
                "finished_at": ended_at.replace(microsecond=0).isoformat(),
            }
        )

    proof = {
        "run_id": payload["run_id"],
        "slice_id": slice_id,
        "verified_at": now_utc(),
        "repo_root": payload["repo_root"],
        "worktree": str(worktree_path),
        "verified_head": verified_head,
        "verified_branch": verified_branch,
        "policy_source": policy["policy_source"],
        "verification_source": policy["verification_source"],
        "write_mode": load_run_write_mode(payload),
        "sources": policy["sources"],
        "allowed_commands": policy["commands"],
        "verification_mode": policy["mode"],
        "changed_paths": policy["changed_paths"],
        "results": results,
        "all_passed": all_passed,
    }
    json_dump(verification_result_path(run_dir, slice_id), proof)
    return proof


def load_verification_result(run_dir: Path, slice_id: str) -> dict[str, Any]:
    path = verification_result_path(run_dir, slice_id)
    if not path.exists():
        raise AfkModeError(
            "Successful slices require a verification result artifact. Run verify-slice first."
        )
    try:
        payload = json_load(path)
    except Exception as exc:
        raise AfkModeError(f"Verification result artifact is invalid JSON: {path}") from exc
    if payload.get("slice_id") != slice_id:
        raise AfkModeError(
            f"Verification result artifact slice mismatch: expected {slice_id}, got {payload.get('slice_id')}"
        )
    if not isinstance(payload.get("results"), list) or not payload["results"]:
        raise AfkModeError("Verification result artifact is missing executed command results.")
    return payload
