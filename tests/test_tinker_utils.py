import json

import pytest

from utils.tinker_utils import (
    TinkerBudgetExceeded,
    TinkerSpendGuard,
    estimate_experiment_cost,
    tinker_cost,
)


def test_tinker_cost_uses_published_per_million_rates():
    cost = tinker_cost("Qwen/Qwen3.8-27B", 1_000_000, 1_000_000)
    assert cost == pytest.approx(0.6975 + 5.595)


def test_estimate_reports_manager_and_cua_components():
    result = estimate_experiment_cost(
        "openai/gpt-oss-120b",
        "thinkingmachines/Inkling-Small",
        task_count=2,
        parallel_agents=3,
    )
    assert result["manager_calls"] == 10
    assert result["cua_calls"] == 120
    assert result["estimated_cost_usd"] == pytest.approx(
        result["manager_cost_usd"] + result["cua_cost_usd"]
    )


def test_spend_guard_reserves_settles_and_blocks(monkeypatch, tmp_path):
    budget_file = tmp_path / "budget.json"
    monkeypatch.setenv("MACU_BUDGET_FILE", str(budget_file))
    monkeypatch.setenv("MACU_MAX_COST_USD", "0.01")
    guard = TinkerSpendGuard()

    reservation = guard.reserve("openai/gpt-oss-120b", 1_000, 1_000)
    actual = guard.settle(reservation, "openai/gpt-oss-120b", 1_000, 500)

    state = json.loads(budget_file.read_text(encoding="utf-8"))
    assert state["spent_usd"] == pytest.approx(actual)
    assert state["reserved_usd"] == 0

    with pytest.raises(TinkerBudgetExceeded):
        guard.reserve("Qwen/Qwen3.8-27B", 1_000, 10_000)
