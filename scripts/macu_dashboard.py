#!/usr/bin/env python3
"""Local MACU experiment dashboard for OSWorld tasks.

The dashboard is intentionally local-only. It discovers OSWorld tasks, focuses
on GIMP and LibreOffice Calc, launches ``run_macu.py`` without a shell, tails
its output, and summarizes evaluator results.
"""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Optional, Union

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory, stream_with_context

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.dashboard_cleanup import cleanup_dashboard_runtime
from utils.tinker_utils import TINKER_MODELS, estimate_experiment_cost

ASSET_ROOT = PROJECT_ROOT / "scripts" / "_dashboard_assets"
DEFAULT_OSWORLD_ROOT = Path(os.environ.get("OSWORLD_ROOT", PROJECT_ROOT.parent / "OSWorld"))
DEFAULT_RESULT_ROOT = PROJECT_ROOT / "runs" / "dashboard"
FOCUS_DOMAINS = ("gimp", "libreoffice_calc", "libreoffice_impress")
GEMINI_MODELS = {
    "gemini-3.1-flash-lite-preview": {
        "name": "Gemini 3.1 Flash Lite", "family": "Google", "context": "1M",
        "type": "Vision", "vision": True, "image_input": True,
        "prefill": 0.25, "cached_prefill": 0.0, "sample": 1.5,
        "provider": "google", "cua_available": False,
        "note": "Manager only in the current dashboard",
    },
    "gemini-3.1-pro-preview": {
        "name": "Gemini 3.1 Pro", "family": "Google", "context": "1M",
        "type": "Reasoning + Vision", "vision": True, "image_input": True,
        "prefill": 2.0, "cached_prefill": 0.0, "sample": 12.0,
        "provider": "google", "cua_available": False,
        "note": "Manager only in the current dashboard",
    },
}
OPENAI_CUA_MODELS = {
    "gpt-5.4-mini": {
        "name": "GPT-5.4 Mini", "family": "OpenAI", "context": "128K",
        "type": "Native Computer Use + Vision", "vision": True, "image_input": True,
        "prefill": 0.75, "cached_prefill": 0.075, "sample": 4.5,
        "provider": "openai", "cua_available": True,
        "note": "Requires OPENAI_API_KEY in .env",
    },
    "gpt-5.4": {
        "name": "GPT-5.4", "family": "OpenAI", "context": "128K",
        "type": "Native Computer Use + Vision", "vision": True, "image_input": True,
        "prefill": 2.5, "cached_prefill": 0.25, "sample": 15.0,
        "provider": "openai", "cua_available": True,
        "note": "Requires OPENAI_API_KEY in .env",
    },
}
QWEN_CUA_MODELS = {
    "Qwen/Qwen3.6-27B": {
        "name": "Qwen3.6-27B", "family": "Qwen", "context": "64K",
        "type": "Local Vision CUA", "vision": True, "image_input": True,
        "prefill": 0.0, "cached_prefill": 0.0, "sample": 0.0,
        "provider": "qwen", "cua_available": True,
        "note": "Local OpenAI-compatible server; configure OPENAI_BASE_URL in .env",
    },
}
OPENROUTER_MODELS = {
    "moonshotai/kimi-k3": {
        "name": "Kimi K3", "family": "Moonshot", "context": "1M",
        "type": "Reasoning + Vision", "vision": True, "image_input": True,
        "prefill": 0.304, "cached_prefill": 0.21, "sample": 11.936,
        "provider": "openrouter", "cua_available": False,
        "note": "OpenRouter fallback · manager only",
    },
    "google/gemma-4-26b-a4b-it": {
        "name": "Gemma 4 26B A4B", "family": "Google", "context": "256K",
        "type": "Vision", "vision": True, "image_input": True,
        "prefill": 0.0675, "cached_prefill": 0.0375, "sample": 0.225,
        "provider": "openrouter", "cua_available": False,
        "note": "OpenRouter fallback · manager only",
    },
}
DASHBOARD_MODELS = {
    **{key: {**value, "provider": "tinker", "cua_available": True} for key, value in TINKER_MODELS.items()},
    **GEMINI_MODELS,
    **OPENROUTER_MODELS,
    **OPENAI_CUA_MODELS,
    **QWEN_CUA_MODELS,
}

SHARED_CANVAS_TASKS = {
    "0a211154-fda0-48d0-9274-eaac4ce5486d": ("high", 2),
    "4ed5abd0-8b5d-47bd-839f-cacfa15ca37a": ("high", 3),
    "05dd4c1d-c489-4c85-8389-a7836c4f0567": ("high", 3),
    "7ae48c60-f143-4119-b659-15b8f485eb9a": ("high", 3),
    "30e3e107-1cfb-46ee-a755-2cd080d7ba6a": ("high", 4),
    "0326d92d-d218-48a8-9ca1-981cd6d064c7": ("high", 2),
    "347ef137-7eeb-4c80-a3bb-0951f26a8aff": ("high", 2),
    "986fc832-6af2-417c-8845-9272b3a1528b": ("high", 3),
    "04578141-1d42-4146-b9cf-6fab4ce5fd74": ("high", 3),
    "a53f80cd-4a90-4490-8310-097b011433f6": ("high", 2),
    "bf4e9888-f10f-47af-8dba-76413038b73c": ("high", 3),
    "d68204bf-11c1-4b13-b48b-d303c73d4bf6": ("high", 3),
    "ed43c15f-00cb-4054-9c95-62c880865d68": ("high", 3),
    "5c1a6c3d-c1b3-47cb-9b01-8d1b7544ffa1": ("medium", 2),
    "535364ea-05bd-46ea-9937-9f55c68507e8": ("medium", 2),
    "a434992a-89df-4577-925c-0c58b747f0f4": ("medium", 2),
    "e4ef0baf-4b52-4590-a47e-d4d464cca2d7": ("medium", 2),
}

