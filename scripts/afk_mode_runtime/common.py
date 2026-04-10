from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - dependency is expected in runtime/test envs
    yaml = None


IGNORED_DOC_SEGMENTS = {
    ".codex",
    ".git",
    "archive",
    "archives",
    "archived",
    "completed",
    "deprecated",
    "exec-plans",
    "reference",
    "references",
}
EXPLICIT_DOCS: tuple[tuple[str, str, int, str], ...] = (
    ("canonical_spec", "SPEC.md", 100, "repo-root canonical spec"),
    ("canonical_spec", "docs/SPEC.md", 95, "docs canonical spec"),
    ("design", "DESIGN.md", 90, "repo-root design doc"),
    ("design", "docs/DESIGN.md", 85, "docs design doc"),
    ("prd", "PRD.md", 84, "repo-root PRD"),
    ("prd", "docs/PRD.md", 83, "docs PRD"),
)
PATTERN_DOCS: tuple[tuple[str, str, int, str], ...] = (
    ("future_mvp", "*mvp*.md", 80, "future MVP or gap doc"),
    ("roadmap", "*roadmap*.md", 70, "roadmap doc"),
    ("requirements", "*requirements*.md", 65, "requirements doc"),
)
MAX_STATUS_ITEMS = 10
DEFAULT_RUN_ROOT = Path.home() / ".codex" / "afk-runs"
ACTIVE_RUNS_FILENAME = "active-runs.json"
DEFAULT_PROFILE_ROOT = Path.home() / ".codex" / "repo-profiles"
CHECKED_IN_PROFILE_RELATIVE_PATHS = (
    Path(".codex") / "plugin-profile.yaml",
    Path(".codex-plugin.yaml"),
)
DURATION_PART_RE = re.compile(r"(?P<value>\d+)(?P<unit>h|m|s)")
CANDIDATES_PLACEHOLDER = "Fill this section with the ranked shortlist before execution starts."
CANDIDATES_META_FILENAME = "candidates.meta.json"
DESTRUCTIVE_COMMAND_RE = re.compile(
    r"(^|\s)(rm\s+-rf|git\s+reset\s+--hard|git\s+checkout\s+--|git\s+clean\s+-fdx?|mv\s+.+\s+/dev/null)",
    re.IGNORECASE,
)
SUPPORTED_CAPABILITIES = {
    "read_discovery",
    "verification_routing",
    "repo_workflow_read",
    "isolated_write",
    "skill_guidance",
    "session_guardrails",
}
WRITE_MODE_NONE = "none"
WRITE_MODE_REPO_OWNED = "repo_owned"
WRITE_MODE_FALLBACK = "fallback"
WRITE_MODES = {
    WRITE_MODE_NONE,
    WRITE_MODE_REPO_OWNED,
    WRITE_MODE_FALLBACK,
}
VERIFICATION_SOURCE_NONE = "none"
VERIFICATION_SOURCE_REPO_OWNED = "repo_owned"
VERIFICATION_SOURCE_FALLBACK = "fallback"
VERIFICATION_SOURCE_GENERIC = VERIFICATION_SOURCE_FALLBACK
VERIFICATION_SOURCES = {
    VERIFICATION_SOURCE_NONE,
    VERIFICATION_SOURCE_REPO_OWNED,
    VERIFICATION_SOURCE_FALLBACK,
}
GUARDRAIL_ACTION_DENY = "deny"
GUARDRAIL_ACTION_ASK_FIRST = "ask_first"
GUARDRAIL_ACTIONS = {
    GUARDRAIL_ACTION_DENY,
    GUARDRAIL_ACTION_ASK_FIRST,
}
GUARDRAIL_MATCH_COMMAND_SUBSTRING = "command_substring"
GUARDRAIL_MATCH_PATH = "path"
GUARDRAIL_MATCH_CATEGORY = "category"
GUARDRAIL_MATCH_TYPES = {
    GUARDRAIL_MATCH_COMMAND_SUBSTRING,
    GUARDRAIL_MATCH_PATH,
    GUARDRAIL_MATCH_CATEGORY,
}
GUARDRAIL_CATEGORY_GIT_PUSH = "git_push"
GUARDRAIL_CATEGORY_GLOBAL_GIT_CONFIG = "global_git_config"
GUARDRAIL_CATEGORIES = {
    GUARDRAIL_CATEGORY_GIT_PUSH,
    GUARDRAIL_CATEGORY_GLOBAL_GIT_CONFIG,
}
GUARDRAIL_APPROVAL_SCOPE_EXACT_COMMAND_ONCE = "exact_command_once"
GUARDRAIL_APPROVAL_SCOPE_RULE_FOR_RUN = "rule_for_run"
GUARDRAIL_APPROVAL_SCOPES = {
    GUARDRAIL_APPROVAL_SCOPE_EXACT_COMMAND_ONCE,
    GUARDRAIL_APPROVAL_SCOPE_RULE_FOR_RUN,
}
WORKFLOW_STATE_ROOT_CANDIDATES = (
    ".codex/work",
    ".codex/work-items",
)
TRUST_MODE_OBSERVE_ONLY = "observe_only"
TRUST_MODE_ASSISTIVE = "assistive"
TRUST_MODE_TRUSTED = "trusted"
TRUST_MODES = {
    TRUST_MODE_OBSERVE_ONLY,
    TRUST_MODE_ASSISTIVE,
    TRUST_MODE_TRUSTED,
}
ESTIMATION_SIZE_VALUES = {
    "small",
    "medium",
    "large",
}
ESTIMATION_RISK_VALUES = {
    "low",
    "medium",
    "high",
}
ESTIMATION_VERIFY_COST_VALUES = {
    "fast",
    "medium",
    "slow",
}


