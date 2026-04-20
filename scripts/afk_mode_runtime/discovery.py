from __future__ import annotations

from collections import deque
import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any

from .common import (
    DEFAULT_PROFILE_ROOT,
    EXPLICIT_DOCS,
    IGNORED_DOC_SEGMENTS,
    MAX_STATUS_ITEMS,
    PATTERN_DOCS,
    SUPPORTED_CAPABILITIES,
    TRUST_MODE_OBSERVE_ONLY,
    VERIFICATION_SOURCE_NONE,
    WORKFLOW_STATE_ROOT_CANDIDATES,
    WRITE_MODE_NONE,
    AfkModeError,
    checked_in_profile_candidates,
    default_profile_path,
    json_load,
    now_utc,
    parse_timestamp,
    relative_to,
    repo_fingerprint,
    run_command,
    unique_strings,
    yaml_dump,
)
from .policy import build_bootstrap_profile, build_repo_context, request_capability


def git_root_for(cwd: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return Path(result.stdout.strip()).resolve()
    return None


def git_info(repo_root: Path | None) -> dict[str, Any]:
    if repo_root is None:
        return {
            "is_repo": False,
            "dirty": False,
            "branch": None,
            "head": None,
            "status_lines": [],
        }

    head = run_command(["git", "-C", str(repo_root), "rev-parse", "HEAD"]).stdout.strip()
    branch_result = subprocess.run(
        ["git", "-C", str(repo_root), "branch", "--show-current"],
        capture_output=True,
        text=True,
    )
    branch = branch_result.stdout.strip() or None
    status_lines = [
        line
        for line in run_command(["git", "-C", str(repo_root), "status", "--short"]).stdout.splitlines()
        if line.strip()
    ]
    return {
        "is_repo": True,
        "dirty": bool(status_lines),
        "branch": branch,
        "head": head,
        "status_lines": status_lines,
    }


def should_ignore_doc(path: Path, repo_root: Path) -> bool:
    relative = path.relative_to(repo_root)
    parts = {part.lower() for part in relative.parts[:-1]}
    return bool(parts & IGNORED_DOC_SEGMENTS)


def detect_design_docs(repo_root: Path) -> list[dict[str, Any]]:
    docs: dict[Path, dict[str, Any]] = {}
    for kind, relative, priority, reason in EXPLICIT_DOCS:
        path = repo_root / relative
        if not path.exists() or should_ignore_doc(path, repo_root):
            continue
        docs[path] = {
            "kind": kind,
            "path": str(path),
            "relative_path": relative_to(path, repo_root),
            "priority": priority,
            "reason": reason,
        }

    docs_dir = repo_root / "docs"
    if docs_dir.exists():
        for kind, pattern, priority, reason in PATTERN_DOCS:
            for path in docs_dir.rglob(pattern):
                if not path.is_file() or should_ignore_doc(path, repo_root):
                    continue
                existing = docs.get(path)
                candidate = {
                    "kind": kind,
                    "path": str(path),
                    "relative_path": relative_to(path, repo_root),
                    "priority": priority,
                    "reason": reason,
                }
                if existing is None or candidate["priority"] > existing["priority"]:
                    docs[path] = candidate

    return sorted(
        docs.values(),
        key=lambda item: (-item["priority"], item["relative_path"]),
    )


def detect_workflow_state_roots(repo_root: Path) -> list[dict[str, Any]]:
    roots: list[dict[str, Any]] = []
    for relative in WORKFLOW_STATE_ROOT_CANDIDATES:
        path = repo_root / relative
        if not path.exists():
            continue
        status_paths = sorted(path.glob("*/STATUS.json"))
        roots.append(
            {
                "relative_path": relative,
                "path": str(path),
                "status_glob": "*/STATUS.json",
                "status_file_count": len(status_paths),
            }
        )
    return roots


def load_package_scripts(repo_root: Path) -> dict[str, str]:
    package_json = repo_root / "package.json"
    if not package_json.exists():
        return {}
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {
        key: value
        for key, value in scripts.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def detect_execution_entrypoints(repo_root: Path, package_scripts: dict[str, str]) -> list[str]:
    entries: list[str] = []
    if (repo_root / "scripts" / "codex" / "work-item.sh").exists():
        entries.append("bash scripts/codex/work-item.sh")
    if (repo_root / "strata" / "cli.py").exists():
        entries.append("python3 -m strata.cli workflow")
    if "harness:new" in package_scripts:
        entries.append("npm run harness:new")
    return unique_strings(entries)


def detect_verification_commands(repo_root: Path, package_scripts: dict[str, str]) -> list[str]:
    commands: list[str] = []
    if "harness:verify-local" in package_scripts:
        commands.append("corepack pnpm harness:verify-local")
    if (repo_root / "scripts" / "verify_change.py").exists():
        commands.append("python3 scripts/verify_change.py --profile harness")
    if "verify:release" in package_scripts:
        commands.append("npm run verify:release")
    if "verify:static" in package_scripts:
        commands.append("npm run verify:static")
    if "test" in package_scripts:
        commands.append("npm run test")
    if not commands and (repo_root / "tests").exists():
        if (repo_root / "pyproject.toml").exists():
            commands.append("pytest")
        else:
            commands.append("python3 -m unittest discover -s tests -p 'test_*.py'")
    return unique_strings(commands)


def detect_generic_file_checks(repo_root: Path) -> list[str]:
    checker_by_suffix = {
        ".py": "python",
        ".js": "node",
        ".mjs": "node",
        ".cjs": "node",
        ".sh": "shell",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
    }
    result: list[str] = []
    try:
        tracked = run_command(["git", "-C", str(repo_root), "ls-files", "-z"]).stdout.split("\0")
    except AfkModeError:
        tracked = []
    for raw_path in tracked:
        if not raw_path:
            continue
        suffix = Path(raw_path).suffix.lower()
        checker = checker_by_suffix.get(suffix)
        if checker is None:
            continue
        result.append(checker)
    return unique_strings(result)


def detect_skill_metadata(repo_root: Path) -> dict[str, Any]:
    paths: list[str] = []
    if (repo_root / "skills" / "README.md").exists():
        paths.append("skills/README.md")
    if (repo_root / ".agents" / "skills").exists():
        paths.append(".agents/skills")
    load_command = None
    if (repo_root / "skills" / "README.md").exists() and (repo_root / "strata" / "cli.py").exists():
        load_command = "python3 -m strata.cli workflow use-skill --task <task-id> --skill <path>"
    return {
        "paths": unique_strings(paths),
        "load_command": load_command,
    }


def build_repo_signals(repo_root: Path, design_docs: list[dict[str, Any]]) -> dict[str, Any]:
    package_scripts = load_package_scripts(repo_root)
    workflow_roots = detect_workflow_state_roots(repo_root)
    truth_order: list[str] = []
    if (repo_root / "AGENTS.md").exists():
        truth_order.append("AGENTS.md")
    truth_order.extend(doc["relative_path"] for doc in design_docs)
    truth_order.extend(
        f"{root['relative_path']}/{root['status_glob']}"
        for root in workflow_roots
        if root.get("status_file_count", 0) > 0
    )
    return {
        "fingerprint": repo_fingerprint(repo_root),
        "truth_order": unique_strings(truth_order),
        "workflow_state": {
            "roots": workflow_roots,
            "execution_entrypoints": detect_execution_entrypoints(repo_root, package_scripts),
        },
        "verification": {
            "commands": detect_verification_commands(repo_root, package_scripts),
            "generic_file_checks": detect_generic_file_checks(repo_root),
        },
        "skills": detect_skill_metadata(repo_root),
    }


def truth_sources_from_context(
    signals: dict[str, Any] | None,
    repo_context: dict[str, Any] | None,
) -> list[str]:
    truth_sources: list[str] = []
    if isinstance(repo_context, dict):
        profile = repo_context.get("profile") or {}
        truth = profile.get("truth") or {}
        order = truth.get("order") or []
        truth_sources.extend(
            item.strip()
            for item in order
            if isinstance(item, str) and item.strip()
        )
    if isinstance(signals, dict):
        truth_order = signals.get("truth_order") or []
        truth_sources.extend(
            item.strip()
            for item in truth_order
            if isinstance(item, str) and item.strip()
        )
    return unique_strings(truth_sources)


def workflow_statuses(repo_root: Path) -> dict[str, Any]:
    state_roots = detect_workflow_state_roots(repo_root)
    if not state_roots:
        return {
            "has_codex_work": False,
            "status_file_count": 0,
            "recent_completed_work": [],
            "open_work": [],
            "state_roots": [],
        }

    entries: list[dict[str, Any]] = []
    for root in state_roots:
        root_path = repo_root / root["relative_path"]
        for path in sorted(root_path.glob(root["status_glob"])):
            try:
                payload = json_load(path)
            except json.JSONDecodeError:
                continue
            approval = payload.get("approval") or {}
            updated_at = payload.get("updated_at")
            updated_sort = parse_timestamp(updated_at) or dt.datetime.fromtimestamp(
                path.stat().st_mtime, tz=dt.timezone.utc
            )
            entries.append(
                {
                    "task_id": payload.get("task_id") or path.parent.name,
                    "phase": payload.get("phase"),
                    "workflow_path": payload.get("workflow_path"),
                    "request_summary": approval.get("request_summary"),
                    "updated_at": updated_at or updated_sort.replace(microsecond=0).isoformat(),
                    "status_path": str(path),
                    "_updated_sort": updated_sort,
                }
            )

    entries.sort(key=lambda item: item["_updated_sort"], reverse=True)
    recent_completed = [
        {key: value for key, value in item.items() if key != "_updated_sort"}
        for item in entries
        if item["phase"] == "closed"
    ][:MAX_STATUS_ITEMS]
    open_work = [
        {key: value for key, value in item.items() if key != "_updated_sort"}
        for item in entries
        if item["phase"] != "closed"
    ][:MAX_STATUS_ITEMS]
    return {
        "has_codex_work": True,
        "status_file_count": len(entries),
        "recent_completed_work": recent_completed,
        "open_work": open_work,
        "state_roots": state_roots,
    }


def nearby_repo_candidates(
    cwd: Path,
    max_depth: int = 2,
    limit: int = 10,
    *,
    profile_root: Path = DEFAULT_PROFILE_ROOT,
) -> list[dict[str, Any]]:
    base = cwd.resolve()
    candidates: list[dict[str, Any]] = []
    seen: set[Path] = set()
    queue: deque[tuple[Path, int]] = deque()
    try:
        children = sorted(base.iterdir(), key=lambda item: item.name)
    except OSError:
        children = []
    for child in children:
        if child.is_symlink() or not child.is_dir() or child.name.startswith("."):
            continue
        queue.append((child, 1))

    while queue:
        path, depth = queue.popleft()
        repo_root = git_root_for(path)
        if repo_root is None or repo_root in seen:
            pass
        else:
            docs = detect_design_docs(repo_root)
            signals = build_repo_signals(repo_root, docs)
            repo_context = build_repo_context(repo_root, signals, profile_root=profile_root)
            truth_sources = truth_sources_from_context(signals, repo_context)
            if truth_sources:
                seen.add(repo_root)
                candidates.append(
                    {
                        "repo_root": str(repo_root),
                        "repo_name": repo_root.name,
                        "design_docs": docs[:3],
                        "truth_sources": truth_sources[:5],
                        "trust_mode": repo_context["trust_mode"],
                        "checked_in_profile_path": repo_context["checked_in_profile_path"],
                    }
                )
                if len(candidates) >= limit:
                    break
        if depth >= max_depth:
            continue
        try:
            children = sorted(path.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for child in children:
            if child.is_symlink() or not child.is_dir() or child.name.startswith("."):
                continue
            queue.append((child, depth + 1))
    candidates.sort(key=lambda item: item["repo_name"])
    return candidates


def discover_repo(
    cwd: Path,
    *,
    profile_path: Path | None = None,
    profile_root: Path = DEFAULT_PROFILE_ROOT,
) -> dict[str, Any]:
    repo_root = git_root_for(cwd)
    design_docs = detect_design_docs(repo_root) if repo_root else []
    signals = build_repo_signals(repo_root, design_docs) if repo_root else None
    repo_context = (
        build_repo_context(
            repo_root,
            signals,
            explicit_profile=profile_path,
            profile_root=profile_root,
        )
        if repo_root and signals
        else None
    )
    truth_sources = truth_sources_from_context(signals, repo_context)
    capability_verdicts = (
        {
            capability: request_capability(
                {
                    **repo_context,
                    "repo_root_path": repo_root,
                    "profile_root": profile_root,
                    "signals": signals,
                },
                capability,
            )
            for capability in sorted(SUPPORTED_CAPABILITIES)
        }
        if repo_root and signals and repo_context
        else {}
    )
    return {
        "generated_at": now_utc(),
        "cwd": str(cwd.resolve()),
        "repo_root": str(repo_root) if repo_root else None,
        "repo_name": repo_root.name if repo_root else None,
        "design_docs": design_docs,
        "truth_sources": truth_sources,
        "git": git_info(repo_root),
        "workflow": {
            "has_agents_md": (repo_root / "AGENTS.md").exists() if repo_root else False,
            "has_repo_skills": bool(signals["skills"]["paths"]) if signals else False,
            **(workflow_statuses(repo_root) if repo_root else {
                "has_codex_work": False,
                "status_file_count": 0,
                "recent_completed_work": [],
                "open_work": [],
                "state_roots": [],
            }),
            "execution_entrypoints": signals["workflow_state"]["execution_entrypoints"] if signals else [],
        },
        "verification": signals["verification"] if signals else {"commands": [], "generic_file_checks": []},
        "signals": signals,
        "repo_context": repo_context,
        "trust_mode": repo_context["trust_mode"] if repo_context else TRUST_MODE_OBSERVE_ONLY,
        "policy_source": repo_context["policy_source"] if repo_context else "heuristic",
        "write_mode": repo_context["write_mode"] if repo_context else WRITE_MODE_NONE,
        "write_authorized": bool(repo_context["write_authorized"]) if repo_context else False,
        "verification_source": (
            repo_context["verification_source"] if repo_context else VERIFICATION_SOURCE_NONE
        ),
        "verification_route": repo_context["verification_route"] if repo_context else [],
        "fallback_verification_route": (
            repo_context["fallback_verification_route"] if repo_context else []
        ),
        "capability_verdicts": capability_verdicts,
        "repo_candidates": (
            nearby_repo_candidates(cwd, profile_root=profile_root) if repo_root is None else []
        ),
    }


def bootstrap_profile(
    cwd: Path,
    *,
    output: Path | None = None,
    allow_isolated_write: bool = False,
    force: bool = False,
    profile_root: Path = DEFAULT_PROFILE_ROOT,
) -> dict[str, Any]:
    repo_root = git_root_for(cwd)
    if repo_root is None:
        raise AfkModeError("AFK Mode can only bootstrap a profile inside a git repository.")
    design_docs = detect_design_docs(repo_root)
    signals = build_repo_signals(repo_root, design_docs)
    output_path = output or default_profile_path(repo_root, profile_root)
    checked_in_targets = {path.resolve() for path in checked_in_profile_candidates(repo_root)}
    if allow_isolated_write and output_path.resolve() not in checked_in_targets:
        raise AfkModeError(
            "bootstrap-profile --allow-isolated-write is only valid when writing a checked-in repo profile."
        )
    if output_path.exists() and not force:
        raise AfkModeError(f"Profile already exists: {output_path}. Re-run with --force to overwrite.")
    payload = build_bootstrap_profile(
        repo_root,
        signals,
        allow_isolated_write=allow_isolated_write,
    )
    yaml_dump(output_path, payload)
    repo_context = build_repo_context(repo_root, signals, explicit_profile=output_path, profile_root=profile_root)
    return {
        "output": str(output_path),
        "repo_root": str(repo_root),
        "trust_mode": repo_context["trust_mode"],
        "policy_source": repo_context["policy_source"],
        "write_mode": repo_context["write_mode"],
        "write_authorized": bool(repo_context["write_authorized"]),
        "verification_source": repo_context["verification_source"],
        "capabilities": payload["capabilities"],
        "profile": payload,
    }