app = Flask(__name__, static_folder=None)
_TINKER_AUTH_CACHE: dict[str, Any] = {}


def _tinker_auth_check() -> tuple[bool, str]:
    key = os.environ.get("TINKER_API_KEY", "").strip()
    if not key:
        return False, "TINKER_API_KEY is not exported"
    fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    if (
        _TINKER_AUTH_CACHE.get("fingerprint") == fingerprint
        and time.time() - float(_TINKER_AUTH_CACHE.get("checked_at", 0)) < 60
    ):
        return bool(_TINKER_AUTH_CACHE["ok"]), str(_TINKER_AUTH_CACHE["detail"])
    try:
        import anthropic
        from utils.tinker_utils import TINKER_ANTHROPIC_BASE_URL
        client = anthropic.Anthropic(
            api_key=key,
            base_url=os.environ.get("TINKER_ANTHROPIC_BASE_URL", TINKER_ANTHROPIC_BASE_URL),
            timeout=15.0,
        )
        client.messages.count_tokens(
            model="thinkingmachines/Inkling-Small",
            messages=[{"role": "user", "content": "MACU readiness check"}],
        )
        result = (True, "Tinker authentication verified")
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status_code", None)
        result = (False, f"Tinker authentication failed{f' (HTTP {status})' if status else ''}")
    _TINKER_AUTH_CACHE.update(
        fingerprint=fingerprint, checked_at=time.time(), ok=result[0], detail=result[1]
    )
    return result


def _estimate_with_cua_provider(manager_model: str, cua_provider: str, cua_model: str, task_count: int, parallel_agents: int) -> dict[str, Any]:
    if cua_provider == "tinker":
        return estimate_experiment_cost(manager_model, cua_model, task_count, parallel_agents)
    catalog = OPENAI_CUA_MODELS if cua_provider == "openai" else QWEN_CUA_MODELS
    if cua_model not in catalog:
        raise ValueError(f"Choose a supported {cua_provider} computer-use model")
    manager_calls = max(0, task_count) * 5
    cua_calls = max(0, task_count) * max(1, parallel_agents) * 20
    manager_info, cua_info = TINKER_MODELS[manager_model], catalog[cua_model]
    manager_cost = (manager_calls * 8_000 * manager_info["prefill"] + manager_calls * 1_000 * manager_info["sample"]) / 1_000_000
    cua_cost = (cua_calls * 12_000 * cua_info["prefill"] + cua_calls * 500 * cua_info["sample"]) / 1_000_000
    return {
        "manager_calls": manager_calls, "cua_calls": cua_calls,
        "manager_cost_usd": round(manager_cost, 4), "cua_cost_usd": round(cua_cost, 4),
        "estimated_cost_usd": round(manager_cost + cua_cost, 4),
        "assumptions": "5 manager calls/task; agents × 20 CUA turns/task. Local model cost is shown as $0.",
    }


