import json
from pathlib import Path

from scripts.macu_dashboard import (
    _clear_stale_task_claim,
    app,
    collect_live_agents,
    collect_results,
    discover_tasks,
)


def _write_task(root: Path, domain: str, task_id: str, instruction: str) -> None:
    path = root / "evaluation_examples" / "examples" / domain / f"{task_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "id": task_id,
                "instruction": instruction,
                "related_apps": [domain],
                "snapshot": "init_state",
            }
        ),
        encoding="utf-8",
    )


def test_discover_tasks_focuses_on_gimp_and_calc(tmp_path):
    _write_task(tmp_path, "gimp", "g-1", "Edit an image")
    _write_task(tmp_path, "libreoffice_calc", "c-1", "Update a spreadsheet")
    _write_task(tmp_path, "chrome", "w-1", "Browse a website")

    tasks = discover_tasks(tmp_path)

    assert [task["key"] for task in tasks] == [
        "gimp/g-1",
        "libreoffice_calc/c-1",
    ]
    assert tasks[1]["instruction"] == "Update a spreadsheet"


def test_collect_results_summarizes_completed_run(tmp_path):
    task_dir = tmp_path / "g-1"
    task_dir.mkdir()
    (task_dir / "dependency_graph.json").write_text(
        json.dumps(
            {
                "domain": "gimp",
                "instruction": "Edit an image",
                "dependency_graph": {
                    "subtasks": [{"id": "a"}, {"id": "b"}],
                },
            }
        ),
        encoding="utf-8",
    )
    (task_dir / "final_results.json").write_text(
        json.dumps(
            {
                "domain": "gimp",
                "instruction": "Edit an image",
                "osworld_evaluator_score": 0.75,
                "total_duration_seconds": 12.5,
                "total_cost_usd": 0.2,
                "final_response": "Done",
                "replanning": {"num_applied": 2},
                "subtask_results": {
                    "a": {"status": "success"},
                    "b": {"status": "error"},
                },
            }
        ),
        encoding="utf-8",
    )

    results = collect_results(tmp_path)

    assert len(results) == 1
    assert results[0]["status"] == "completed"
    assert results[0]["score"] == 0.75
    assert results[0]["subtask_count"] == 2
    assert results[0]["success_count"] == 1
    assert results[0]["error_count"] == 1
    assert results[0]["replans"] == 2


def test_live_agents_exposes_latest_frame_and_activity(tmp_path):
    task_dir = tmp_path / "task-1"
    agent_dir = task_dir / "agent-a"
    trajectory_dir = agent_dir / "results" / "pyautogui" / "screenshot" / "model"
    trajectory_dir.mkdir(parents=True)
    (task_dir / "dependency_graph.json").write_text(
        json.dumps({"dependency_graph": {"subtasks": [{
            "id": "agent-a", "agent_type": "cua", "description": "Edit the sheet",
            "instruction": "Enter totals", "dependencies": [],
        }]}}),
        encoding="utf-8",
    )
    (agent_dir / "vm_info.json").write_text(
        json.dumps({"vm_path": "/vms/worker-one.vmx"}), encoding="utf-8"
    )
    (trajectory_dir / "step_1.png").write_bytes(b"fake-png")
    (trajectory_dir / "traj.jsonl").write_text(
        json.dumps({
            "step_num": 1,
            "response": "I will enter the total.<tool_call>",
            "screenshot_file": "step_1.png",
        }) + "\n",
        encoding="utf-8",
    )

    agents = collect_live_agents(tmp_path)

    assert len(agents) == 1
    assert agents[0]["agent_id"] == "agent-a"
    assert agents[0]["vm"] == "worker-one"
    assert agents[0]["step"] == 1
    assert agents[0]["activity"] == "I will enter the total."
    assert agents[0]["frame_version"] is not None

    client = app.test_client()
    response = client.get(
        "/api/live-frame/task-1/agent-a",
        query_string={"result_root": str(tmp_path)},
    )
    assert response.status_code == 200
    assert response.data == b"fake-png"


def test_gemini_manager_estimate_keeps_inkling_as_cua():
    client = app.test_client()
    response = client.post(
        "/api/estimate",
        json={
            "run_mode": "macu",
            "manager_provider": "google",
            "manager_model": "gemini-3.1-flash-lite-preview",
            "cua_model": "thinkingmachines/Inkling-Small",
            "task_keys": ["calc/task-1"],
            "max_parallelism": 2,
            "max_cost_usd": 15,
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["manager_calls"] == 5
    assert payload["manager_cost_usd"] > 0
    assert payload["cua_cost_usd"] > 0


def test_openrouter_fallback_estimate_supports_kimi_k3():
    client = app.test_client()
    response = client.post(
        "/api/estimate",
        json={
            "run_mode": "macu",
            "manager_provider": "openrouter",
            "manager_model": "moonshotai/kimi-k3",
            "cua_model": "thinkingmachines/Inkling-Small",
            "task_keys": ["calc/task-1"],
            "max_parallelism": 2,
            "max_cost_usd": 15,
        },
    )

    assert response.status_code == 200
    assert response.get_json()["manager_cost_usd"] > 0


def test_interrupted_task_claim_is_cleared_for_dashboard_retry(tmp_path):
    task_dir = tmp_path / "task-1"
    task_dir.mkdir()
    (task_dir / ".claimed").write_text("host:999999999\n0\n", encoding="utf-8")
    (task_dir / "run_error.json").write_text('{"error":"stopped"}', encoding="utf-8")

    _clear_stale_task_claim(tmp_path, "task-1")

    assert not (task_dir / ".claimed").exists()
    assert not (task_dir / "run_error.json").exists()
