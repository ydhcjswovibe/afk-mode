#!/usr/bin/env python3
"""Tests for the afk-mode runtime helper and hook helpers."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import afk_mode
import afk_mode_runtime
import afk_mode_runtime.candidate_queue as candidate_queue
import afk_mode_runtime.hook_config as hook_config
import afk_mode_runtime.kernel_run as kernel_run
import yaml


class AfkModeRuntimeTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.run_root = self.root / ".codex" / "afk-runs"
        self.hooks_path = self.root / ".codex" / "hooks.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def git(self, repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def run_plugin_script(self, script: Path, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HOME"] = str(self.root)
        return subprocess.run(
            ["python3", str(script)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HOME"] = str(self.root)
        return subprocess.run(
            ["python3", str(Path(__file__).with_name("afk_mode.py")), *args],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )

    def run_wrapper(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HOME"] = str(self.root)
        wrapper = Path(__file__).resolve().parents[1] / "afk-mode"
        return subprocess.run(
            [str(wrapper), *args],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )

    def init_repo(self, name: str = "repo") -> Path:
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init")
        self.git(repo, "config", "user.name", "AFK Mode Test")
        self.git(repo, "config", "user.email", "afk-mode@example.com")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git(repo, "add", "tracked.txt")
        self.git(repo, "commit", "-m", "Initial commit")
        return repo

    def commit_paths(self, repo: Path, *paths: str, message: str) -> None:
        self.git(repo, "add", *paths)
        self.git(repo, "commit", "-m", message)

    def write_profile(
        self,
        repo: Path,
        *,
        allow_isolated_write: bool = True,
        verify_commands: list[str] | None = None,
        status_roots: list[str] | None = None,
        relative_path: str = ".codex/plugin-profile.yaml",
        guardrails: dict[str, object] | None = None,
        estimation: dict[str, object] | None = None,
    ) -> Path:
        profile_path = repo / Path(relative_path)
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_roots = status_roots or [".codex/work"]
        default_verify_command = 'python3 -c "print(\'afk-mode-ok\')"'
        capabilities = [
            "read_discovery",
            "verification_routing",
            "session_guardrails",
        ]
        if workflow_roots:
            capabilities.append("repo_workflow_read")
        if allow_isolated_write:
            capabilities.append("isolated_write")
        payload = {
            "version": 1,
            "repo_id": repo.name,
            "display_name": repo.name,
            "truth": {
                "order": ["SPEC.md"],
            },
            "verify": {
                "commands": verify_commands or [default_verify_command],
            },
            "workflow": {
                "status_roots": workflow_roots,
                "execution_entrypoints": [],
            },
            "guardrails": {
                "rules": [],
            },
            "skills": {
                "paths": [],
                "load_command": None,
            },
            "capabilities": capabilities,
            "plugin_overrides": {},
        }
        if estimation is not None:
            payload["plugin_overrides"]["afk_mode"] = {
                "estimation": estimation,
            }
        if guardrails:
            payload["guardrails"].update(guardrails)
        profile_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return profile_path

    def rank_candidates(self, run_dir: Path, slice_id: str = "slice-one") -> None:
        self.write_candidate_queue(
            run_dir,
            {
                "slice_id": slice_id,
                "ordinal": 1,
                "title": slice_id,
                "plan_status": "frozen",
                "ready_for_execution": True,
            },
        )
        self.write_plan_artifacts(run_dir, slice_id, include_frozen_plan=True)

    def write_candidate_queue(self, run_dir: Path, *entries: dict[str, object]) -> None:
        slices = []
        for index, raw in enumerate(entries, start=1):
            slice_id = str(raw.get("slice_id") or f"slice-{index}")
            slices.append(
                {
                    "slice_id": slice_id,
                    "ordinal": int(raw.get("ordinal") or index),
                    "title": str(raw.get("title") or slice_id),
                    "source": raw.get("source") or {"kind": "manual_override"},
                    "estimate": raw.get("estimate")
                    or {
                        "size": "medium",
                        "risk": "medium",
                        "verify_cost": "medium",
                    },
                    "status": str(raw.get("status") or "queued"),
                    "dependencies": list(raw.get("dependencies") or []),
                    "requires_workflow_token": bool(raw.get("requires_workflow_token", False)),
                    "plan_status": str(raw.get("plan_status") or "missing"),
                    "ready_for_execution": bool(raw.get("ready_for_execution", False)),
                    "workflow_evidence": raw.get("workflow_evidence") or {},
                }
            )
        (run_dir / afk_mode.CANDIDATES_QUEUE_FILENAME).write_text(
            json.dumps(
                {
                    "version": 1,
                    "generated_at": "2026-04-20T00:00:00+00:00",
                    "slices": slices,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (run_dir / "candidates.md").write_text(
            "\n".join(
                [
                    "# AFK Run Candidates",
                    "",
                    "## Candidate Queue",
                    "",
                    *[
                        "\n".join(
                            [
                                f"{entry['ordinal']}. `{entry['slice_id']}`",
                                f"   - Title: {entry['title']}",
                                f"   - Status: {entry['status']}",
                            ]
                        )
                        for entry in slices
                    ],
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def write_plan_artifacts(
        self,
        run_dir: Path,
        slice_id: str,
        *,
        include_plan: bool = True,
        include_review_summary: bool = True,
        include_frozen_plan: bool = False,
    ) -> Path:
        plan_dir = candidate_queue.plan_dir_for(run_dir, slice_id)
        plan_dir.mkdir(parents=True, exist_ok=True)
        if include_plan:
            (plan_dir / "plan.json").write_text(
                json.dumps({"slice_id": slice_id, "goal": "Implement candidate"}, indent=2) + "\n",
                encoding="utf-8",
            )
        if include_review_summary:
            (plan_dir / "review_summary.json").write_text(
                json.dumps({"slice_id": slice_id, "decision": "freeze" if include_frozen_plan else "revise"}, indent=2)
                + "\n",
                encoding="utf-8",
            )
        if include_frozen_plan:
            (plan_dir / "frozen_plan.json").write_text(
                json.dumps({"slice_id": slice_id, "decision": "freeze"}, indent=2) + "\n",
                encoding="utf-8",
            )
        return plan_dir

    def write_candidates_meta(self, run_dir: Path, *entries: dict[str, str]) -> None:
        payload = {"slices": list(entries)}
        (run_dir / "candidates.meta.json").write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )

    def write_status(self, repo: Path, task_id: str, *, phase: str, summary: str) -> None:
        status_path = repo / ".codex" / "work" / task_id / "STATUS.json"
        status_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "task_id": task_id,
            "phase": phase,
            "workflow_path": "implementation",
            "approval": {
                "request_summary": summary,
            },
        }
        status_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def load_hooks(self) -> dict[str, object]:
        if not self.hooks_path.exists():
            return {"hooks": {}}
        return json.loads(self.hooks_path.read_text(encoding="utf-8"))

    def assert_begin_run_blocker_contract(self, payload: dict[str, object]) -> None:
        self.assertEqual(
            set(payload),
            {"started", "blocked", "blocker_code", "reason", "next_action", "recovery", "discovery"},
        )
        self.assertFalse(payload["started"])
        self.assertTrue(payload["blocked"])
        self.assertEqual(
            set(payload["recovery"]),
            {
                "existing_run_id",
                "existing_run_dir",
                "existing_run_status",
                "existing_active_slice_id",
                "existing_active_branch",
                "existing_active_worktree",
                "existing_remaining_seconds",
                "required_approval_type",
                "required_repo_policy_source",
                "current_repo_policy_source",
                "current_write_mode",
                "current_write_authorized",
                "current_verification_source",
                "required_verification_route",
                "suggested_profile_path",
            },
        )

    def test_discover_non_repo_returns_candidates(self) -> None:
        base = self.root / "projects"
        base.mkdir()
        repo = base / "strata"
        repo.mkdir()
        self.git(repo, "init")
        self.git(repo, "config", "user.name", "AFK Mode Test")
        self.git(repo, "config", "user.email", "afk-mode@example.com")
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git(repo, "add", "SPEC.md", "tracked.txt")
        self.git(repo, "commit", "-m", "Initial commit")

        payload = afk_mode.discover_repo(base)

        self.assertFalse(payload["git"]["is_repo"])
        self.assertEqual(payload["repo_root"], None)
        self.assertEqual(payload["repo_candidates"][0]["repo_root"], str(repo))

    def test_discover_reports_dirty_repo(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        (repo / "tracked.txt").write_text("dirty change\n", encoding="utf-8")

        payload = afk_mode.discover_repo(repo)

        self.assertTrue(payload["git"]["is_repo"])
        self.assertTrue(payload["git"]["dirty"])
        self.assertIn("tracked.txt", "\n".join(payload["git"]["status_lines"]))

    def test_discover_reports_assistive_repo_without_profile(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        (repo / "scripts").mkdir()
        (repo / "scripts" / "verify_change.py").write_text("print('ok')\n", encoding="utf-8")
        self.commit_paths(repo, "SPEC.md", "scripts/verify_change.py", message="Add discovery signals")

        payload = afk_mode.discover_repo(repo)

        self.assertEqual(payload["trust_mode"], afk_mode.TRUST_MODE_ASSISTIVE)
        self.assertFalse(payload["capability_verdicts"]["isolated_write"]["allowed"])
        self.assertEqual(payload["write_mode"], "fallback")
        self.assertFalse(payload["write_authorized"])
        self.assertEqual(payload["verification_source"], "fallback")
        self.assertIn("verify_change.py", payload["verification"]["commands"][0])

    def test_discover_reports_assistive_repo_with_agents_truth_only(self) -> None:
        repo = self.init_repo("agents-only")
        (repo / "AGENTS.md").write_text("# Routing\n", encoding="utf-8")
        (repo / "scripts").mkdir()
        (repo / "scripts" / "verify_change.py").write_text("print('ok')\n", encoding="utf-8")
        self.commit_paths(repo, "AGENTS.md", "scripts/verify_change.py", message="Add repo truth")

        payload = afk_mode.discover_repo(repo)

        self.assertEqual(payload["design_docs"], [])
        self.assertIn("AGENTS.md", payload["truth_sources"])
        self.assertEqual(payload["trust_mode"], afk_mode.TRUST_MODE_ASSISTIVE)
        self.assertEqual(payload["write_mode"], "none")

        begin_payload = afk_mode.begin_run(repo, "30m", self.run_root)
        self.assertTrue(begin_payload["blocked"])
        self.assertEqual(begin_payload["blocker_code"], "profile_required")

    def test_discover_non_repo_lists_agents_only_repo_candidate(self) -> None:
        base = self.root / "projects"
        base.mkdir()
        repo = base / "agents-repo"
        repo.mkdir()
        self.git(repo, "init")
        self.git(repo, "config", "user.name", "AFK Mode Test")
        self.git(repo, "config", "user.email", "afk-mode@example.com")
        (repo / "AGENTS.md").write_text("# Routing\n", encoding="utf-8")
        (repo / "scripts").mkdir()
        (repo / "scripts" / "verify_change.py").write_text("print('ok')\n", encoding="utf-8")
        self.git(repo, "add", "AGENTS.md", "scripts/verify_change.py")
        self.git(repo, "commit", "-m", "Add agents-only repo truth")

        payload = afk_mode.discover_repo(base)

        self.assertEqual(payload["repo_candidates"][0]["repo_root"], str(repo))
        self.assertIn("AGENTS.md", payload["repo_candidates"][0]["truth_sources"])

    def test_facade_exports_runtime_surface(self) -> None:
        expected = [
            "AfkModeError",
            "advance_run",
            "discover_repo",
            "bootstrap_profile",
            "begin_run",
            "start_run",
            "status",
            "open_slice",
            "verify_slice",
            "record_slice",
            "finish_run",
            "save_patch",
            "cleanup_run",
            "approve_guardrail",
            "build_session_context",
            "pretool_decision",
            "default_profile_path",
            "relative_to",
            "verification_result_path",
            "estimate_candidates",
        ]

        for name in expected:
            self.assertTrue(hasattr(afk_mode, name), name)

    def test_runtime_package_exports_exact_surface(self) -> None:
        expected = {
            "CANDIDATES_PLACEHOLDER",
            "CANDIDATES_QUEUE_FILENAME",
            "CHECKED_IN_PROFILE_RELATIVE_PATHS",
            "DEFAULT_PROFILE_ROOT",
            "DEFAULT_RUN_ROOT",
            "SUPPORTED_CAPABILITIES",
            "TRUST_MODE_ASSISTIVE",
            "TRUST_MODE_OBSERVE_ONLY",
            "TRUST_MODE_TRUSTED",
            "TRUST_MODES",
            "AfkModeError",
            "advance_run",
            "approve_guardrail",
            "begin_run",
            "bootstrap_profile",
            "build_session_context",
            "cleanup_run",
            "default_profile_path",
            "discover_repo",
            "estimate_candidates",
            "finish_run",
            "load_run",
            "open_slice",
            "pretool_decision",
            "repair_active_runs",
            "record_slice",
            "relative_to",
            "save_patch",
            "save_run",
            "start_run",
            "status",
            "verification_result_path",
            "verify_slice",
        }
        self.assertEqual(set(afk_mode_runtime.__all__), expected)
        self.assertEqual({name for name in expected if hasattr(afk_mode_runtime, name)}, expected)

    def test_facade_cli_discover_smoke(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.commit_paths(repo, "SPEC.md", message="Add spec")

        result = self.run_cli("discover", "--cwd", str(repo))
        payload = json.loads(result.stdout)

        self.assertEqual(payload["repo_root"], str(repo))
        self.assertTrue(payload["git"]["is_repo"])

    def test_wrapper_discover_smoke(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.commit_paths(repo, "SPEC.md", message="Add spec")

        result = self.run_wrapper("discover", "--cwd", str(repo))
        payload = json.loads(result.stdout)

        self.assertEqual(payload["repo_root"], str(repo))
        self.assertTrue(payload["git"]["is_repo"])

    def test_estimate_candidates_uses_baseline_defaults_when_metadata_is_missing(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        started = afk_mode.start_run(repo, "2h", self.run_root)
        run_dir = Path(started["run_dir"])

        self.rank_candidates(run_dir, slice_id="slice-eta")

        payload = afk_mode.estimate_candidates(run_dir)

        self.assertEqual(payload["run_id"], started["run_id"])
        self.assertEqual(len(payload["slices"]), 1)
        estimate = payload["slices"][0]
        self.assertEqual(estimate["slice_id"], "slice-eta")
        self.assertEqual(estimate["size"], "medium")
        self.assertEqual(estimate["risk"], "medium")
        self.assertEqual(estimate["verify_cost"], "medium")
        self.assertEqual(estimate["source"], "baseline")
        self.assertEqual(estimate["estimation_confidence"], "low")
        self.assertEqual(estimate["point_minutes"], 75.0)
        self.assertEqual(estimate["fit_status"], "fit")
        self.assertIn("baseline data", estimate["reason"])

    def test_cli_estimate_candidates_smoke(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        started = afk_mode.start_run(repo, "2h", self.run_root)
        run_dir = Path(started["run_dir"])
        self.rank_candidates(run_dir, slice_id="slice-cli-estimate")

        result = self.run_cli("estimate-candidates", "--run-dir", str(run_dir))
        payload = json.loads(result.stdout)

        self.assertEqual(payload["run_id"], started["run_id"])
        self.assertEqual(payload["slices"][0]["slice_id"], "slice-cli-estimate")

    def test_estimate_candidates_validates_meta_sidecar(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        started = afk_mode.start_run(repo, "2h", self.run_root)
        run_dir = Path(started["run_dir"])

        self.rank_candidates(run_dir, slice_id="slice-bad-meta")
        self.write_candidates_meta(
            run_dir,
            {
                "slice_id": "slice-bad-meta",
                "size": "huge",
                "risk": "low",
                "verify_cost": "fast",
            },
        )

        with self.assertRaisesRegex(afk_mode.AfkModeError, "size must be one of"):
            afk_mode.estimate_candidates(run_dir)

    def test_estimate_candidates_prefers_local_exact_samples_including_failed_attempts(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        started = afk_mode.start_run(repo, "1h", self.run_root)
        run_dir = Path(started["run_dir"])

        self.rank_candidates(run_dir, slice_id="slice-local")
        self.write_candidates_meta(
            run_dir,
            {
                "slice_id": "slice-local",
                "size": "small",
                "risk": "low",
                "verify_cost": "fast",
            },
        )

        fingerprint = afk_mode.default_profile_path(repo, self.root / ".codex" / "repo-profiles").stem
        metrics_path = self.root / ".codex" / "afk-metrics" / f"{fingerprint}.json"
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(
            json.dumps(
                {
                    "repo_root": str(repo),
                    "fingerprint": fingerprint,
                    "samples": [
                        {
                            "run_id": "r1",
                            "slice_id": "s1",
                            "recorded_at": "2026-04-10T00:00:00+00:00",
                            "status": "success",
                            "write_mode": "repo_owned",
                            "size": "small",
                            "risk": "low",
                            "verify_cost": "fast",
                            "wall_clock_seconds": 600,
                            "verification_seconds": 30,
                            "estimate_point_minutes": 0,
                            "estimate_upper_minutes": 0,
                        },
                        {
                            "run_id": "r2",
                            "slice_id": "s2",
                            "recorded_at": "2026-04-10T00:00:00+00:00",
                            "status": "failed",
                            "write_mode": "repo_owned",
                            "size": "small",
                            "risk": "low",
                            "verify_cost": "fast",
                            "wall_clock_seconds": 900,
                            "verification_seconds": 45,
                            "estimate_point_minutes": 0,
                            "estimate_upper_minutes": 0,
                        },
                        {
                            "run_id": "r3",
                            "slice_id": "s3",
                            "recorded_at": "2026-04-10T00:00:00+00:00",
                            "status": "success",
                            "write_mode": "repo_owned",
                            "size": "small",
                            "risk": "low",
                            "verify_cost": "fast",
                            "wall_clock_seconds": 1200,
                            "verification_seconds": 50,
                            "estimate_point_minutes": 0,
                            "estimate_upper_minutes": 0,
                        },
                        {
                            "run_id": "r4",
                            "slice_id": "s4",
                            "recorded_at": "2026-04-10T00:00:00+00:00",
                            "status": "success",
                            "write_mode": "repo_owned",
                            "size": "small",
                            "risk": "low",
                            "verify_cost": "fast",
                            "wall_clock_seconds": 1500,
                            "verification_seconds": 60,
                            "residual_seconds": 1440,
                            "estimate_point_minutes": 0,
                            "estimate_upper_minutes": 0,
                        },
                        {
                            "run_id": "r5",
                            "slice_id": "s5",
                            "recorded_at": "2026-04-10T00:00:00+00:00",
                            "status": "success",
                            "write_mode": "repo_owned",
                            "size": "small",
                            "risk": "low",
                            "verify_cost": "fast",
                            "wall_clock_seconds": 1800,
                            "verification_seconds": 75,
                            "residual_seconds": 1725,
                            "estimate_point_minutes": 0,
                            "estimate_upper_minutes": 0,
                        },
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        payload = afk_mode.estimate_candidates(run_dir)
        estimate = payload["slices"][0]

        self.assertEqual(
            set(estimate),
            {
                "slice_id",
                "ordinal",
                "size",
                "risk",
                "verify_cost",
                "point_minutes",
                "upper_minutes",
                "fit_status",
                "estimation_confidence",
                "samples_used",
                "source",
                "reason",
                "metadata_defaulted",
            },
        )
        self.assertEqual(estimate["source"], "local_exact")
        self.assertEqual(estimate["samples_used"], 5)
        self.assertEqual(estimate["point_minutes"], 20.0)
        self.assertEqual(estimate["upper_minutes"], 25.0)
        self.assertEqual(estimate["estimation_confidence"], "high")
        self.assertEqual(estimate["fit_status"], "fit")
        self.assertIn("5 local exact samples", estimate["reason"])

    def test_estimate_candidates_prefers_recent_samples_within_exact_cohort(self) -> None:
        repo = self.init_repo("estimate-recent")
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        started = afk_mode.start_run(repo, "2h", self.run_root)
        run_dir = Path(started["run_dir"])

        self.rank_candidates(run_dir, slice_id="slice-recent")
        self.write_candidates_meta(
            run_dir,
            {
                "slice_id": "slice-recent",
                "size": "small",
                "risk": "low",
                "verify_cost": "fast",
            },
        )

        fingerprint = afk_mode.default_profile_path(repo, self.root / ".codex" / "repo-profiles").stem
        metrics_path = self.root / ".codex" / "afk-metrics" / f"{fingerprint}.json"
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        samples = []
        for index in range(25):
            samples.append(
                {
                    "run_id": f"r{index + 1}",
                    "slice_id": f"s{index + 1}",
                    "recorded_at": f"2026-04-{index + 1:02d}T00:00:00+00:00",
                    "status": "success",
                    "write_mode": "repo_owned",
                    "size": "small",
                    "risk": "low",
                    "verify_cost": "fast",
                    "wall_clock_seconds": 1800 if index >= 20 else 600,
                    "verification_seconds": 30,
                    "estimate_point_minutes": 0,
                    "estimate_upper_minutes": 0,
                }
            )
        metrics_path.write_text(
            json.dumps(
                {
                    "repo_root": str(repo),
                    "fingerprint": fingerprint,
                    "samples": samples,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        estimate = afk_mode.estimate_candidates(run_dir)["slices"][0]

        self.assertEqual(estimate["source"], "local_exact")
        self.assertEqual(estimate["samples_used"], 20)
        self.assertEqual(estimate["point_minutes"], 10.0)
        self.assertEqual(estimate["upper_minutes"], 30.0)

    def test_estimate_candidates_uses_file_order_when_recorded_at_is_missing(self) -> None:
        repo = self.init_repo("estimate-file-order")
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        started = afk_mode.start_run(repo, "2h", self.run_root)
        run_dir = Path(started["run_dir"])

        self.rank_candidates(run_dir, slice_id="slice-file-order")
        self.write_candidates_meta(
            run_dir,
            {
                "slice_id": "slice-file-order",
                "size": "small",
                "risk": "low",
                "verify_cost": "fast",
            },
        )

        fingerprint = afk_mode.default_profile_path(repo, self.root / ".codex" / "repo-profiles").stem
        metrics_path = self.root / ".codex" / "afk-metrics" / f"{fingerprint}.json"
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        samples = []
        for index in range(25):
            samples.append(
                {
                    "run_id": f"r{index + 1}",
                    "slice_id": f"s{index + 1}",
                    "status": "success",
                    "write_mode": "repo_owned",
                    "size": "small",
                    "risk": "low",
                    "verify_cost": "fast",
                    "wall_clock_seconds": 1800 if index >= 20 else 600,
                    "verification_seconds": 30,
                    "estimate_point_minutes": 0,
                    "estimate_upper_minutes": 0,
                }
            )
        metrics_path.write_text(
            json.dumps(
                {
                    "repo_root": str(repo),
                    "fingerprint": fingerprint,
                    "samples": samples,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        estimate = afk_mode.estimate_candidates(run_dir)["slices"][0]

        self.assertEqual(estimate["source"], "local_exact")
        self.assertEqual(estimate["samples_used"], 20)
        self.assertEqual(estimate["upper_minutes"], 30.0)

    def test_estimate_candidates_applies_recent_window_after_cohort_selection(self) -> None:
        repo = self.init_repo("estimate-cohort")
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        started = afk_mode.start_run(repo, "2h", self.run_root)
        run_dir = Path(started["run_dir"])

        self.rank_candidates(run_dir, slice_id="slice-cohort")
        self.write_candidates_meta(
            run_dir,
            {
                "slice_id": "slice-cohort",
                "size": "small",
                "risk": "low",
                "verify_cost": "fast",
            },
        )

        fingerprint = afk_mode.default_profile_path(repo, self.root / ".codex" / "repo-profiles").stem
        metrics_path = self.root / ".codex" / "afk-metrics" / f"{fingerprint}.json"
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        samples = [
            {
                "run_id": "exact-1",
                "slice_id": "exact-1",
                "recorded_at": "2026-04-01T00:00:00+00:00",
                "status": "success",
                "write_mode": "repo_owned",
                "size": "small",
                "risk": "low",
                "verify_cost": "fast",
                "wall_clock_seconds": 900,
                "verification_seconds": 30,
                "estimate_point_minutes": 0,
                "estimate_upper_minutes": 0,
            },
            {
                "run_id": "exact-2",
                "slice_id": "exact-2",
                "recorded_at": "2026-04-02T00:00:00+00:00",
                "status": "failed",
                "write_mode": "repo_owned",
                "size": "small",
                "risk": "low",
                "verify_cost": "fast",
                "wall_clock_seconds": 1200,
                "verification_seconds": 45,
                "estimate_point_minutes": 0,
                "estimate_upper_minutes": 0,
            },
            {
                "run_id": "exact-3",
                "slice_id": "exact-3",
                "recorded_at": "2026-04-03T00:00:00+00:00",
                "status": "success",
                "write_mode": "repo_owned",
                "size": "small",
                "risk": "low",
                "verify_cost": "fast",
                "wall_clock_seconds": 1500,
                "verification_seconds": 60,
                "estimate_point_minutes": 0,
                "estimate_upper_minutes": 0,
            },
            {
                "run_id": "exact-4",
                "slice_id": "exact-4",
                "recorded_at": "2026-04-04T00:00:00+00:00",
                "status": "success",
                "write_mode": "repo_owned",
                "size": "small",
                "risk": "low",
                "verify_cost": "fast",
                "wall_clock_seconds": 1800,
                "verification_seconds": 75,
                "estimate_point_minutes": 0,
                "estimate_upper_minutes": 0,
            },
        ]
        for index in range(20):
            samples.append(
                {
                    "run_id": f"partial-{index}",
                    "slice_id": f"partial-{index}",
                    "recorded_at": f"2026-05-{index + 1:02d}T00:00:00+00:00",
                    "status": "success",
                    "write_mode": "repo_owned",
                    "size": "small",
                    "risk": "medium",
                    "verify_cost": "fast",
                    "wall_clock_seconds": 600,
                    "verification_seconds": 20,
                    "estimate_point_minutes": 0,
                    "estimate_upper_minutes": 0,
                }
            )
        metrics_path.write_text(
            json.dumps(
                {
                    "repo_root": str(repo),
                    "fingerprint": fingerprint,
                    "samples": samples,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        estimate = afk_mode.estimate_candidates(run_dir)["slices"][0]

        self.assertEqual(estimate["source"], "local_exact")
        self.assertEqual(estimate["samples_used"], 4)
        self.assertEqual(estimate["point_minutes"], 22.5)

    def test_open_slice_returns_estimate_warning_and_status_surfaces_estimates(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        started = afk_mode.start_run(repo, "30m", self.run_root)
        run_dir = Path(started["run_dir"])
        self.write_candidate_queue(
            run_dir,
            {
                "slice_id": "slice-over",
                "ordinal": 1,
                "title": "High effort",
            },
            {
                "slice_id": "slice-next",
                "ordinal": 2,
                "title": "Lower effort",
            },
        )
        self.write_candidates_meta(
            run_dir,
            {
                "slice_id": "slice-over",
                "size": "large",
                "risk": "high",
                "verify_cost": "slow",
            },
            {
                "slice_id": "slice-next",
                "size": "small",
                "risk": "low",
                "verify_cost": "fast",
            },
        )
        self.write_plan_artifacts(run_dir, "slice-over", include_frozen_plan=True)

        active = afk_mode.open_slice(run_dir, "slice-over", 1, "over")

        self.assertEqual(active["estimate"]["fit_status"], "over")
        self.assertIsNotNone(active["estimate_warning"])

        status_payload = afk_mode.status(run_dir)
        self.assertEqual(status_payload["active_slice_estimate"]["slice_id"], "slice-over")
        self.assertEqual(status_payload["next_slice_estimate"]["slice_id"], "slice-next")
        self.assertEqual(status_payload["next_slice_estimate"]["source"], "baseline")

    def test_record_slice_appends_success_and_failed_terminal_samples(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")

        started_success = afk_mode.start_run(repo, "2h", self.run_root)
        success_dir = Path(started_success["run_dir"])
        self.rank_candidates(success_dir, slice_id="slice-success")
        self.write_candidates_meta(
            success_dir,
            {
                "slice_id": "slice-success",
                "size": "small",
                "risk": "low",
                "verify_cost": "fast",
            },
        )
        active_success = afk_mode.open_slice(success_dir, "slice-success", 1, "success")
        success_worktree = Path(active_success["worktree"])
        (success_worktree / "tracked.txt").write_text("success\n", encoding="utf-8")
        self.git(success_worktree, "add", "tracked.txt")
        self.git(success_worktree, "commit", "-m", "Success slice")
        success_commit = self.git(success_worktree, "rev-parse", "HEAD").strip()
        afk_mode.verify_slice(success_dir, "slice-success")
        afk_mode.record_slice(
            success_dir,
            "slice-success",
            "success",
            "Successful slice",
            active_success["branch"],
            success_commit,
            str(success_worktree),
            None,
            None,
            [],
        )
        afk_mode.finish_run(success_dir, "completed", "done")

        started_failed = afk_mode.start_run(repo, "2h", self.run_root)
        failed_dir = Path(started_failed["run_dir"])
        self.rank_candidates(failed_dir, slice_id="slice-failed")
        self.write_candidates_meta(
            failed_dir,
            {
                "slice_id": "slice-failed",
                "size": "small",
                "risk": "low",
                "verify_cost": "fast",
            },
        )
        afk_mode.open_slice(failed_dir, "slice-failed", 1, "failed")
        afk_mode.record_slice(
            failed_dir,
            "slice-failed",
            "failed",
            "Failed slice",
            None,
            None,
            None,
            None,
            str(failed_dir / "logs" / "failed.log"),
            [],
        )

        fingerprint = afk_mode.default_profile_path(repo, self.root / ".codex" / "repo-profiles").stem
        metrics_path = self.root / ".codex" / "afk-metrics" / f"{fingerprint}.json"
        payload = json.loads(metrics_path.read_text(encoding="utf-8"))
        statuses = {sample["status"] for sample in payload["samples"]}

        self.assertEqual(statuses, {"success", "failed"})
        self.assertTrue(all(sample["size"] == "small" for sample in payload["samples"]))
        self.assertTrue(all("residual_seconds" in sample for sample in payload["samples"]))
        self.assertTrue(
            all(
                sample["residual_seconds"] >= 0
                and sample["residual_seconds"] <= sample["wall_clock_seconds"]
                for sample in payload["samples"]
            )
        )

    def test_discover_accepts_root_fallback_checked_in_profile(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        (repo / ".codex").write_text("", encoding="utf-8")
        self.write_profile(repo, relative_path=".codex-plugin.yaml")
        self.commit_paths(repo, "SPEC.md", ".codex", ".codex-plugin.yaml", message="Add fallback profile")

        payload = afk_mode.discover_repo(repo)

        self.assertEqual(payload["trust_mode"], afk_mode.TRUST_MODE_TRUSTED)
        self.assertEqual(
            payload["repo_context"]["checked_in_profile_path"],
            str(repo / ".codex-plugin.yaml"),
        )
        self.assertTrue(payload["capability_verdicts"]["isolated_write"]["allowed"])

    def test_build_session_context_ignores_stale_registry_entries(self) -> None:
        repo = self.init_repo()
        registry_path = self.run_root / "active-runs.json"
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(
            json.dumps(
                {
                    "updated_at": None,
                    "runs": {
                        str(repo): {
                            "run_id": "missing-run",
                            "run_dir": str(self.root / "missing-run"),
                            "repo_root": str(repo),
                        }
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        context = afk_mode.build_session_context(repo, run_root=self.run_root)

        self.assertIsNone(context)

    def test_start_run_rejects_corrupted_active_run_registry(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")

        registry_path = self.run_root / "active-runs.json"
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text('{"runs": ', encoding="utf-8")

        with self.assertRaisesRegex(afk_mode.AfkModeError, "Active run registry is corrupted"):
            afk_mode.start_run(repo, "45m", self.run_root)

    def test_registry_missing_runs_field_is_treated_as_corruption(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")

        registry_path = self.run_root / "active-runs.json"
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(
            json.dumps({"updated_at": None}, indent=2) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(afk_mode.AfkModeError, "missing required field 'runs'"):
            afk_mode.start_run(repo, "45m", self.run_root)
        with self.assertRaisesRegex(afk_mode.AfkModeError, "missing required field 'runs'"):
            afk_mode.build_session_context(repo, run_root=self.run_root)

    def test_registry_null_runs_field_is_treated_as_corruption(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")

        registry_path = self.run_root / "active-runs.json"
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(
            json.dumps({"updated_at": None, "runs": None}, indent=2) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(afk_mode.AfkModeError, "field 'runs' must be an object"):
            afk_mode.start_run(repo, "45m", self.run_root)
        with self.assertRaisesRegex(afk_mode.AfkModeError, "field 'runs' must be an object"):
            afk_mode.build_session_context(repo, run_root=self.run_root)

    def test_start_run_rejects_dirty_repo_without_acknowledgement(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        (repo / "tracked.txt").write_text("dirty change\n", encoding="utf-8")

        with self.assertRaisesRegex(afk_mode.AfkModeError, "ack-dirty-head-baseline"):
            afk_mode.start_run(repo, "45m", self.run_root)

        started = afk_mode.start_run(
            repo,
            "45m",
            self.run_root,
            ack_dirty_head_baseline=True,
        )
        self.assertTrue(started["dirty_head_baseline_acknowledged"])

    def test_start_run_requires_explicit_fallback_write(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        (repo / "scripts").mkdir()
        (repo / "scripts" / "verify_change.py").write_text("print('ok')\n", encoding="utf-8")
        self.commit_paths(repo, "SPEC.md", "scripts/verify_change.py", message="Add signals")

        with self.assertRaisesRegex(afk_mode.AfkModeError, "allow-fallback-write"):
            afk_mode.start_run(repo, "30m", self.run_root)

        started = afk_mode.start_run(
            repo,
            "30m",
            self.run_root,
            allow_fallback_write=True,
        )
        self.assertEqual(started["write_mode"], "fallback")
        self.assertTrue(started["write_authorized"])

    def test_begin_run_starts_trusted_repo(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")

        result = afk_mode.begin_run(repo, "30m", self.run_root)

        self.assertTrue(result["started"])
        self.assertFalse(result["blocked"])
        self.assertEqual(result["run"]["trust_mode"], afk_mode.TRUST_MODE_TRUSTED)

    def test_begin_run_accepts_checked_in_profile_truth_order_without_detected_design_docs(self) -> None:
        repo = self.init_repo("profile-truth")
        (repo / "docs").mkdir()
        (repo / "docs" / "IMPLEMENTATION.md").write_text("# Implementation\n", encoding="utf-8")
        self.write_profile(repo)

        profile_path = repo / ".codex" / "plugin-profile.yaml"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["truth"]["order"] = ["docs/IMPLEMENTATION.md"]
        profile_path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
        self.commit_paths(
            repo,
            "docs/IMPLEMENTATION.md",
            ".codex/plugin-profile.yaml",
            message="Add checked-in profile truth order",
        )

        result = afk_mode.begin_run(repo, "30m", self.run_root)

        self.assertTrue(result["started"])
        self.assertFalse(result["blocked"])
        self.assertIn("docs/IMPLEMENTATION.md", result["discovery"]["truth_sources"])

    def test_begin_run_reports_dirty_ack_blocker(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        (repo / "tracked.txt").write_text("dirty change\n", encoding="utf-8")

        result = afk_mode.begin_run(repo, "30m", self.run_root)

        self.assertFalse(result["started"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["blocker_code"], "dirty_head_ack_required")
        self.assertIn("--ack-dirty-head-baseline", result["next_action"])
        self.assertEqual(result["recovery"]["required_approval_type"], "dirty_head_baseline")
        self.assertEqual(
            result["recovery"]["required_verification_route"],
            ['python3 -c "print(\'afk-mode-ok\')"'],
        )
        self.assertEqual(result["recovery"]["current_repo_policy_source"], "repo_owned")

    def test_begin_run_reports_fallback_write_blocker_for_assistive_repo(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        (repo / "scripts").mkdir()
        (repo / "scripts" / "verify_change.py").write_text("print('ok')\n", encoding="utf-8")
        self.commit_paths(repo, "SPEC.md", "scripts/verify_change.py", message="Add signals")

        result = afk_mode.begin_run(repo, "30m", self.run_root)

        self.assertFalse(result["started"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["blocker_code"], "fallback_write_approval_required")
        self.assertEqual(result["discovery"]["trust_mode"], afk_mode.TRUST_MODE_ASSISTIVE)
        self.assertEqual(result["recovery"]["required_repo_policy_source"], "repo_owned")
        self.assertEqual(result["recovery"]["required_approval_type"], "fallback_write")
        self.assertEqual(result["recovery"]["current_write_mode"], "fallback")
        self.assertFalse(result["recovery"]["current_write_authorized"])
        self.assertEqual(result["recovery"]["current_verification_source"], "fallback")
        self.assertIn("verify_change.py", result["recovery"]["required_verification_route"][0])
        self.assertTrue(result["recovery"]["suggested_profile_path"].endswith(".yaml"))

    def test_begin_run_reports_existing_run_recovery_fields(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")

        started = afk_mode.start_run(repo, "30m", self.run_root)
        result = afk_mode.begin_run(repo, "30m", self.run_root)

        self.assertTrue(result["blocked"])
        self.assertEqual(result["blocker_code"], "active_run_exists")
        self.assertEqual(result["recovery"]["existing_run_id"], started["run_id"])
        self.assertEqual(result["recovery"]["existing_run_dir"], started["run_dir"])
        self.assertGreaterEqual(result["recovery"]["existing_remaining_seconds"], 0)

    def test_begin_run_blocker_payload_contract_is_stable(self) -> None:
        dirty_repo = self.init_repo("dirty-repo")
        (dirty_repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(dirty_repo)
        self.commit_paths(
            dirty_repo,
            "SPEC.md",
            ".codex/plugin-profile.yaml",
            message="Add checked-in profile",
        )
        (dirty_repo / "tracked.txt").write_text("dirty change\n", encoding="utf-8")

        dirty_result = afk_mode.begin_run(dirty_repo, "30m", self.run_root)
        self.assert_begin_run_blocker_contract(dirty_result)
        self.assertEqual(dirty_result["blocker_code"], "dirty_head_ack_required")

        fallback_repo = self.init_repo("fallback-repo")
        (fallback_repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        (fallback_repo / "scripts").mkdir()
        (fallback_repo / "scripts" / "verify_change.py").write_text("print('ok')\n", encoding="utf-8")
        self.commit_paths(
            fallback_repo,
            "SPEC.md",
            "scripts/verify_change.py",
            message="Add fallback signals",
        )

        fallback_result = afk_mode.begin_run(fallback_repo, "30m", self.run_root)
        self.assert_begin_run_blocker_contract(fallback_result)
        self.assertEqual(fallback_result["blocker_code"], "fallback_write_approval_required")

        profile_repo = self.init_repo("profile-repo")
        (profile_repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(profile_repo, allow_isolated_write=False)
        self.commit_paths(
            profile_repo,
            "SPEC.md",
            ".codex/plugin-profile.yaml",
            message="Add checked-in profile without isolated write",
        )

        profile_result = afk_mode.begin_run(
            profile_repo,
            "30m",
            self.run_root,
            allow_fallback_write=True,
        )
        self.assert_begin_run_blocker_contract(profile_result)
        self.assertEqual(profile_result["blocker_code"], "profile_required")

        active_repo = self.init_repo("active-repo")
        (active_repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(active_repo)
        self.commit_paths(
            active_repo,
            "SPEC.md",
            ".codex/plugin-profile.yaml",
            message="Add checked-in profile",
        )
        afk_mode.start_run(active_repo, "30m", self.run_root)

        active_result = afk_mode.begin_run(active_repo, "30m", self.run_root)
        self.assert_begin_run_blocker_contract(active_result)
        self.assertEqual(active_result["blocker_code"], "active_run_exists")

    def test_checked_in_profile_caps_overlay_write_capability(self) -> None:
        repo = self.init_repo()
        overlay_root = self.root / "overlay-profiles"
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo, allow_isolated_write=False)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add checked-in profile")

        overlay_path = afk_mode.default_profile_path(repo, overlay_root)
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        overlay_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo_id": repo.name,
                    "display_name": repo.name,
                    "truth": {"order": ["SPEC.md"]},
                    "verify": {"commands": ["false"]},
                    "workflow": {"status_roots": [".codex/work-items"], "execution_entrypoints": ["custom"]},
                    "guardrails": {"rules": []},
                    "skills": {"paths": [], "load_command": None},
                    "capabilities": ["read_discovery", "verification_routing", "isolated_write"],
                    "plugin_overrides": {},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        payload = afk_mode.discover_repo(repo, profile_root=overlay_root)

        self.assertEqual(payload["trust_mode"], afk_mode.TRUST_MODE_TRUSTED)
        self.assertFalse(payload["capability_verdicts"]["isolated_write"]["allowed"])
        self.assertEqual(payload["write_mode"], "none")
        self.assertFalse(payload["write_authorized"])
        self.assertEqual(
            payload["repo_context"]["profile"]["verify"]["commands"],
            ['python3 -c "print(\'afk-mode-ok\')"'],
        )
        self.assertEqual(
            payload["repo_context"]["profile"]["workflow"]["status_roots"],
            [".codex/work"],
        )

    def test_begin_run_checked_in_profile_without_isolated_write_blocks_without_fallback(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo, allow_isolated_write=False)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add checked-in profile")

        result = afk_mode.begin_run(repo, "30m", self.run_root, allow_fallback_write=True)

        self.assertTrue(result["blocked"])
        self.assertEqual(result["blocker_code"], "profile_required")
        self.assertEqual(result["discovery"]["trust_mode"], afk_mode.TRUST_MODE_TRUSTED)
        self.assertEqual(result["discovery"]["write_mode"], "none")

    def test_checked_in_profile_freezes_session_override_verify_and_workflow(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo, allow_isolated_write=False)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add checked-in profile")

        override_path = self.root / "profiles" / "session-override.yaml"
        override_path.parent.mkdir(parents=True, exist_ok=True)
        override_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo_id": repo.name,
                    "display_name": repo.name,
                    "truth": {"order": ["SPEC.md"]},
                    "verify": {"commands": ["false"]},
                    "workflow": {
                        "status_roots": [".codex/work-items"],
                        "execution_entrypoints": ["custom workflow"],
                    },
                    "guardrails": {"rules": []},
                    "skills": {"paths": [], "load_command": None},
                    "capabilities": ["read_discovery", "verification_routing", "isolated_write"],
                    "plugin_overrides": {},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        payload = afk_mode.discover_repo(repo, profile_path=override_path)

        self.assertFalse(payload["capability_verdicts"]["isolated_write"]["allowed"])
        self.assertEqual(payload["write_mode"], "none")
        self.assertEqual(
            payload["repo_context"]["profile"]["verify"]["commands"],
            ['python3 -c "print(\'afk-mode-ok\')"'],
        )
        self.assertEqual(
            payload["repo_context"]["profile"]["workflow"]["status_roots"],
            [".codex/work"],
        )

    def test_checked_in_profile_rejects_legacy_guardrails(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        profile_path = repo / ".codex" / "plugin-profile.yaml"
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo_id": repo.name,
                    "display_name": repo.name,
                    "truth": {"order": ["SPEC.md"]},
                    "verify": {"commands": ['python3 -c "print(\'ok\')"']},
                    "workflow": {"status_roots": [".codex/work"], "execution_entrypoints": []},
                    "guardrails": {"ask_first": ["git push"]},
                    "skills": {"paths": [], "load_command": None},
                    "capabilities": ["read_discovery", "verification_routing", "session_guardrails"],
                    "plugin_overrides": {},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add legacy guardrails")

        with self.assertRaisesRegex(afk_mode.AfkModeError, "guardrails.rules"):
            afk_mode.discover_repo(repo)

    def test_overlay_legacy_guardrails_are_converted(self) -> None:
        repo = self.init_repo()
        overlay_root = self.root / "overlay-profiles"
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.commit_paths(repo, "SPEC.md", message="Add spec")

        overlay_path = afk_mode.default_profile_path(repo, overlay_root)
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        overlay_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo_id": repo.name,
                    "display_name": repo.name,
                    "truth": {"order": ["SPEC.md"]},
                    "verify": {"commands": ['python3 -c "print(\'ok\')"']},
                    "workflow": {"status_roots": [".codex/work"], "execution_entrypoints": []},
                    "guardrails": {
                        "ask_first": ["git push"],
                        "blocked_commands": ["git config --global"],
                        "blocked_paths": ["restricted"],
                    },
                    "skills": {"paths": [], "load_command": None},
                    "capabilities": ["read_discovery", "verification_routing", "isolated_write", "session_guardrails"],
                    "plugin_overrides": {},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        payload = afk_mode.discover_repo(repo, profile_root=overlay_root)
        guardrails = payload["repo_context"]["profile"]["guardrails"]

        self.assertTrue(guardrails["legacy_guardrails_converted"])
        self.assertEqual(len(guardrails["rules"]), 3)
        self.assertEqual(len(guardrails["converted_rule_ids"]), 3)
        self.assertEqual(payload["write_mode"], "none")
        self.assertFalse(payload["write_authorized"])
        self.assertTrue(
            any(rule["approval_scope"] == "exact_command_once" for rule in guardrails["rules"] if rule["action"] == "ask_first")
        )

    def test_local_overlay_cannot_grant_write_capability(self) -> None:
        repo = self.init_repo()
        overlay_root = self.root / "overlay-profiles"
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        (repo / "module.py").write_text("value = 1\n", encoding="utf-8")
        self.commit_paths(repo, "SPEC.md", "module.py", message="Add fallback signals")

        overlay_path = afk_mode.default_profile_path(repo, overlay_root)
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        overlay_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo_id": repo.name,
                    "display_name": repo.name,
                    "truth": {"order": ["SPEC.md"]},
                    "verify": {"commands": ['python3 -c "print(\'overlay\')"']},
                    "workflow": {"status_roots": [".codex/work"], "execution_entrypoints": []},
                    "guardrails": {"rules": []},
                    "skills": {"paths": [], "load_command": None},
                    "capabilities": [
                        "read_discovery",
                        "verification_routing",
                        "isolated_write",
                        "session_guardrails",
                    ],
                    "plugin_overrides": {},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        payload = afk_mode.discover_repo(repo, profile_root=overlay_root)

        self.assertEqual(payload["policy_source"], "local_overlay")
        self.assertEqual(payload["write_mode"], "fallback")
        self.assertFalse(payload["write_authorized"])
        self.assertFalse(payload["capability_verdicts"]["isolated_write"]["allowed"])

    def test_duplicate_guardrail_rule_ids_across_layers_fail(self) -> None:
        repo = self.init_repo()
        overlay_root = self.root / "overlay-profiles"
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(
            repo,
            guardrails={
                "rules": [
                    {
                        "id": "shared-rule",
                        "action": "deny",
                        "match_type": "category",
                        "category": "global_git_config",
                    }
                ]
            },
        )
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add checked-in guardrail")

        overlay_path = afk_mode.default_profile_path(repo, overlay_root)
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        overlay_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repo_id": repo.name,
                    "display_name": repo.name,
                    "truth": {"order": ["SPEC.md"]},
                    "verify": {"commands": ['python3 -c "print(\'ok\')"']},
                    "workflow": {"status_roots": [".codex/work"], "execution_entrypoints": []},
                    "guardrails": {
                        "rules": [
                            {
                                "id": "shared-rule",
                                "action": "ask_first",
                                "match_type": "category",
                                "category": "git_push",
                                "approval_scope": "rule_for_run",
                            }
                        ]
                    },
                    "skills": {"paths": [], "load_command": None},
                    "capabilities": ["read_discovery", "verification_routing", "isolated_write", "session_guardrails"],
                    "plugin_overrides": {},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(afk_mode.AfkModeError, "Duplicate guardrail rule id"):
            afk_mode.discover_repo(repo, profile_root=overlay_root)

    def test_start_run_ids_are_unique(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")

        first = afk_mode.start_run(repo, "45m", self.run_root)
        afk_mode.finish_run(Path(first["run_dir"]), "completed", "First run closed")
        second = afk_mode.start_run(repo, "45m", self.run_root)

        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(first["run_dir"], second["run_dir"])

    def test_start_run_enables_hooks_and_finish_run_disables_them(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")

        started = afk_mode.start_run(repo, "2h", self.run_root)
        hooks_payload = self.load_hooks()
        self.assertIn("SessionStart", hooks_payload["hooks"])
        self.assertIn("PreToolUse", hooks_payload["hooks"])
        self.assertIn("Stop", hooks_payload["hooks"])
        for event_name, spec in hook_config.AFK_HOOK_SPECS.items():
            registered_entries = hooks_payload["hooks"][event_name]
            self.assertTrue(registered_entries)
            registered_command = registered_entries[0]["hooks"][0]["command"]
            self.assertEqual(registered_command, spec["hooks"][0]["command"])
            self.assertTrue(Path(registered_command.split(maxsplit=1)[1]).exists())

        afk_mode.finish_run(Path(started["run_dir"]), "completed", "Done")
        hooks_payload = self.load_hooks()
        self.assertEqual(hooks_payload["hooks"], {})

    def test_hooks_stay_enabled_until_last_active_run_finishes(self) -> None:
        repo_one = self.init_repo("repo-one")
        (repo_one / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo_one)
        self.commit_paths(repo_one, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec one")

        repo_two = self.init_repo("repo-two")
        (repo_two / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo_two)
        self.commit_paths(repo_two, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec two")

        first = afk_mode.start_run(repo_one, "45m", self.run_root)
        second = afk_mode.start_run(repo_two, "45m", self.run_root)

        afk_mode.finish_run(Path(first["run_dir"]), "completed", "First done")
        hooks_payload = self.load_hooks()
        self.assertIn("Stop", hooks_payload["hooks"])

        afk_mode.finish_run(Path(second["run_dir"]), "completed", "Second done")
        hooks_payload = self.load_hooks()
        self.assertEqual(hooks_payload["hooks"], {})

    def test_afk_hook_sync_preserves_unrelated_hooks(self) -> None:
        self.hooks_path.parent.mkdir(parents=True, exist_ok=True)
        self.hooks_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "python3 /tmp/custom-stop.py",
                                        "statusMessage": "Custom stop",
                                    }
                                ]
                            }
                        ]
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")

        started = afk_mode.start_run(repo, "2h", self.run_root)
        hooks_payload = self.load_hooks()
        stop_commands = [
            hook["command"]
            for entry in hooks_payload["hooks"]["Stop"]
            for hook in entry["hooks"]
        ]
        self.assertIn("python3 /tmp/custom-stop.py", stop_commands)

        afk_mode.finish_run(Path(started["run_dir"]), "completed", "Done")
        hooks_payload = self.load_hooks()
        self.assertEqual(
            hooks_payload["hooks"]["Stop"][0]["hooks"][0]["command"],
            "python3 /tmp/custom-stop.py",
        )

    def test_start_run_rejects_second_active_run_for_same_repo(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")

        first = afk_mode.start_run(repo, "45m", self.run_root)

        with self.assertRaisesRegex(afk_mode.AfkModeError, "already has an active afk run"):
            afk_mode.start_run(repo, "45m", self.run_root)

        afk_mode.finish_run(Path(first["run_dir"]), "completed", "First run closed")
        second = afk_mode.start_run(repo, "45m", self.run_root)
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_save_run_and_register_cleans_up_run_dir_when_registration_fails(self) -> None:
        repo = self.init_repo("register-failure")
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")

        discovery = afk_mode.discover_repo(repo)
        run_id, run_dir = kernel_run.create_run_artifacts(self.run_root, discovery)

        with mock.patch.object(
            kernel_run,
            "register_active_run",
            side_effect=afk_mode.AfkModeError("registration failed"),
        ):
            with self.assertRaisesRegex(afk_mode.AfkModeError, "registration failed"):
                kernel_run.save_run_and_register(
                    self.run_root,
                    repo,
                    run_dir,
                    {
                        "run_id": run_id,
                        "repo_root": str(repo),
                    },
                )

        self.assertFalse(run_dir.exists())
        self.assertFalse(self.hooks_path.exists())

    def test_start_open_status_record_finish_and_cleanup(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        (repo / "docs").mkdir()
        (repo / "docs" / "product-mvp.md").write_text("# MVP\n", encoding="utf-8")
        self.write_status(repo, "20260408-open", phase="worker", summary="Open summary")
        self.write_profile(repo)
        self.commit_paths(
            repo,
            "SPEC.md",
            "docs/product-mvp.md",
            ".codex/plugin-profile.yaml",
            ".codex/work/20260408-open/STATUS.json",
            message="Add planning docs",
        )

        started = afk_mode.start_run(repo, "2h", self.run_root)
        run_dir = Path(started["run_dir"])
        self.assertTrue((self.run_root / "active-runs.json").exists())
        self.assertEqual(started["trust_mode"], afk_mode.TRUST_MODE_TRUSTED)
        self.rank_candidates(run_dir)

        active = afk_mode.open_slice(run_dir, "slice-one", 1, "first-slice")
        worktree = Path(active["worktree"])
        self.assertTrue(worktree.exists())
        status_payload = afk_mode.status(run_dir)
        self.assertEqual(status_payload["active_slice"]["slice_id"], "slice-one")
        self.assertGreater(status_payload["remaining_seconds"], 0)
        self.assertEqual(status_payload["policy_source"], "repo_owned")
        self.assertEqual(status_payload["write_mode"], "repo_owned")
        self.assertTrue(status_payload["write_authorized"])
        self.assertEqual(status_payload["verification_source"], "repo_owned")
        self.assertEqual(
            status_payload["verification_route"],
            ['python3 -c "print(\'afk-mode-ok\')"'],
        )
        self.assertEqual(
            status_payload["guardrails"],
            {"rules": [], "legacy_guardrails_converted": False, "converted_rule_ids": []},
        )
        self.assertEqual(status_payload["guardrail_approvals"], [])

        (worktree / "tracked.txt").write_text("changed\n", encoding="utf-8")
        self.git(worktree, "add", "tracked.txt")
        self.git(worktree, "commit", "-m", "Slice one")
        commit_sha = self.git(worktree, "rev-parse", "HEAD").strip()
        verification_result = afk_mode.verify_slice(run_dir, "slice-one")
        self.assertTrue(verification_result["all_passed"])
        self.assertTrue(afk_mode.verification_result_path(run_dir, "slice-one").exists())

        recorded = afk_mode.record_slice(
            run_dir,
            "slice-one",
            "success",
            "Implemented slice one",
            active["branch"],
            commit_sha,
            str(worktree),
            None,
            str(run_dir / "logs" / "slice-one.log"),
            [],
        )
        self.assertEqual(recorded["status"], "success")
        self.assertEqual(
            recorded["verification_result"],
            afk_mode.relative_to(afk_mode.verification_result_path(run_dir, "slice-one"), run_dir),
        )
        self.assertIsNone(afk_mode.load_run(run_dir)["active_slice"])

        cleanup = afk_mode.cleanup_run(run_dir)
        self.assertIn(str(worktree), cleanup["removed"])
        self.assertFalse(worktree.exists())

        finished = afk_mode.finish_run(run_dir, "completed", "All done")
        self.assertEqual(finished["status"], "completed")
        registry = json.loads((self.run_root / "active-runs.json").read_text(encoding="utf-8"))
        self.assertEqual(registry["runs"], {})

    def test_open_slice_requires_ranked_candidates_and_remaining_budget(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        started = afk_mode.start_run(repo, "30m", self.run_root)
        run_dir = Path(started["run_dir"])

        with self.assertRaisesRegex(afk_mode.AfkModeError, "was not found in candidates.json"):
            afk_mode.open_slice(run_dir, "slice-gated", 1, "gated")

        self.rank_candidates(run_dir, slice_id="slice-gated")
        run_payload = afk_mode.load_run(run_dir)
        run_payload["started_at"] = "2000-01-01T00:00:00+00:00"
        afk_mode.save_run(run_dir, run_payload)

        with self.assertRaisesRegex(afk_mode.AfkModeError, "budget is exhausted"):
            afk_mode.open_slice(run_dir, "slice-gated", 1, "gated")

    def test_record_success_requires_audit_fields(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        started = afk_mode.start_run(repo, "30m", self.run_root)
        run_dir = Path(started["run_dir"])

        with self.assertRaisesRegex(afk_mode.AfkModeError, "branch name"):
            afk_mode.record_slice(
                run_dir,
                "slice-missing-branch",
                "success",
                "Missing branch",
                None,
                "deadbeef",
                None,
                None,
                None,
                ["pytest"],
            )

        self.rank_candidates(run_dir, slice_id="slice-proof")
        active = afk_mode.open_slice(run_dir, "slice-proof", 1, "proof")
        worktree = Path(active["worktree"])
        (worktree / "tracked.txt").write_text("proof\n", encoding="utf-8")
        self.git(worktree, "add", "tracked.txt")
        self.git(worktree, "commit", "-m", "Proof commit")
        commit_sha = self.git(worktree, "rev-parse", "HEAD").strip()

        with self.assertRaisesRegex(afk_mode.AfkModeError, "verification result artifact"):
            afk_mode.record_slice(
                run_dir,
                "slice-proof",
                "success",
                "Missing proof",
                active["branch"],
                commit_sha,
                str(worktree),
                None,
                None,
                [],
            )

    def test_record_success_rejects_mismatched_proof_commands(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        started = afk_mode.start_run(repo, "30m", self.run_root)
        run_dir = Path(started["run_dir"])
        self.rank_candidates(run_dir, slice_id="slice-mismatch")
        active = afk_mode.open_slice(run_dir, "slice-mismatch", 1, "mismatch")
        worktree = Path(active["worktree"])
        (worktree / "tracked.txt").write_text("changed\n", encoding="utf-8")
        self.git(worktree, "add", "tracked.txt")
        self.git(worktree, "commit", "-m", "Slice mismatch")
        commit_sha = self.git(worktree, "rev-parse", "HEAD").strip()

        afk_mode.verify_slice(run_dir, "slice-mismatch")

        with self.assertRaisesRegex(afk_mode.AfkModeError, "do not match the verification result artifact"):
            afk_mode.record_slice(
                run_dir,
                "slice-mismatch",
                "success",
                "Mismatch",
                active["branch"],
                commit_sha,
                str(worktree),
                None,
                None,
                ["false"],
            )

    def test_record_failed_requires_patch_or_log(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        started = afk_mode.start_run(repo, "30m", self.run_root)
        run_dir = Path(started["run_dir"])

        with self.assertRaisesRegex(afk_mode.AfkModeError, "patch path or a log path"):
            afk_mode.record_slice(
                run_dir,
                "slice-failed",
                "failed",
                "Failed without evidence",
                None,
                None,
                None,
                None,
                None,
                [],
            )

    def test_record_success_requires_verified_branch_tip_and_head(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        started = afk_mode.start_run(repo, "30m", self.run_root)
        run_dir = Path(started["run_dir"])
        self.rank_candidates(run_dir, slice_id="slice-tip")
        active = afk_mode.open_slice(run_dir, "slice-tip", 1, "tip")
        worktree = Path(active["worktree"])

        (worktree / "tracked.txt").write_text("first\n", encoding="utf-8")
        self.git(worktree, "add", "tracked.txt")
        self.git(worktree, "commit", "-m", "First verified commit")
        first_commit = self.git(worktree, "rev-parse", "HEAD").strip()

        verification_result = afk_mode.verify_slice(run_dir, "slice-tip")
        self.assertEqual(verification_result["verified_head"], first_commit)
        self.assertEqual(verification_result["verified_branch"], active["branch"])

        (worktree / "tracked.txt").write_text("second\n", encoding="utf-8")
        self.git(worktree, "add", "tracked.txt")
        self.git(worktree, "commit", "-m", "Advance branch after verification")
        second_commit = self.git(worktree, "rev-parse", "HEAD").strip()

        with self.assertRaisesRegex(afk_mode.AfkModeError, "must match the tip of branch"):
            afk_mode.record_slice(
                run_dir,
                "slice-tip",
                "success",
                "Old commit is no longer the branch tip",
                active["branch"],
                first_commit,
                str(worktree),
                None,
                None,
                [],
            )

        with self.assertRaisesRegex(afk_mode.AfkModeError, "does not match the commit that was verified"):
            afk_mode.record_slice(
                run_dir,
                "slice-tip",
                "success",
                "New branch tip was not re-verified",
                active["branch"],
                second_commit,
                str(worktree),
                None,
                None,
                [],
            )

    def test_save_patch_and_hook_helpers(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        started = afk_mode.start_run(repo, "30m", self.run_root)
        run_dir = Path(started["run_dir"])
        self.rank_candidates(run_dir, slice_id="slice-two")
        active = afk_mode.open_slice(run_dir, "slice-two", 2, "guarded")
        worktree = Path(active["worktree"])
        (worktree / "tracked.txt").write_text("modified\n", encoding="utf-8")
        (worktree / "extra.txt").write_text("new\n", encoding="utf-8")

        patch_result = afk_mode.save_patch(
            worktree,
            run_dir / "patches" / "slice-two.patch",
            include_untracked=True,
            run_dir=run_dir,
        )
        self.assertGreater(patch_result["bytes"], 0)

        context = afk_mode.build_session_context(repo, run_root=self.run_root)
        self.assertEqual(context["run_id"], started["run_id"])
        decision = afk_mode.pretool_decision(
            repo,
            "git reset --hard HEAD",
            run_root=self.run_root,
        )
        self.assertEqual(
            decision["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        reminder = afk_mode.pretool_decision(
            repo,
            "git status --short",
            run_root=self.run_root,
        )
        self.assertIn("Prefer running Bash inside that worktree", reminder["systemMessage"])

    def test_typed_guardrails_enforce_denies_and_dual_approval_scopes(self) -> None:
        repo = self.init_repo()
        restricted_dir = repo / "restricted"
        restricted_dir.mkdir()
        (restricted_dir / "data.txt").write_text("secret\n", encoding="utf-8")
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(
            repo,
            guardrails={
                "rules": [
                    {
                        "id": "ask-push",
                        "title": "Push changes",
                        "action": "ask_first",
                        "match_type": "category",
                        "category": "git_push",
                        "approval_scope": "rule_for_run",
                    },
                    {
                        "id": "deny-global-git-config",
                        "action": "deny",
                        "match_type": "category",
                        "category": "global_git_config",
                    },
                    {
                        "id": "deny-restricted-path",
                        "action": "deny",
                        "match_type": "path",
                        "pattern": "restricted",
                    },
                    {
                        "id": "ask-release",
                        "title": "Release command",
                        "action": "ask_first",
                        "match_type": "command_substring",
                        "pattern": "npm run release",
                        "approval_scope": "exact_command_once",
                    },
                ],
            },
        )
        self.commit_paths(
            repo,
            "SPEC.md",
            ".codex/plugin-profile.yaml",
            "restricted/data.txt",
            message="Add spec and guardrails",
        )
        started = afk_mode.start_run(repo, "30m", self.run_root)
        run_dir = Path(started["run_dir"])

        blocked_command = afk_mode.pretool_decision(
            repo,
            'git config --global user.name "Test"',
            run_root=self.run_root,
        )
        self.assertEqual(blocked_command["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(
            blocked_command["hookSpecificOutput"]["guardrail"]["rule_id"],
            "deny-global-git-config",
        )

        blocked_path = afk_mode.pretool_decision(
            restricted_dir,
            "git status --short",
            run_root=self.run_root,
        )
        self.assertEqual(blocked_path["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(
            blocked_path["hookSpecificOutput"]["guardrail"]["rule_id"],
            "deny-restricted-path",
        )

        push_guardrail = afk_mode.pretool_decision(
            repo,
            "git push origin HEAD",
            run_root=self.run_root,
        )
        self.assertEqual(push_guardrail["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(push_guardrail["hookSpecificOutput"]["guardrail"]["rule_id"], "ask-push")
        self.assertIn(str(Path(__file__).resolve().parents[1] / "afk-mode"), push_guardrail["systemMessage"])
        self.assertIn("--rule-id ask-push", push_guardrail["systemMessage"])

        approval = afk_mode.approve_guardrail(
            run_dir,
            rule_id="ask-push",
            reason="user approved",
        )
        self.assertTrue(approval["approved"])
        self.assertIsNone(
            afk_mode.pretool_decision(
                repo,
                "git push origin HEAD",
                run_root=self.run_root,
            )
        )
        self.assertIsNone(
            afk_mode.pretool_decision(
                repo,
                "git push origin main",
                run_root=self.run_root,
            )
        )

        release_guardrail = afk_mode.pretool_decision(
            repo,
            "npm run release",
            run_root=self.run_root,
        )
        self.assertEqual(release_guardrail["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(release_guardrail["hookSpecificOutput"]["guardrail"]["rule_id"], "ask-release")
        self.assertIn(str(Path(__file__).resolve().parents[1] / "afk-mode"), release_guardrail["systemMessage"])
        self.assertIn("--approved-command", release_guardrail["systemMessage"])

        release_approval = afk_mode.approve_guardrail(
            run_dir,
            "npm run release",
            reason="legacy alias approval",
        )
        self.assertTrue(release_approval["deprecated_command_alias_used"])
        self.assertIsNone(
            afk_mode.pretool_decision(
                repo,
                "npm run release",
                run_root=self.run_root,
            )
        )
        self.assertEqual(
            afk_mode.pretool_decision(
                repo,
                "npm run release",
                run_root=self.run_root,
            )["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        status_payload = afk_mode.status(run_dir)
        self.assertEqual(status_payload["guardrail_approvals"][0]["rule_id"], "ask-push")
        self.assertEqual(status_payload["guardrail_approvals"][0]["approval_scope"], "rule_for_run")

    def test_cli_approve_guardrail_by_rule_id(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(
            repo,
            guardrails={
                "rules": [
                    {
                        "id": "ask-release",
                        "action": "ask_first",
                        "match_type": "command_substring",
                        "pattern": "npm run release",
                        "approval_scope": "exact_command_once",
                    }
                ]
            },
        )
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add CLI guardrail")

        started = afk_mode.start_run(repo, "30m", self.run_root)
        run_dir = Path(started["run_dir"])
        initial = afk_mode.pretool_decision(repo, "npm run release", run_root=self.run_root)
        self.assertEqual(initial["hookSpecificOutput"]["guardrail"]["rule_id"], "ask-release")

        result = self.run_cli(
            "approve-guardrail",
            "--run-dir",
            str(run_dir),
            "--rule-id",
            "ask-release",
            "--approved-command",
            "npm run release",
            "--reason",
            "cli approval",
        )
        payload = json.loads(result.stdout)

        self.assertTrue(payload["approved"])
        self.assertEqual(payload["rule_id"], "ask-release")
        self.assertIsNone(
            afk_mode.pretool_decision(
                repo,
                "npm run release",
                run_root=self.run_root,
            )
        )

    def test_cli_repair_registry_resets_corrupt_payload_when_requested(self) -> None:
        registry_path = self.run_root / "active-runs.json"
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text('{"runs": ', encoding="utf-8")

        result = self.run_cli(
            "repair-registry",
            "--run-root",
            str(self.run_root),
            "--reset-corrupt",
        )
        payload = json.loads(result.stdout)

        self.assertEqual(payload["status"], "reset_corrupt")
        self.assertTrue(Path(payload["backup"]).exists())
        repaired = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertEqual(repaired["runs"], {})

    def test_save_patch_requires_active_slice_worktree_and_run_patch_dir(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        started = afk_mode.start_run(repo, "30m", self.run_root)
        run_dir = Path(started["run_dir"])
        self.rank_candidates(run_dir, slice_id="slice-guard")
        active = afk_mode.open_slice(run_dir, "slice-guard", 1, "guard")
        worktree = Path(active["worktree"])

        with self.assertRaisesRegex(afk_mode.AfkModeError, "requires --run-dir"):
            afk_mode.save_patch(
                worktree,
                run_dir / "patches" / "missing-run-dir.patch",
                include_untracked=True,
            )
        with self.assertRaisesRegex(afk_mode.AfkModeError, "active slice worktree"):
            afk_mode.save_patch(
                repo,
                run_dir / "patches" / "wrong-root.patch",
                include_untracked=True,
                run_dir=run_dir,
            )
        with self.assertRaisesRegex(afk_mode.AfkModeError, "run patches directory"):
            afk_mode.save_patch(
                worktree,
                self.root / "escape.patch",
                include_untracked=True,
                run_dir=run_dir,
            )
        with self.assertRaisesRegex(afk_mode.AfkModeError, "No changes found"):
            afk_mode.save_patch(
                worktree,
                run_dir / "patches" / "empty.patch",
                include_untracked=True,
                run_dir=run_dir,
            )

        (worktree / "tracked.txt").write_text("patched\n", encoding="utf-8")
        patch_result = afk_mode.save_patch(
            worktree,
            run_dir / "patches" / "guard.patch",
            include_untracked=True,
            run_dir=run_dir,
        )
        self.assertEqual(patch_result["run_dir"], str(run_dir))
        self.assertGreater(patch_result["bytes"], 0)

    def test_bootstrap_profile_rejects_isolated_write_for_local_overlay(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        (repo / "scripts").mkdir()
        (repo / "scripts" / "verify_change.py").write_text("print('ok')\n", encoding="utf-8")
        self.commit_paths(repo, "SPEC.md", "scripts/verify_change.py", message="Add bootstrap signals")

        output = self.root / "profiles" / "repo.yaml"
        with self.assertRaisesRegex(afk_mode.AfkModeError, "only valid when writing a checked-in repo profile"):
            afk_mode.bootstrap_profile(
                repo,
                output=output,
                allow_isolated_write=True,
            )

    def test_bootstrap_checked_in_profile_keeps_heuristics_as_hints(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        (repo / "scripts").mkdir()
        (repo / "scripts" / "verify_change.py").write_text("print('ok')\n", encoding="utf-8")
        self.commit_paths(repo, "SPEC.md", "scripts/verify_change.py", message="Add bootstrap hints")

        output = repo / ".codex" / "plugin-profile.yaml"
        result = afk_mode.bootstrap_profile(
            repo,
            output=output,
            allow_isolated_write=True,
            force=True,
        )

        self.assertTrue(output.exists())
        self.assertEqual(result["trust_mode"], afk_mode.TRUST_MODE_ASSISTIVE)
        profile = yaml.safe_load(output.read_text(encoding="utf-8"))
        self.assertEqual(profile["verify"]["commands"], [])
        self.assertEqual(profile["workflow"]["execution_entrypoints"], [])
        self.assertNotIn("isolated_write", profile["capabilities"])
        hints = profile["plugin_overrides"]["afk_mode"]["discovery_hints"]
        self.assertIn("verification_commands", hints)
        self.assertIn("python3 scripts/verify_change.py --profile harness", hints["verification_commands"])

    def test_fallback_write_runs_with_generic_verification_and_status_metadata(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        (repo / "scripts").mkdir()
        (repo / "scripts" / "verify_change.py").write_text("print('ok')\n", encoding="utf-8")
        self.commit_paths(repo, "SPEC.md", "scripts/verify_change.py", message="Add fallback signals")

        result = afk_mode.begin_run(repo, "30m", self.run_root, allow_fallback_write=True)

        self.assertTrue(result["started"])
        self.assertEqual(result["run"]["write_mode"], "fallback")
        self.assertTrue(result["run"]["write_authorized"])
        self.assertEqual(result["run"]["verification_source"], "fallback")

        run_dir = Path(result["run"]["run_dir"])
        status_payload = afk_mode.status(run_dir)
        self.assertEqual(status_payload["write_mode"], "fallback")
        self.assertTrue(status_payload["write_authorized"])
        self.assertEqual(status_payload["verification_source"], "fallback")
        self.assertIn("verify_change.py", status_payload["verification_route"][0])

    def test_fallback_pretool_denies_structural_git_commands(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        (repo / "scripts").mkdir()
        (repo / "scripts" / "verify_change.py").write_text("print('ok')\n", encoding="utf-8")
        self.commit_paths(repo, "SPEC.md", "scripts/verify_change.py", message="Add fallback signals")
        started = afk_mode.start_run(repo, "30m", self.run_root, allow_fallback_write=True)

        push_decision = afk_mode.pretool_decision(
            repo,
            "git push origin HEAD",
            run_root=self.run_root,
        )
        self.assertEqual(push_decision["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(
            push_decision["hookSpecificOutput"]["guardrail"]["rule_id"],
            "fallback-deny-git-push",
        )

        rebase_decision = afk_mode.pretool_decision(
            repo,
            "git rebase main",
            run_root=self.run_root,
        )
        self.assertEqual(
            rebase_decision["hookSpecificOutput"]["guardrail"]["rule_id"],
            "fallback-deny-git-rebase",
        )

        context = afk_mode.build_session_context(repo, run_root=self.run_root)
        self.assertEqual(context["run_id"], started["run_id"])
        self.assertEqual(context["write_mode"], "fallback")
        self.assertTrue(context["write_authorized"])

    def test_fallback_verify_rejects_structural_changes(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        (repo / "module.py").write_text("value = 1\n", encoding="utf-8")
        (repo / "package.json").write_text("{\"name\":\"base\"}\n", encoding="utf-8")
        self.commit_paths(repo, "SPEC.md", "module.py", "package.json", message="Add fallback module")
        started = afk_mode.start_run(repo, "30m", self.run_root, allow_fallback_write=True)
        run_dir = Path(started["run_dir"])
        self.rank_candidates(run_dir, slice_id="slice-fallback")
        active = afk_mode.open_slice(run_dir, "slice-fallback", 1, "fallback")
        worktree = Path(active["worktree"])

        (worktree / "package.json").write_text("{\"name\":\"blocked\"}\n", encoding="utf-8")
        self.git(worktree, "add", "package.json")
        self.git(worktree, "commit", "-m", "Touch package manifest")

        with self.assertRaisesRegex(afk_mode.AfkModeError, "does not allow modifying package.json"):
            afk_mode.verify_slice(run_dir, "slice-fallback")

    def test_fallback_verify_rejects_dot_prefixed_protected_paths(self) -> None:
        cases = [
            (".codex/guarded.py", "print('base')\n", "does not allow modifying .codex"),
            (".github/workflow.py", "print('base')\n", "does not allow modifying .github"),
            (".env.py", "value = 1\n", "does not allow modifying environment files"),
        ]
        for index, (relative_path, initial_content, expected_error) in enumerate(cases, start=1):
            with self.subTest(relative_path=relative_path):
                repo = self.init_repo(f"fallback-protected-{index}")
                target = repo / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
                target.write_text(initial_content, encoding="utf-8")
                self.commit_paths(repo, "SPEC.md", relative_path, message=f"Add {relative_path}")

                started = afk_mode.start_run(repo, "30m", self.run_root, allow_fallback_write=True)
                run_dir = Path(started["run_dir"])
                self.rank_candidates(run_dir, slice_id="slice-fallback")
                active = afk_mode.open_slice(run_dir, "slice-fallback", 1, "fallback")
                worktree = Path(active["worktree"])

                (worktree / relative_path).write_text("value = 2\n", encoding="utf-8")
                self.git(worktree, "add", relative_path)
                self.git(worktree, "commit", "-m", f"Touch {relative_path}")

                with self.assertRaisesRegex(afk_mode.AfkModeError, expected_error):
                    afk_mode.verify_slice(run_dir, "slice-fallback")

    def test_fallback_verify_supports_file_level_generic_checks(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        (repo / "module.py").write_text("value = 1\n", encoding="utf-8")
        self.commit_paths(repo, "SPEC.md", "module.py", message="Add fallback python module")
        started = afk_mode.start_run(repo, "30m", self.run_root, allow_fallback_write=True)
        run_dir = Path(started["run_dir"])
        self.rank_candidates(run_dir, slice_id="slice-generic")
        active = afk_mode.open_slice(run_dir, "slice-generic", 1, "generic")
        worktree = Path(active["worktree"])

        (worktree / "module.py").write_text("value = 2\n", encoding="utf-8")
        self.git(worktree, "add", "module.py")
        self.git(worktree, "commit", "-m", "Change module")
        commit_sha = self.git(worktree, "rev-parse", "HEAD").strip()

        proof = afk_mode.verify_slice(run_dir, "slice-generic")
        self.assertTrue(proof["all_passed"])
        self.assertEqual(proof["verification_source"], "fallback")
        self.assertEqual(proof["verification_mode"], "changed_files_only")
        self.assertEqual(proof["changed_paths"], ["module.py"])
        self.assertIn("py_compile", proof["allowed_commands"][0])

        recorded = afk_mode.record_slice(
            run_dir,
            "slice-generic",
            "success",
            "Fallback generic proof succeeded",
            active["branch"],
            commit_sha,
            str(worktree),
            None,
            None,
            [],
        )
        self.assertEqual(recorded["status"], "success")

    def test_hook_scripts_emit_expected_payloads(self) -> None:
        repo = self.init_repo()
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        started = afk_mode.start_run(repo, "2h", self.run_root)
        run_dir = Path(started["run_dir"])
        self.rank_candidates(run_dir, slice_id="slice-hooks")
        active = afk_mode.open_slice(run_dir, "slice-hooks", 1, "hooks")

        plugin_root = Path(__file__).resolve().parents[1]
        session_result = self.run_plugin_script(
            plugin_root / "hooks" / "session_start.py",
            {"cwd": str(repo), "source": "startup"},
        )
        session_payload = json.loads(session_result.stdout)
        self.assertIn("AFK Mode run", session_payload["hookSpecificOutput"]["additionalContext"])
        self.assertIn(active["slice_id"], session_payload["hookSpecificOutput"]["additionalContext"])

        pretool_result = self.run_plugin_script(
            plugin_root / "hooks" / "pre_tool_use.py",
            {"cwd": str(repo), "tool_input": {"command": "git reset --hard HEAD"}},
        )
        pretool_payload = json.loads(pretool_result.stdout)
        self.assertEqual(
            pretool_payload["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )

        stop_result = self.run_plugin_script(
            plugin_root / "hooks" / "stop_guard.py",
            {"cwd": str(repo)},
        )
        stop_payload = json.loads(stop_result.stdout)
        self.assertEqual(stop_payload["decision"], "block")
        self.assertIn(active["slice_id"], stop_payload["reason"])

        reentrant_result = self.run_plugin_script(
            plugin_root / "hooks" / "stop_guard.py",
            {"cwd": str(repo), "stop_hook_active": True},
        )
        reentrant_payload = json.loads(reentrant_result.stdout)
        self.assertTrue(reentrant_payload["continue"])

    def test_hook_scripts_are_silent_without_active_run(self) -> None:
        plugin_root = Path(__file__).resolve().parents[1]

        session_result = self.run_plugin_script(
            plugin_root / "hooks" / "session_start.py",
            {"cwd": str(self.root), "source": "startup"},
        )
        self.assertEqual(session_result.stdout.strip(), "")

        pretool_result = self.run_plugin_script(
            plugin_root / "hooks" / "pre_tool_use.py",
            {"cwd": str(self.root), "tool_input": {"command": "git status --short"}},
        )
        self.assertEqual(pretool_result.stdout.strip(), "")

        stop_result = self.run_plugin_script(
            plugin_root / "hooks" / "stop_guard.py",
            {"cwd": str(self.root)},
        )
        self.assertEqual(stop_result.stdout.strip(), "")

    def test_skill_commands_reference_deployed_plugin_helper(self) -> None:
        skill_path = Path(__file__).resolve().parents[1] / "skills" / "afk-mode" / "SKILL.md"
        content = skill_path.read_text(encoding="utf-8")

        self.assertIn("The public user-facing entrypoint is the skill invocation itself:", content)
        self.assertIn("`$afk-mode 90m`", content)
        self.assertIn("Do not ask the user to type `begin-run`, `advance-run`, `open-slice`, or other", content)
        self.assertIn('AFK_MODE_CMD="${AFK_MODE_CMD:-${AFK_MODE_PLUGIN_DIR:-$HOME/plugins/afk-mode}/afk-mode}"', content)
        self.assertIn('"${AFK_MODE_CMD}" begin-run --cwd "$PWD" --budget "<duration>"', content)
        self.assertNotIn("/home/ydhcjswo/plugins/afk-mode", content)
        self.assertNotIn("python3 scripts/afk_mode.py", content)
        self.assertNotIn("python3 ../../scripts/afk_mode.py", content)

    def test_plugin_interface_prompts_use_explicit_afk_mode_entrypoint(self) -> None:
        plugin_path = Path(__file__).resolve().parents[1] / ".codex-plugin" / "plugin.json"
        plugin_payload = json.loads(plugin_path.read_text(encoding="utf-8"))
        default_prompt = " ".join(plugin_payload["interface"]["defaultPrompt"])
        self.assertIn("$afk-mode <budget>", default_prompt)
        self.assertIn("only user-facing entrypoint", default_prompt)

        agent_prompt_path = Path(__file__).resolve().parents[1] / "skills" / "afk-mode" / "agents" / "openai.yaml"
        agent_payload = yaml.safe_load(agent_prompt_path.read_text(encoding="utf-8"))
        self.assertEqual(agent_payload["policy"]["allow_implicit_invocation"], False)
        self.assertIn("$afk-mode <budget>", agent_payload["interface"]["default_prompt"])
        self.assertIn("internal helpers", agent_payload["interface"]["default_prompt"])

    def test_publish_script_uses_portable_plugin_dir(self) -> None:
        script_path = Path(__file__).resolve().with_name("publish_plugin.sh")
        content = script_path.read_text(encoding="utf-8")

        self.assertIn('TARGET_DIR="${AFK_MODE_PLUGIN_DIR:-${HOME}/plugins/afk-mode}"', content)
        self.assertIn("--exclude __pycache__", content)
        self.assertIn("--exclude '*.pyc'", content)
        self.assertIn('find "${TARGET_DIR}" -type d -name __pycache__ -prune -exec rm -rf {} +', content)
        self.assertIn('find "${TARGET_DIR}" -type f -name \'*.pyc\' -delete', content)

    def test_hook_script_payload_contracts_are_stable(self) -> None:
        repo = self.init_repo("hook-contracts")
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")
        started = afk_mode.start_run(repo, "2h", self.run_root)
        run_dir = Path(started["run_dir"])
        self.rank_candidates(run_dir, slice_id="slice-contracts")
        afk_mode.open_slice(run_dir, "slice-contracts", 1, "contracts")

        plugin_root = Path(__file__).resolve().parents[1]
        session_payload = json.loads(
            self.run_plugin_script(
                plugin_root / "hooks" / "session_start.py",
                {"cwd": str(repo), "source": "startup"},
            ).stdout
        )
        self.assertIn("hookSpecificOutput", session_payload)
        self.assertIn("hookEventName", session_payload["hookSpecificOutput"])
        self.assertIn("additionalContext", session_payload["hookSpecificOutput"])
        self.assertEqual(session_payload["hookSpecificOutput"]["hookEventName"], "SessionStart")

        pretool_payload = json.loads(
            self.run_plugin_script(
                plugin_root / "hooks" / "pre_tool_use.py",
                {"cwd": str(repo), "tool_input": {"command": "git reset --hard HEAD"}},
            ).stdout
        )
        self.assertIn("hookSpecificOutput", pretool_payload)
        self.assertIn("systemMessage", pretool_payload)
        self.assertIn("hookEventName", pretool_payload["hookSpecificOutput"])
        self.assertIn("permissionDecision", pretool_payload["hookSpecificOutput"])
        self.assertIn("permissionDecisionReason", pretool_payload["hookSpecificOutput"])
        self.assertEqual(pretool_payload["hookSpecificOutput"]["hookEventName"], "PreToolUse")

        stop_payload = json.loads(
            self.run_plugin_script(
                plugin_root / "hooks" / "stop_guard.py",
                {"cwd": str(repo)},
            ).stdout
        )
        self.assertIn("decision", stop_payload)
        self.assertIn("reason", stop_payload)
        self.assertEqual(stop_payload["decision"], "block")
        reentrant_payload = json.loads(
            self.run_plugin_script(
                plugin_root / "hooks" / "stop_guard.py",
                {"cwd": str(repo), "stop_hook_active": True},
            ).stdout
        )
        self.assertIn("continue", reentrant_payload)
        self.assertTrue(reentrant_payload["continue"])

    def test_start_run_writes_structured_candidate_queue_and_status_fields(self) -> None:
        repo = self.init_repo("queue-fields")
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")

        started = afk_mode.start_run(repo, "2h", self.run_root)
        run_dir = Path(started["run_dir"])
        queue_path = run_dir / afk_mode.CANDIDATES_QUEUE_FILENAME
        queue = json.loads(queue_path.read_text(encoding="utf-8"))

        self.assertTrue(queue["slices"])
        self.assertEqual(queue["slices"][0]["status"], "queued")
        self.assertEqual(queue["slices"][0]["source"]["kind"], "repo_truth")

        status_payload = afk_mode.status(run_dir)
        self.assertEqual(status_payload["phase"], "planning")
        self.assertEqual(status_payload["controller_state"], "queued")
        self.assertIsNotNone(status_payload["heartbeat_at"])
        self.assertEqual(status_payload["wake_policy"], "hard_blockers_only")
        self.assertEqual(status_payload["planning_budget_ratio"], 0.2)
        self.assertEqual(status_payload["verification_reserve_ratio"], 0.2)
        self.assertEqual(status_payload["resume_policy"], "fail_stale_active_slice")
        self.assertEqual(status_payload["consecutive_failures"], 0)
        self.assertFalse(status_payload["stale"])

    def test_advance_run_selects_repo_workflow_candidate_and_requests_plan_first(self) -> None:
        repo = self.init_repo("advance-open")
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_status(repo, "20260420-queue", phase="worker", summary="Implement queued task")
        self.write_profile(repo)
        self.commit_paths(
            repo,
            "SPEC.md",
            ".codex/plugin-profile.yaml",
            ".codex/work/20260420-queue/STATUS.json",
            message="Add workflow-backed candidate",
        )

        started = afk_mode.start_run(repo, "2h", self.run_root)
        run_dir = Path(started["run_dir"])

        advanced = afk_mode.advance_run(run_dir)

        self.assertEqual(advanced["next_action"], "draft_plan")
        self.assertEqual(advanced["selected_candidate"]["slice_id"], "20260420-queue")
        self.assertEqual(advanced["phase"], "planning")
        self.assertEqual(advanced["controller_state"], "plan_required")
        self.assertFalse(advanced["wake_operator"])
        self.assertEqual(advanced["blocker_severity"], "none")
        self.assertTrue(Path(advanced["plan_dir"]).exists())

    def test_advance_run_opens_frozen_candidate_and_requests_implementation(self) -> None:
        repo = self.init_repo("advance-frozen")
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_status(repo, "20260420-queue", phase="worker", summary="Implement queued task")
        self.write_profile(repo)
        self.commit_paths(
            repo,
            "SPEC.md",
            ".codex/plugin-profile.yaml",
            ".codex/work/20260420-queue/STATUS.json",
            message="Add workflow-backed candidate",
        )

        started = afk_mode.start_run(repo, "2h", self.run_root)
        run_dir = Path(started["run_dir"])
        self.write_plan_artifacts(run_dir, "20260420-queue", include_frozen_plan=True)

        advanced = afk_mode.advance_run(run_dir)

        self.assertTrue(advanced["requires_implementation"])
        self.assertEqual(advanced["next_action"], "requires_implementation")
        self.assertEqual(advanced["active_slice"]["slice_id"], "20260420-queue")
        self.assertEqual(advanced["phase"], "implementing")
        self.assertEqual(advanced["controller_state"], "opened")

    def test_advance_run_blocks_fallback_runs_for_unattended_mode(self) -> None:
        repo = self.init_repo("advance-fallback")
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        (repo / "scripts").mkdir()
        (repo / "scripts" / "verify_change.py").write_text("print('ok')\n", encoding="utf-8")
        self.commit_paths(repo, "SPEC.md", "scripts/verify_change.py", message="Add fallback signals")

        started = afk_mode.start_run(repo, "30m", self.run_root, allow_fallback_write=True)
        run_dir = Path(started["run_dir"])

        advanced = afk_mode.advance_run(run_dir)

        self.assertTrue(advanced["blocked"])
        self.assertEqual(advanced["phase"], "blocked")
        self.assertEqual(advanced["last_blocker"]["code"], "overnight_repo_owned_required")
        self.assertEqual(advanced["blocker_severity"], "hard")
        self.assertTrue(advanced["wake_operator"])

    def test_advance_run_blocks_missing_workflow_evidence_as_hard_blocker(self) -> None:
        repo = self.init_repo("advance-workflow-evidence")
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")

        started = afk_mode.start_run(repo, "2h", self.run_root)
        run_dir = Path(started["run_dir"])
        self.write_candidate_queue(
            run_dir,
            {
                "slice_id": "slice-needs-workflow",
                "ordinal": 1,
                "title": "Needs workflow evidence",
                "requires_workflow_token": True,
            },
        )

        advanced = afk_mode.advance_run(run_dir)

        self.assertTrue(advanced["blocked"])
        self.assertEqual(advanced["blocker_code"], "workflow_evidence_required")
        self.assertEqual(advanced["blocker_severity"], "hard")
        self.assertTrue(advanced["wake_operator"])

    def test_advance_run_stops_softly_when_no_candidate_fits_budget(self) -> None:
        repo = self.init_repo("advance-over-budget")
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")

        started = afk_mode.start_run(repo, "10m", self.run_root)
        run_dir = Path(started["run_dir"])
        self.write_candidate_queue(
            run_dir,
            {
                "slice_id": "slice-large",
                "ordinal": 1,
                "title": "Large slice",
            },
        )
        self.write_candidates_meta(
            run_dir,
            {
                "slice_id": "slice-large",
                "size": "large",
                "risk": "high",
                "verify_cost": "slow",
            },
        )

        advanced = afk_mode.advance_run(run_dir)

        self.assertTrue(advanced["finished"])
        self.assertEqual(advanced["status"], "stopped")
        self.assertEqual(advanced["blocker_code"], "no_candidate_within_budget")
        self.assertEqual(advanced["blocker_severity"], "soft")
        self.assertFalse(advanced["wake_operator"])

    def test_advance_run_prefers_smallest_safe_frozen_candidate(self) -> None:
        repo = self.init_repo("advance-smallest-safe")
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")

        started = afk_mode.start_run(repo, "3h", self.run_root)
        run_dir = Path(started["run_dir"])
        self.write_candidate_queue(
            run_dir,
            {
                "slice_id": "slice-large",
                "ordinal": 1,
                "title": "Large slice",
                "ready_for_execution": True,
                "plan_status": "frozen",
            },
            {
                "slice_id": "slice-small",
                "ordinal": 2,
                "title": "Small slice",
                "ready_for_execution": True,
                "plan_status": "frozen",
            },
        )
        self.write_plan_artifacts(run_dir, "slice-large", include_frozen_plan=True)
        self.write_plan_artifacts(run_dir, "slice-small", include_frozen_plan=True)
        self.write_candidates_meta(
            run_dir,
            {
                "slice_id": "slice-large",
                "size": "large",
                "risk": "medium",
                "verify_cost": "medium",
            },
            {
                "slice_id": "slice-small",
                "size": "small",
                "risk": "low",
                "verify_cost": "fast",
            },
        )

        advanced = afk_mode.advance_run(run_dir)

        self.assertEqual(advanced["active_slice"]["slice_id"], "slice-small")

    def test_advance_run_records_success_and_finishes_completed_run(self) -> None:
        repo = self.init_repo("advance-success")
        (repo / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
        self.write_profile(repo)
        self.commit_paths(repo, "SPEC.md", ".codex/plugin-profile.yaml", message="Add spec")

        started = afk_mode.start_run(repo, "2h", self.run_root)
        run_dir = Path(started["run_dir"])
        self.write_candidate_queue(
            run_dir,
            {
                "slice_id": "slice-controller",
                "ordinal": 1,
                "title": "Controller slice",
                "ready_for_execution": True,
                "plan_status": "frozen",
            },
        )
        self.write_plan_artifacts(run_dir, "slice-controller", include_frozen_plan=True)

        opened = afk_mode.advance_run(run_dir)
        worktree = Path(opened["active_slice"]["worktree"])
        (worktree / "tracked.txt").write_text("controller\n", encoding="utf-8")

        finished = afk_mode.advance_run(
            run_dir,
            implementation_result="done",
            summary="Controller recorded the slice",
        )

        self.assertTrue(finished["finished"])
        self.assertEqual(finished["status"], "completed")
        payload = afk_mode.load_run(run_dir)
        self.assertEqual(payload["slices"][0]["status"], "success")
        queue = json.loads((run_dir / afk_mode.CANDIDATES_QUEUE_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(queue["slices"][0]["status"], "done")


if __name__ == "__main__":
    unittest.main()