def _resolved(value: Union[str, Path]) -> Path:
    return Path(value).expanduser().resolve()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def discover_tasks(osworld_root: Path) -> list[dict[str, Any]]:
    """Discover GIMP/Calc tasks directly from the OSWorld examples tree."""
    examples_root = osworld_root / "evaluation_examples" / "examples"
    tasks: list[dict[str, Any]] = []
    if not examples_root.is_dir():
        return tasks

    domain_dirs = [p for p in examples_root.iterdir() if p.is_dir()]
    preferred = [p for p in domain_dirs if p.name in FOCUS_DOMAINS]
    if not preferred:
        preferred = [
            p for p in domain_dirs
            if "gimp" in p.name.lower()
            or ("libreoffice" in p.name.lower() and "calc" in p.name.lower())
        ]

    task_paths = [path for domain_dir in sorted(preferred) for path in sorted(domain_dir.glob("*.json"))]
    # The curated GIMP command-line item lives in OSWorld's multi_apps domain.
    for task_id in SHARED_CANVAS_TASKS:
        task_paths.extend(examples_root.glob(f"*/{task_id}.json"))
    seen_paths: set[Path] = set()
    for task_path in task_paths:
        if task_path in seen_paths:
            continue
        seen_paths.add(task_path)
        domain_dir = task_path.parent
        try:
            raw = json.loads(task_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        task_id = str(raw.get("id") or task_path.stem)
        tasks.append(
            {
                "key": f"{domain_dir.name}/{task_id}",
                "id": task_id,
                "domain": domain_dir.name,
                "instruction": str(raw.get("instruction") or ""),
                "related_apps": raw.get("related_apps") or [],
                "snapshot": raw.get("snapshot"),
                "group": "shared_canvas" if task_id in SHARED_CANVAS_TASKS else "standard",
                "confidence": SHARED_CANVAS_TASKS.get(task_id, (None, None))[0],
                "suggested_agents": SHARED_CANVAS_TASKS.get(task_id, (None, None))[1],
            }
        )
    return tasks


def setup_checks(osworld_root: Path, provider: str = "vmware") -> list[dict[str, Any]]:
    examples = osworld_root / "evaluation_examples" / "examples"
    registry = osworld_root / ".vmware_vms"
    free_vms = 0
    if registry.is_file():
        try:
            free_vms = sum(
                1 for line in registry.read_text(encoding="utf-8").splitlines()
                if line.strip().endswith("|free")
            )
        except OSError:
            pass

    provider_binary = "vmrun" if provider == "vmware" else "apptainer"
    tinker_ok, tinker_detail = _tinker_auth_check()
    checks = [
        ("python", sys.version_info >= (3, 12), f"Python {sys.version.split()[0]} (3.12 required for runs)"),
        ("osworld", osworld_root.is_dir(), str(osworld_root)),
        ("examples", examples.is_dir(), str(examples)),
        ("provider", shutil.which(provider_binary) is not None, f"{provider_binary} on PATH"),
        ("tinker_key", tinker_ok, tinker_detail),
    ]
    if provider == "vmware":
        checks.append(("vm_pool", free_vms > 0, f"{free_vms} free VM(s) in {registry}"))
    return [{"id": key, "ok": ok, "detail": detail} for key, ok, detail in checks]


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def collect_results(result_root: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not result_root.is_dir():
        return results
    for task_dir in result_root.iterdir():
        if not task_dir.is_dir() or task_dir.name.startswith("_"):
            continue
        final = _read_json(task_dir / "final_results.json")
        graph = _read_json(task_dir / "dependency_graph.json")
        claim = (task_dir / ".claimed").exists()
        run_error = _read_json(task_dir / "run_error.json")
        if final is None and graph is None and not claim and run_error is None:
            continue

        dependency_graph = (graph or {}).get("dependency_graph") or {}
        subtasks = dependency_graph.get("subtasks") or []
        subtask_results = (final or {}).get("subtask_results") or {}
        success_count = sum(1 for item in subtask_results.values() if item.get("status") == "success")
        error_count = sum(1 for item in subtask_results.values() if item.get("status") != "success")
        score = (final or {}).get("osworld_evaluator_score")
        status = "completed" if final else ("failed" if run_error else ("running_or_claimed" if claim else "preparing"))
        results.append(
            {
                "task_id": task_dir.name,
                "domain": (final or {}).get("domain") or (graph or {}).get("domain"),
                "instruction": (final or {}).get("instruction") or (graph or {}).get("instruction") or "",
                "status": status,
                "score": score,
                "duration_seconds": (final or {}).get("total_duration_seconds"),
                "cost_usd": (final or {}).get("total_cost_usd"),
                "subtask_count": len(subtasks),
                "success_count": success_count,
                "error_count": error_count,
                "replans": ((final or {}).get("replanning") or {}).get("num_applied", 0),
                "final_response": (final or {}).get("final_response", "") or (run_error or {}).get("error", ""),
                "has_visualization": (task_dir / "visualization" / "index.html").is_file(),
                "has_video": (task_dir / "visualization" / "replay.mp4").is_file(),
            }
        )
    return sorted(results, key=lambda item: item["task_id"])


def _latest_agent_frame(agent_dir: Path) -> tuple[Optional[Path], dict[str, Any]]:
    """Return the newest saved VM frame and its trajectory metadata."""
    trajectory_files = list(agent_dir.rglob("traj.jsonl"))
    trajectory_files.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    latest_row: dict[str, Any] = {}
    latest_frame: Optional[Path] = None
    for trajectory_path in trajectory_files:
        try:
            for line in trajectory_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    latest_row = row
                    screenshot_name = row.get("screenshot_file")
                    if screenshot_name:
                        candidate = (trajectory_path.parent / str(screenshot_name)).resolve()
                        if candidate.is_file() and _inside(candidate, agent_dir):
                            latest_frame = candidate
        except OSError:
            continue
        if latest_frame is not None:
            break

    if latest_frame is None:
        frames = [
            path for path in agent_dir.rglob("step_*.png")
            if path.is_file() and _inside(path, agent_dir)
        ]
        if frames:
            latest_frame = max(frames, key=lambda path: path.stat().st_mtime)
    if latest_frame is None and (agent_dir / "setup_visual.png").is_file():
        latest_frame = agent_dir / "setup_visual.png"
    if latest_frame is None and (agent_dir.parent / "initial_screenshot.png").is_file():
        latest_frame = agent_dir.parent / "initial_screenshot.png"
    return latest_frame, latest_row


def _agent_action_history(agent_dir: Path, limit: int = 8) -> list[dict[str, Any]]:
    """Return recent actions plus cursor coordinates for the live monitor."""
    trajectories = sorted(
        agent_dir.rglob("traj.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not trajectories:
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = trajectories[0].read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("step_num") is None:
            continue
        raw_action = row.get("action") or ""
        if isinstance(raw_action, dict):
            action_text = str(raw_action.get("action") or raw_action.get("script") or raw_action)
        elif isinstance(raw_action, list):
            action_text = "; ".join(str(item) for item in raw_action)
        else:
            action_text = str(raw_action)
        coordinate = None
        match = re.search(
            r"(?:click|moveTo|dragTo|doubleClick|rightClick)\s*\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)",
            action_text,
            re.IGNORECASE,
        )
        if match:
            coordinate = {"x": float(match.group(1)), "y": float(match.group(2))}
        rows.append({
            "step": row.get("step_num"),
            "action": " ".join(action_text.split())[:300],
            "activity": _agent_activity(row),
            "coordinate": coordinate,
        })
    return rows[-limit:]


def _agent_activity(row: dict[str, Any]) -> str:
    response = str(row.get("response") or "").strip()
    activity = response.split("<", 1)[0].strip() or response.splitlines()[0].strip() if response else ""
    if not activity:
        action = row.get("action") or ""
        if isinstance(action, dict):
            activity = str(action.get("action") or action.get("script") or "")
        else:
            activity = str(action)
    return " ".join(activity.split())[:500]


def collect_live_agents(result_root: Path) -> list[dict[str, Any]]:
    """Describe each CUA branch and its latest persisted VM screenshot."""
    agents: list[dict[str, Any]] = []
    if not result_root.is_dir():
        return agents
    job_snapshot = JOB.snapshot() if "JOB" in globals() else {}
    job_running = bool(job_snapshot.get("running"))
    active_started_at = job_snapshot.get("started_at") if job_running else None
    active_result_root = Path(job_snapshot["result_root"]).resolve() if job_snapshot.get("result_root") else None
    for task_dir in sorted(result_root.iterdir()):
        if not task_dir.is_dir() or task_dir.name.startswith("_"):
            continue
        graph_path = task_dir / "dependency_graph.json"
        # A result directory may contain a DAG from an earlier Single Agent or
        # MACU run. Never display that stale topology while the current manager
        # is still generating its new graph.
        if (
            active_started_at
            and active_result_root == result_root.resolve()
            and (not graph_path.is_file() or graph_path.stat().st_mtime < active_started_at)
        ):
            continue
        graph = _read_json(graph_path) or {}
        subtasks = ((graph.get("dependency_graph") or {}).get("subtasks") or [])
        final = _read_json(task_dir / "final_results.json") or {}
        final_states = final.get("subtask_results") or {}
        error_path = task_dir / "run_error.json"
        run_failed = error_path.is_file() and (
            not graph_path.is_file() or error_path.stat().st_mtime >= graph_path.stat().st_mtime
        )
        task_active = job_running or (
            (task_dir / ".claimed").is_file() and not final and not run_failed
        )
        for subtask in subtasks:
            if subtask.get("agent_type", "cua") == "manager":
                continue
            agent_id = str(subtask.get("id") or "")
            if not agent_id:
                continue
            agent_dir = (task_dir / agent_id).resolve()
            if not _inside(agent_dir, task_dir):
                continue
            frame, row = _latest_agent_frame(agent_dir) if agent_dir.is_dir() else (None, {})
            result_state = final_states.get(agent_id) or {}
            completion_files = list(agent_dir.rglob("completed.txt")) if agent_dir.is_dir() else []
            completion_file = max(completion_files, key=lambda path: path.stat().st_mtime) if completion_files else None
            completion_is_current = bool(
                completion_file and (
                    not graph_path.is_file() or completion_file.stat().st_mtime >= graph_path.stat().st_mtime
                )
            )
            if result_state:
                status = "complete" if result_state.get("status") == "success" else "failed"
            elif completion_is_current:
                try:
                    self_score = float(completion_file.read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    self_score = 0.0
                status = "complete" if self_score > 0 else "failed"
            elif agent_dir.is_dir() and task_active:
                status = "active"
            elif agent_dir.is_dir():
                status = "stopped"
            else:
                status = "waiting"
            vm_info = _read_json(agent_dir / "vm_info.json") or {}
            vm_path = str(vm_info.get("vm_path") or "")
            args_files = list(agent_dir.rglob("args.json")) if agent_dir.is_dir() else []
            runtime_args = _read_json(max(args_files, key=lambda path: path.stat().st_mtime)) if args_files else {}
            runtime_args = runtime_args or {}
            model = str(runtime_args.get("model") or "")
            observation = str(runtime_args.get("observation_type") or "screenshot")
            model_accepts_images = bool(TINKER_MODELS.get(model, {}).get("image_input")) if model else None
            receives_vision = bool(model_accepts_images and "screenshot" in observation) if model else None
            action_history = _agent_action_history(agent_dir) if agent_dir.is_dir() else []
            cursor = next(
                (item["coordinate"] for item in reversed(action_history) if item.get("coordinate")),
                None,
            )
            agents.append({
                "task_id": task_dir.name,
                "agent_id": agent_id,
                "description": str(subtask.get("description") or agent_id),
                "instruction": str(subtask.get("instruction") or ""),
                "dependencies": [str(item) for item in (subtask.get("dependencies") or [])],
                "status": status,
                "step": row.get("step_num"),
                "activity": _agent_activity(row),
                "vm": Path(vm_path).stem if vm_path else "Allocating VM…",
                "model": model or None,
                "provider": runtime_args.get("cua_provider"),
                "observation": observation,
                "receives_vision": receives_vision,
                "action_space": runtime_args.get("action_space"),
                "coordinate_mode": runtime_args.get("coord"),
                "screen_resolution": (
                    f"{runtime_args.get('screen_width')}x{runtime_args.get('screen_height')}"
                    if runtime_args.get("screen_width") and runtime_args.get("screen_height") else None
                ),
                "max_steps": runtime_args.get("max_steps"),
                "reasoning_effort": runtime_args.get("reasoning_effort"),
                "cursor": cursor,
                "action_history": action_history,
                "frame_version": frame.stat().st_mtime_ns if frame else None,
            })
    return agents


class JobState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.proc: Optional[subprocess.Popen[str]] = None
        self.job_id: Optional[str] = None
        self.started_at: Optional[float] = None
        self.ended_at: Optional[float] = None
        self.returncode: Optional[int] = None
        self.command: list[str] = []
        self.result_root: Optional[Path] = None
        self.log: deque[str] = deque(maxlen=800)
        self.artifact_tasks: set[str] = set()
        self.artifact_building: set[str] = set()

    def _batch_info(self, running: bool) -> dict[str, Any]:
        tasks: list[dict[str, Any]] = []
        manifest_path = next(
            (Path(item) for item in self.command if str(item).endswith(".json") and "tasks_" in str(item)),
            None,
        )
        manifest = _read_json(manifest_path) if manifest_path else None
        if manifest:
            position = 0
            for domain, task_ids in manifest.items():
                for task_id in task_ids:
                    position += 1
                    task_dir = (self.result_root / task_id) if self.result_root else Path()
                    if (task_dir / "final_results.json").is_file():
                        status = "complete"
                    elif (task_dir / "run_error.json").is_file():
                        status = "failed"
                    elif (task_dir / ".claimed").is_file() and running:
                        status = "active"
                    else:
                        status = "queued"
                    tasks.append({"task_id": task_id, "domain": domain, "position": position, "status": status})
        current = next((task for task in tasks if task["status"] == "active"), None)
        phase = "Waiting to launch"
        recent = "\n".join(self.log)
        if running:
            phase = "Starting task"
            phase_patterns = [
                ("Taking initial VM screenshot", "Preparing initial VM and screenshot"),
                ("Generating dependency graph", "Manager generating MACU dependency graph"),
                ("Graph generated", "Dependency graph ready"),
                ("Launched CUA", "Computer-use agents running"),
                ("complete (", "Reviewing completed agent work"),
                ("Replan", "Manager replanning dependencies"),
                ("OSWorld evaluator", "Evaluating task result"),
            ]
            for marker, label in phase_patterns:
                if marker in recent:
                    phase = label
        elif self.returncode is not None:
            phase = "Batch completed" if self.returncode == 0 else "Batch stopped or failed"
        return {"tasks": tasks, "current": current, "total": len(tasks), "phase": phase}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            return {
                "id": self.job_id,
                "running": running,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "elapsed_seconds": round(time.time() - self.started_at, 1) if running and self.started_at else None,
                "returncode": self.returncode,
                "command": self.command,
                "run_mode": "single" if "--no-manager" in self.command else ("macu" if self.command else None),
                "max_parallelism": (
                    int(self.command[self.command.index("--max-parallelism") + 1])
                    if "--max-parallelism" in self.command else None
                ),
                "result_root": str(self.result_root) if self.result_root else None,
                "log": list(self.log),
                "batch": self._batch_info(running),
            }

    def start(
        self,
        command: list[str],
        result_root: Path,
        log_path: Path,
        env_overrides: Optional[dict[str, str]] = None,
    ) -> None:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                raise RuntimeError("A MACU job is already running")
            self.job_id = uuid.uuid4().hex[:10]
            self.started_at = time.time()
            self.ended_at = None
            self.returncode = None
            self.command = command
            self.result_root = result_root
            self.log.clear()
            self.artifact_tasks.clear()
            self.artifact_building.clear()
            self.log.append("$ " + " ".join(command))
            child_env = os.environ.copy()
            child_env.update(env_overrides or {})
            self.proc = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            threading.Thread(
                target=self._consume,
                args=(self.proc, log_path),
                name=f"macu-dashboard-{self.job_id}",
                daemon=True,
            ).start()
            threading.Thread(
                target=self._watch_completed_artifacts,
                args=(self.proc,),
                name=f"macu-video-{self.job_id}",
                daemon=True,
            ).start()

    def _watch_completed_artifacts(self, proc: subprocess.Popen[str]) -> None:
        while proc.poll() is None:
            self._build_completed_artifacts()
            time.sleep(2)
        self._build_completed_artifacts()

    def _build_completed_artifacts(self) -> None:
        if self.result_root is None:
            return
        for task_dir in self.result_root.iterdir():
            if (
                not task_dir.is_dir()
                or not (task_dir / "final_results.json").is_file()
                or task_dir.name in self.artifact_tasks
            ):
                continue
            with self.lock:
                if task_dir.name in self.artifact_building:
                    continue
                self.artifact_building.add(task_dir.name)
            commands = [
                [sys.executable, str(PROJECT_ROOT / "scripts" / "visualize_run.py"), str(task_dir)],
                [sys.executable, str(PROJECT_ROOT / "scripts" / "build_run_video.py"), str(task_dir)],
            ]
            for command in commands:
                try:
                    completed = subprocess.run(
                        command, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=300
                    )
                    message = (completed.stdout or completed.stderr).strip().splitlines()
                    if completed.returncode == 0:
                        self.log.append(f"[dashboard] {message[-1] if message else 'artifact built'}")
                    else:
                        self.log.append(f"[dashboard] artifact build failed: {message[-1] if message else completed.returncode}")
                except Exception as exc:  # noqa: BLE001
                    self.log.append(f"[dashboard] artifact build failed: {exc}")
            video_path = task_dir / "visualization" / "replay.mp4"
            with self.lock:
                self.artifact_building.discard(task_dir.name)
                if video_path.is_file() and video_path.stat().st_size > 0:
                    self.artifact_tasks.add(task_dir.name)
                    self.log.append(f"[dashboard] video saved for completed task {task_dir.name}: {video_path}")

    def _consume(self, proc: subprocess.Popen[str], log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as output:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                output.write(line + "\n")
                output.flush()
                with self.lock:
                    self.log.append(line)
        returncode = proc.wait()
        self._build_completed_artifacts()
        with self.lock:
            self.returncode = returncode
            self.ended_at = time.time()
            self.log.append(f"[dashboard] process exited with code {returncode}")

    def stop(self) -> bool:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                return False
            proc = self.proc
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        return True


JOB = JobState()


@app.get("/")
def index():
    return send_from_directory(ASSET_ROOT, "index.html")


@app.get("/assets/<path:name>")
def assets(name: str):
    return send_from_directory(ASSET_ROOT, name)


@app.get("/api/tasks")
def api_tasks():
    root = _resolved(request.args.get("osworld_root") or DEFAULT_OSWORLD_ROOT)
    provider = request.args.get("provider", "vmware")
    return jsonify(
        {
            "osworld_root": str(root),
            "tasks": discover_tasks(root),
            "checks": setup_checks(root, provider),
            "models": DASHBOARD_MODELS,
            "run_modes": ["single", "macu"],
            "live_stream": True,
            "shared_canvas_count": len(SHARED_CANVAS_TASKS),
        }
    )


@app.post("/api/estimate")
def api_estimate():
    payload = request.get_json(force=True) or {}
    try:
        run_mode = str(payload.get("run_mode") or "macu")
        if run_mode not in {"single", "macu"}:
            raise ValueError("Multi-Cursor mode is not available yet")
        manager_provider = str(payload.get("manager_provider") or "tinker")
        manager_model = str(payload.get("manager_model") or "thinkingmachines/Inkling-Small")
        task_count = len(payload.get("task_keys") or [])
        estimate = _estimate_with_cua_provider(
            manager_model=(manager_model if manager_provider == "tinker" else "thinkingmachines/Inkling-Small"),
            cua_provider=str(payload.get("cua_provider") or "tinker"),
            cua_model=str(payload.get("cua_model") or "thinkingmachines/Inkling-Small"),
            task_count=task_count,
            parallel_agents=(1 if run_mode == "single" else _bounded_int(payload, "max_parallelism", 3, 1, 16)),
        )
        if manager_provider in {"google", "openrouter"} and run_mode != "single":
            provider_models = GEMINI_MODELS if manager_provider == "google" else OPENROUTER_MODELS
            if manager_model not in provider_models:
                raise ValueError(f"Choose a {manager_provider} manager model")
            manager_calls = task_count * 5
            info = provider_models[manager_model]
            manager_cost = (
                manager_calls * 8_000 * info["prefill"]
                + manager_calls * 1_000 * info["sample"]
            ) / 1_000_000
            estimate["manager_calls"] = manager_calls
            estimate["manager_cost_usd"] = round(manager_cost, 4)
            estimate["estimated_cost_usd"] = round(manager_cost + estimate["cua_cost_usd"], 4)
            estimate["assumptions"] += f" {manager_provider.title()} manager pricing is estimated separately."
        if run_mode == "single":
            estimate["manager_calls"] = 0
            estimate["manager_cost_usd"] = 0.0
            estimate["estimated_cost_usd"] = estimate["cua_cost_usd"]
            estimate["assumptions"] = "Single CUA per task; no manager graph, aggregation, or replanning calls."
        cap_usd = float(payload.get("max_cost_usd", 15.0))
        if not 0 < cap_usd <= 15.0:
            raise ValueError("Cost cap must be greater than $0 and at most $15")
        estimate["cap_usd"] = cap_usd
        estimate["within_cap"] = estimate["estimated_cost_usd"] <= cap_usd
        return jsonify(estimate)
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/status")
def api_status():
    requested_root = request.args.get("result_root")
    result_root = _resolved(requested_root) if requested_root else (JOB.result_root or DEFAULT_RESULT_ROOT)
    budget = _read_json(result_root / "_budget" / "tinker_spend.json") or {
        "limit_usd": 15.0, "spent_usd": 0.0, "reserved_usd": 0.0
    }
    budget.pop("reservations", None)
    return jsonify({"job": JOB.snapshot(), "results": collect_results(result_root), "budget": budget})


@app.get("/api/live-agents")
def api_live_agents():
    requested_root = request.args.get("result_root")
    result_root = _resolved(requested_root) if requested_root else (JOB.result_root or DEFAULT_RESULT_ROOT)
    return jsonify({"agents": collect_live_agents(result_root)})


@app.get("/api/live-stream")
def api_live_stream():
    """Push worker/DAG changes to the browser as server-sent events."""
    requested_root = request.args.get("result_root")
    result_root = _resolved(requested_root) if requested_root else (JOB.result_root or DEFAULT_RESULT_ROOT)

    @stream_with_context
    def events():
        previous = ""
        while True:
            payload = {
                "agents": collect_live_agents(result_root),
                "job_running": JOB.snapshot().get("running", False),
                "emitted_at": time.time(),
            }
            encoded = json.dumps(payload, separators=(",", ":"))
            comparison = json.dumps(payload["agents"], sort_keys=True, separators=(",", ":"))
            if comparison != previous:
                previous = comparison
                yield f"event: workers\ndata: {encoded}\n\n"
            else:
                yield ": heartbeat\n\n"
            time.sleep(1)

    return Response(
        events(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/live-frame/<task_id>/<agent_id>")
def api_live_frame(task_id: str, agent_id: str):
    requested_root = request.args.get("result_root")
    result_root = _resolved(requested_root) if requested_root else (JOB.result_root or DEFAULT_RESULT_ROOT)
    task_dir = (result_root / task_id).resolve()
    agent_dir = (task_dir / agent_id).resolve()
    if not _inside(task_dir, result_root) or not _inside(agent_dir, task_dir):
        abort(404)
    frame, _ = _latest_agent_frame(agent_dir)
    if frame is None:
        abort(404)
    return send_file(frame, mimetype="image/png", conditional=False, max_age=0)


def _choice(payload: dict[str, Any], name: str, allowed: set[str], default: str) -> str:
    value = str(payload.get(name) or default)
    if value not in allowed:
        raise ValueError(f"Invalid {name}: {value}")
    return value


def _bounded_int(payload: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = int(payload.get(name, default))
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def _clear_stale_task_claim(result_root: Path, task_id: str) -> None:
    """Make an interrupted dashboard task retryable without touching completed runs."""
    task_dir = (result_root / task_id).resolve()
    if not _inside(task_dir, result_root):
        raise ValueError(f"Unsafe task id: {task_id}")
    if any((task_dir / marker).is_file() for marker in ("final_results.json", "final_result.json", "result.txt")):
        return
    claim_path = task_dir / ".claimed"
    if claim_path.is_file():
        owner_lines = claim_path.read_text(encoding="utf-8", errors="replace").splitlines()
        owner = owner_lines[0] if owner_lines else ""
        try:
            owner_pid = int(owner.rsplit(":", 1)[-1])
            os.kill(owner_pid, 0)
        except (ValueError, ProcessLookupError, PermissionError):
            claim_path.unlink(missing_ok=True)
            app.logger.info("Cleared stale task claim for %s (%s)", task_id, owner or "unknown")
        else:
            raise ValueError(f"Task {task_id} is still claimed by live process {owner}")
    (task_dir / "run_error.json").unlink(missing_ok=True)


@app.post("/api/launch")
def api_launch():
    payload = request.get_json(force=True) or {}
    try:
        osworld_root = _resolved(payload.get("osworld_root") or DEFAULT_OSWORLD_ROOT)
        examples_root = osworld_root / "evaluation_examples"
        if not (examples_root / "examples").is_dir():
            raise ValueError(f"OSWorld examples not found under {examples_root}")

        available = {task["key"]: task for task in discover_tasks(osworld_root)}
        selected_keys = payload.get("task_keys") or []
        if not selected_keys:
            raise ValueError("Select at least one OSWorld task")
        if len(selected_keys) > 50:
            raise ValueError("Select at most 50 tasks per dashboard launch")
        unknown = sorted(set(selected_keys) - set(available))
        if unknown:
            raise ValueError(f"Unknown task selection: {unknown[:3]}")

        result_root = _resolved(payload.get("result_root") or DEFAULT_RESULT_ROOT)
        result_root.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, list[str]] = {}
        for key in selected_keys:
            task = available[key]
            _clear_stale_task_claim(result_root, task["id"])
            manifest.setdefault(task["domain"], []).append(task["id"])
        dashboard_dir = result_root / "_dashboard"
        dashboard_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = dashboard_dir / f"tasks_{int(time.time())}.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        run_mode = _choice(payload, "run_mode", {"single", "macu"}, "macu")
        manager_provider = _choice(payload, "manager_provider", {"tinker", "google", "openrouter"}, "tinker")
        cua_provider = _choice(payload, "cua_provider", {"tinker", "openai", "qwen"}, "tinker")
        vm_provider = _choice(payload, "vm_provider", {"vmware", "apptainer"}, "vmware")
        max_parallelism = 1 if run_mode == "single" else _bounded_int(payload, "max_parallelism", 3, 1, 16)
        max_steps = _bounded_int(payload, "max_steps", 60, 1, 500)
        max_replans = _bounded_int(payload, "max_replans", 10, 0, 50)
        manager_model = str(payload.get("manager_model") or "thinkingmachines/Inkling-Small").strip()
        cua_model = str(payload.get("cua_model") or "thinkingmachines/Inkling-Small").strip()
        if manager_provider == "tinker" and manager_model not in TINKER_MODELS:
            raise ValueError("Choose a manager model from the Tinker catalog")
        if manager_provider == "google" and manager_model not in GEMINI_MODELS:
            raise ValueError("Choose a Gemini manager model")
        if manager_provider == "openrouter" and manager_model not in OPENROUTER_MODELS:
            raise ValueError("Choose an OpenRouter fallback model")
        gemini_key = str(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
        openrouter_key = str(os.environ.get("OPENROUTER_API_KEY") or "").strip()
        if manager_provider == "google" and run_mode != "single" and not gemini_key:
            raise ValueError("Set GEMINI_API_KEY in .env and restart the dashboard")
        if manager_provider == "openrouter" and run_mode != "single" and not openrouter_key:
            raise ValueError("Set OPENROUTER_API_KEY in .env and restart the dashboard")
        cua_catalog = TINKER_MODELS if cua_provider == "tinker" else OPENAI_CUA_MODELS if cua_provider == "openai" else QWEN_CUA_MODELS
        if cua_model not in cua_catalog or not cua_catalog[cua_model].get("image_input"):
            raise ValueError(f"Choose a vision-enabled {cua_provider} CUA model")
        if cua_provider == "openai" and not os.environ.get("OPENAI_API_KEY", "").strip():
            raise ValueError("Set OPENAI_API_KEY in .env before using an OpenAI CUA model")
        if cua_provider == "qwen" and not os.environ.get("OPENAI_BASE_URL", "").strip():
            raise ValueError("Set OPENAI_BASE_URL in .env to your Qwen OpenAI-compatible server")
        max_cost_usd = float(payload.get("max_cost_usd", 15.0))
        if not 0 < max_cost_usd <= 15.0:
            raise ValueError("The experiment cost cap must be greater than $0 and at most $15")
        estimate = _estimate_with_cua_provider(
            manager_model if manager_provider == "tinker" else "thinkingmachines/Inkling-Small",
            cua_provider,
            cua_model,
            len(selected_keys),
            max_parallelism,
        )
        if manager_provider in {"google", "openrouter"} and run_mode != "single":
            manager_calls = len(selected_keys) * 5
            info = (GEMINI_MODELS if manager_provider == "google" else OPENROUTER_MODELS)[manager_model]
            manager_cost = (
                manager_calls * 8_000 * info["prefill"]
                + manager_calls * 1_000 * info["sample"]
            ) / 1_000_000
            estimate["manager_cost_usd"] = round(manager_cost, 4)
            estimate["estimated_cost_usd"] = round(manager_cost + estimate["cua_cost_usd"], 4)
        if run_mode == "single":
            estimate["manager_cost_usd"] = 0.0
            estimate["estimated_cost_usd"] = estimate["cua_cost_usd"]
        if estimate["estimated_cost_usd"] > max_cost_usd:
            raise ValueError(
                f"Estimated cost ${estimate['estimated_cost_usd']:.2f} exceeds the "
                f"${max_cost_usd:.2f} cap. Select fewer tasks/agents or cheaper models."
            )

        runtime_manager_provider = "openai" if manager_provider == "openrouter" else manager_provider
        command = [
            sys.executable,
            str(PROJECT_ROOT / "run_macu.py"),
            str(manifest_path),
            "--result-dir", str(result_root),
            "--osworld-root", str(osworld_root),
            "--osworld-data-dir", str(examples_root),
            "--manager-provider", runtime_manager_provider,
            "--manager-model", manager_model,
            "--cua-provider", cua_provider,
            "--max-parallelism", str(max_parallelism),
            "--max-replans", str(max_replans),
            "--max-cost-usd", str(max_cost_usd),
            "--post_task_wait_seconds", str(float(payload.get("post_task_wait_seconds", 10.0))),
        ]
        if run_mode == "single":
            command.append("--no-manager")
        command.extend([
            "--",
            "--headless",
            "--provider_name", vm_provider,
            "--model", cua_model,
            "--coord", "absolute",
            "--max_steps", str(max_steps),
            "--sleep_after_execution", str(float(payload.get("sleep_after_execution", 3.0))),
            "--env_ready_wait_seconds", str(float(payload.get("env_ready_wait_seconds", 10.0))),
        ])
        log_path = dashboard_dir / "dashboard.log"
        env_overrides = None
        if manager_provider == "google":
            env_overrides = {"GEMINI_API_KEY": gemini_key}
        elif manager_provider == "openrouter":
            env_overrides = {
                "OPENAI_API_KEY": openrouter_key,
                "MACU_MANAGER_BASE_URL": "https://openrouter.ai/api/v1",
            }
        JOB.start(command, result_root, log_path, env_overrides=env_overrides)
        return jsonify({"ok": True, "job": JOB.snapshot(), "manifest": str(manifest_path)})
    except (ValueError, TypeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409


@app.post("/api/stop")
def api_stop():
    return jsonify({"ok": JOB.stop()})


@app.post("/api/cleanup")
def api_cleanup():
    """Stop all local MACU workers and return OSWorld pool VMs to free state."""
    payload = request.get_json(silent=True) or {}
    requested_root = payload.get("result_root")
    result_root = _resolved(requested_root) if requested_root else (JOB.result_root or DEFAULT_RESULT_ROOT)
    JOB.stop()
    cleanup_dashboard_runtime(PROJECT_ROOT, DEFAULT_OSWORLD_ROOT, result_root)
    return jsonify({"ok": True, "message": "Runs stopped and VM pool released"})


@app.post("/api/visualize/<task_id>")
def api_visualize(task_id: str):
    snapshot = JOB.snapshot()
    result_root_value = (request.get_json(silent=True) or {}).get("result_root") or snapshot.get("result_root")
    result_root = _resolved(result_root_value or DEFAULT_RESULT_ROOT)
    task_dir = (result_root / task_id).resolve()
    if not _inside(task_dir, result_root) or not (task_dir / "final_results.json").is_file():
        abort(404)
    commands = [
        [sys.executable, str(PROJECT_ROOT / "scripts" / "visualize_run.py"), str(task_dir)],
        [sys.executable, str(PROJECT_ROOT / "scripts" / "build_run_video.py"), str(task_dir)],
    ]
    for command in commands:
        result = subprocess.run(
            command, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr or result.stdout}), 500
    return jsonify({
        "ok": True,
        "url": f"/view/{task_id}/",
        "video_url": f"/view/{task_id}/replay.mp4",
    })


@app.get("/view/<task_id>/")
@app.get("/view/<task_id>/<path:name>")
def view_visualization(task_id: str, name: str = "index.html"):
    result_root = JOB.result_root or DEFAULT_RESULT_ROOT
    visual_root = (result_root / task_id / "visualization").resolve()
    if not _inside(visual_root, result_root) or not visual_root.is_dir():
        abort(404)
    return send_from_directory(visual_root, name)


def _clean_interrupted_dashboard_state() -> None:
    """Release dead local claims whenever the dashboard is restarted."""
    if DEFAULT_RESULT_ROOT.is_dir():
        for task_dir in DEFAULT_RESULT_ROOT.iterdir():
            if not task_dir.is_dir() or task_dir.name.startswith("_"):
                continue
            try:
                _clear_stale_task_claim(DEFAULT_RESULT_ROOT, task_dir.name)
            except ValueError:
                pass  # A live worker still owns this task.

    registry = DEFAULT_OSWORLD_ROOT / ".vmware_vms"
    if registry.is_file():
        changed = False
        output: list[str] = []
        for line in registry.read_text(encoding="utf-8", errors="replace").splitlines():
            vm_path, separator, owner = line.partition("|")
            if not separator or owner == "free":
                output.append(line)
                continue
            try:
                os.kill(int(owner), 0)
            except (ValueError, ProcessLookupError, PermissionError):
                output.append(f"{vm_path}|free")
                changed = True
            else:
                output.append(line)
        if changed:
            registry.write_text("\n".join(output) + "\n", encoding="utf-8")
            app.logger.info("Released stale VMware claims in %s", registry)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: local-only)")
    parser.add_argument("--port", type=int, default=5055)
    parser.add_argument("--open", action="store_true", help="Open the dashboard in the default browser")
    args = parser.parse_args()
    _clean_interrupted_dashboard_state()
    if args.open:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{args.host}:{args.port}")).start()
    print(f"MACU dashboard: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