class AfkModeError(RuntimeError):
    """Raised when helper commands cannot complete."""


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        detail = stderr or stdout or f"exit {result.returncode}"
        raise AfkModeError(f"{' '.join(command)} failed: {detail}")
    return result


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def json_dump(path: Path, payload: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(payload, indent=2) + "\n")


def json_load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def yaml_load(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise AfkModeError("PyYAML is required to read plugin profiles.")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AfkModeError(f"Invalid YAML in profile {path}: {exc}") from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise AfkModeError(f"Profile {path} must contain a top-level mapping.")
    return payload


def yaml_dump(path: Path, payload: dict[str, Any]) -> None:
    if yaml is None:
        raise AfkModeError("PyYAML is required to write plugin profiles.")
    write_text_atomic(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=False))


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def repo_fingerprint(repo_root: Path) -> str:
    return sha256_text(str(repo_root.resolve()))[:16]


def default_profile_path(repo_root: Path, profile_root: Path = DEFAULT_PROFILE_ROOT) -> Path:
    return profile_root / f"{repo_fingerprint(repo_root)}.yaml"


def checked_in_profile_candidates(repo_root: Path) -> list[Path]:
    return [repo_root / relative for relative in CHECKED_IN_PROFILE_RELATIVE_PATHS]


def ensure_mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AfkModeError(f"{label} must be a mapping.")
    return value


def normalize_str_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise AfkModeError(f"{label} must not contain empty strings.")
        return [stripped]
    if not isinstance(value, list):
        raise AfkModeError(f"{label} must be a string or list of strings.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AfkModeError(f"{label} entries must be strings.")
        stripped = item.strip()
        if not stripped:
            raise AfkModeError(f"{label} must not contain empty strings.")
        result.append(stripped)
    return result


def unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def parse_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def slugify(value: str) -> str:
    result: list[str] = []
    previous_dash = False
    for char in value.lower():
        if char.isalnum():
            result.append(char)
            previous_dash = False
        elif not previous_dash:
            result.append("-")
            previous_dash = True
    slug = "".join(result).strip("-")
    return slug or "workspace"


def relative_to(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def parse_duration_seconds(value: str) -> int:
    normalized = value.strip().lower().replace(" ", "")
    if not normalized:
        raise AfkModeError("Budget must not be empty.")
    total = 0
    consumed = 0
    for match in DURATION_PART_RE.finditer(normalized):
        amount = int(match.group("value"))
        unit = match.group("unit")
        consumed += len(match.group(0))
        if unit == "h":
            total += amount * 3600
        elif unit == "m":
            total += amount * 60
        elif unit == "s":
            total += amount
    if total <= 0 or consumed != len(normalized):
        raise AfkModeError(
            f"Unsupported budget '{value}'. Use forms like 90m, 4h, or 6h30m."
        )
    return total


def is_within(path: Path, candidate_root: Path) -> bool:
    try:
        path.resolve().relative_to(candidate_root.resolve())
        return True
    except ValueError:
        return False
