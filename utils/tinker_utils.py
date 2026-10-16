"""Tinker model metadata, pricing, and cross-process spend guard."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid
from typing import Optional

from filelock import FileLock

TINKER_ANTHROPIC_BASE_URL = "https://tinker.thinkingmachines.dev/services/tinker-prod/anthropic/api"

# Prices are USD per million tokens, fetched from Thinking Machines' published
# models.json on 2026-10-16. Keep the source URL visible in the dashboard.
TINKER_MODELS = {
    "Qwen/Qwen3.8-27B": {
        "name": "Qwen3.8-27B", "family": "Qwen", "context": "64K",
        "type": "Hybrid + Vision", "vision": True, "image_input": False,
        "prefill": 0.6975, "cached_prefill": 0.1395, "sample": 5.595,
    },
    "Qwen/Qwen3.5-397B-A17B": {
        "name": "Qwen3.5-397B-A17B", "family": "Qwen", "context": "64K",
        "type": "Hybrid + Vision", "vision": True, "image_input": False,
        "prefill": 1.65, "cached_prefill": 0.33, "sample": 7.50,
        "note": "Tinker Anthropic image input not verified; manager text mode",
    },
    "thinkingmachines/Inkling-Small": {
        "name": "Inkling-Small", "family": "Thinking Machines", "context": "64K",
        "type": "Hybrid + Audio + Vision", "vision": True, "image_input": True,
        "prefill": 0.58, "cached_prefill": 0.116, "sample": 1.44,
        "note": "Limited-time 50% discount",
    },
    "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-BF16": {
        "name": "Nemotron-3.5-Super-VL-120B-A12B", "family": "NVIDIA", "context": "64K",
        "type": "Hybrid + Vision", "vision": True, "image_input": False,
        "prefill": 0.57, "cached_prefill": 0.114, "sample": 1.44,
        "note": "Limited-time 50% discount",
    },
    "moonshotai/Kimi-K2.6": {
        "name": "Kimi-K2.6", "family": "Moonshot", "context": "32K",
        "type": "Hybrid + Vision", "vision": True, "image_input": False,
        "prefill": 2.205, "cached_prefill": 0.441, "sample": 5.49,
    },
    "openai/gpt-oss-120b": {
        "name": "GPT-OSS-120B", "family": "OpenAI", "context": "32K",
        "type": "Reasoning", "vision": False, "image_input": False,
        "prefill": 0.33, "cached_prefill": 0.066, "sample": 0.84,
        "note": "Manager only: no vision input",
    },
}


def tinker_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    info = TINKER_MODELS.get(model)
    if not info:
        raise ValueError(f"Unknown Tinker model pricing: {model}")
    return (
        max(0, int(input_tokens)) * info["prefill"]
        + max(0, int(output_tokens)) * info["sample"]
    ) / 1_000_000


class TinkerBudgetExceeded(RuntimeError):
    pass


class TinkerSpendGuard:
    """Reserve worst-case call cost in a file shared by manager and CUA processes."""

    def __init__(self) -> None:
        self.limit = float(os.environ.get("MACU_MAX_COST_USD", "15"))
        raw_path = os.environ.get("MACU_BUDGET_FILE")
        self.path = Path(raw_path).resolve() if raw_path else None

    def _initial(self) -> dict:
        return {"limit_usd": self.limit, "spent_usd": 0.0, "reserved_usd": 0.0, "reservations": {}}

    def _read(self) -> dict:
        if self.path is None or not self.path.exists():
            return self._initial()
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._initial()

    def _write(self, state: dict) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def reserve(self, model: str, input_tokens: int, max_output_tokens: int) -> Optional[str]:
        if self.path is None or self.limit <= 0:
            return None
        amount = tinker_cost(model, input_tokens, max_output_tokens)
        reservation_id = uuid.uuid4().hex
        with FileLock(str(self.path) + ".lock"):
            state = self._read()
            committed = float(state.get("spent_usd", 0.0))
            reserved = float(state.get("reserved_usd", 0.0))
            if committed + reserved + amount > self.limit + 1e-9:
                raise TinkerBudgetExceeded(
                    f"Tinker budget guard blocked a call reserving ${amount:.4f}: "
                    f"${committed:.4f} spent + ${reserved:.4f} reserved, cap ${self.limit:.2f}."
                )
            state.setdefault("reservations", {})[reservation_id] = {
                "model": model, "reserved_usd": amount, "created_at": time.time(),
                "input_tokens": input_tokens, "max_output_tokens": max_output_tokens,
            }
            state["reserved_usd"] = reserved + amount
            state["limit_usd"] = self.limit
            self._write(state)
        return reservation_id

    def settle(self, reservation_id: Optional[str], model: str, input_tokens: int, output_tokens: int) -> float:
        actual = tinker_cost(model, input_tokens, output_tokens)
        if self.path is None:
            return actual
        with FileLock(str(self.path) + ".lock"):
            state = self._read()
            reservation = state.setdefault("reservations", {}).pop(reservation_id, {}) if reservation_id else {}
            held = float(reservation.get("reserved_usd", 0.0))
            state["reserved_usd"] = max(0.0, float(state.get("reserved_usd", 0.0)) - held)
            state["spent_usd"] = float(state.get("spent_usd", 0.0)) + actual
            self._write(state)
        return actual

    def release(self, reservation_id: Optional[str]) -> None:
        if self.path is None or not reservation_id:
            return
        with FileLock(str(self.path) + ".lock"):
            state = self._read()
            reservation = state.setdefault("reservations", {}).pop(reservation_id, {})
            state["reserved_usd"] = max(
                0.0,
                float(state.get("reserved_usd", 0.0)) - float(reservation.get("reserved_usd", 0.0)),
            )
            self._write(state)


def estimate_experiment_cost(
    manager_model: str,
    cua_model: str,
    task_count: int,
    parallel_agents: int,
    expected_steps: int = 20,
) -> dict:
    """Transparent planning estimate; runtime spend is protected separately."""
    manager_calls = max(0, task_count) * 5
    cua_calls = max(0, task_count) * max(1, parallel_agents) * max(1, expected_steps)
    manager_cost = tinker_cost(manager_model, manager_calls * 8_000, manager_calls * 1_000)
    cua_cost = tinker_cost(cua_model, cua_calls * 12_000, cua_calls * 500)
    return {
        "manager_calls": manager_calls,
        "cua_calls": cua_calls,
        "manager_cost_usd": round(manager_cost, 4),
        "cua_cost_usd": round(cua_cost, 4),
        "estimated_cost_usd": round(manager_cost + cua_cost, 4),
        "assumptions": "5 manager calls/task at 8k input + 1k output; agents × 20 CUA turns/task at 12k input + 500 output.",
    }
