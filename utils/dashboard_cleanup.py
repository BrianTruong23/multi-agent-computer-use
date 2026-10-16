"""Fail-safe cleanup for dashboard-owned MACU processes and OSWorld VMs."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from typing import Optional

import psutil


def _is_macu_process(proc: psutil.Process, project_root: Path) -> bool:
    try:
        command = " ".join(proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    root = str(project_root.resolve())
    return root in command and ("run_macu.py" in command or "scripts/run_cua.py" in command)


def stop_macu_processes(project_root: Path, timeout: float = 10.0) -> list[int]:
    """Terminate all experiment processes launched from this checkout."""
    current_pid = os.getpid()
    processes = [
        proc for proc in psutil.process_iter(["pid"])
        if proc.pid != current_pid and _is_macu_process(proc, project_root)
    ]
    process_groups: set[int] = set()
    for proc in processes:
        try:
            process_groups.add(os.getpgid(proc.pid))
        except (ProcessLookupError, PermissionError):
            pass
    for pgid in process_groups:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    _, alive = psutil.wait_procs(processes, timeout=timeout)
    for proc in alive:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    return [proc.pid for proc in processes]


def stop_vmware_pool(osworld_root: Path) -> list[str]:
    """Hard-stop VMs registered in this OSWorld pool and mark them free."""
    registry = osworld_root / ".vmware_vms"
    if not registry.is_file():
        return []
    rows = registry.read_text(encoding="utf-8", errors="replace").splitlines()
    vm_paths: list[Path] = []
    for row in rows:
        raw_path, separator, _owner = row.partition("|")
        if not separator or not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            path = osworld_root / path
        vm_paths.append(path.resolve())

    vmrun = shutil.which("vmrun") or "/Applications/VMware Fusion.app/Contents/Library/vmrun"
    stopped: list[str] = []
    if Path(vmrun).exists() or shutil.which("vmrun"):
        for vm_path in vm_paths:
            try:
                subprocess.run(
                    [vmrun, "-T", "fusion", "stop", str(vm_path), "hard"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                stopped.append(str(vm_path))
            except (OSError, subprocess.SubprocessError):
                pass

    registry.write_text(
        "\n".join(f"{row.partition('|')[0]}|free" for row in rows if "|" in row) + "\n",
        encoding="utf-8",
    )
    return stopped


def clear_interrupted_claims(result_root: Path) -> None:
    if not result_root.is_dir():
        return
    for task_dir in result_root.iterdir():
        if not task_dir.is_dir() or task_dir.name.startswith("_"):
            continue
        completed = any((task_dir / name).is_file() for name in ("final_results.json", "final_result.json", "result.txt"))
        if not completed:
            (task_dir / ".claimed").unlink(missing_ok=True)


def cleanup_dashboard_runtime(project_root: Path, osworld_root: Path, result_root: Path) -> None:
    stop_macu_processes(project_root)
    stop_vmware_pool(osworld_root)
    clear_interrupted_claims(result_root)


def watch_dashboard(
    dashboard_pid: int,
    project_root: Path,
    osworld_root: Path,
    result_root: Path,
    port: Optional[int] = None,
) -> None:
    """Wait for a dashboard process to disappear, then perform fail-safe cleanup."""
    while psutil.pid_exists(dashboard_pid):
        time.sleep(1)
    cleanup_dashboard_runtime(project_root, osworld_root, result_root)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch-pid", type=int, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--osworld-root", type=Path, required=True)
    parser.add_argument("--result-root", type=Path, required=True)
    parser.add_argument("--port", type=int)
    options = parser.parse_args()
    watch_dashboard(
        options.watch_pid,
        options.project_root,
        options.osworld_root,
        options.result_root,
        options.port,
    )
