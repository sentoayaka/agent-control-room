#!/usr/bin/env python3
"""Local read-only dashboard for the agent-control-room Skill."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent_os_state import ACTIVE, default_state_path, load_state

VERSION = "0.1.0"
CACHE_SECONDS = 8
RESOURCE_CACHE_SECONDS = 30


def default_resource_path() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home / "agent-control-room" / "resources.json"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_from_epoch(value: int | float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class AppServerError(RuntimeError):
    pass


class AppServerClient:
    """Small newline-delimited JSON-RPC client for Codex app-server stdio."""

    def __init__(self, executable: str = "codex", timeout: float = 12.0) -> None:
        self.timeout = timeout
        self.proc = subprocess.Popen(
            [executable, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.selector = selectors.DefaultSelector()
        if self.proc.stdout is not None:
            self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        self.buffer = bytearray()
        self.next_id = 1
        self._initialize()

    def _send(self, message: dict) -> None:
        if self.proc.stdin is None:
            raise AppServerError("app-server stdin is closed")
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        self.proc.stdin.write(payload)
        self.proc.stdin.flush()

    def _read_message(self) -> dict:
        if self.proc.stdout is None:
            raise AppServerError("app-server stdout is closed")
        deadline = time.monotonic() + self.timeout
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self.buffer[:newline]).strip()
                del self.buffer[: newline + 1]
                if line:
                    return json.loads(line.decode("utf-8"))
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError("timed out waiting for app-server")
            events = self.selector.select(remaining)
            if not events:
                raise AppServerError("timed out waiting for app-server")
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                detail = ""
                if self.proc.stderr is not None:
                    detail = self.proc.stderr.read().decode("utf-8", "replace").strip()
                raise AppServerError(detail or "app-server exited")
            self.buffer.extend(chunk)

    def _wait_for_response(self, request_id: int) -> dict:
        while True:
            message = self._read_message()
            if message.get("id") == request_id and ("result" in message or "error" in message):
                if "error" in message:
                    raise AppServerError(json.dumps(message["error"], ensure_ascii=False))
                return message.get("result") or {}

    def _initialize(self) -> None:
        self._send(
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "agent_control_room_dashboard",
                        "title": "Agent Control Room",
                        "version": VERSION,
                    }
                },
            }
        )
        self._wait_for_response(0)
        self._send({"method": "initialized", "params": {}})

    def request(self, method: str, params: dict | None = None) -> dict:
        request_id = self.next_id
        self.next_id += 1
        message = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        self._send(message)
        return self._wait_for_response(request_id)

    def close(self) -> None:
        self.selector.close()
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=3)

    def __enter__(self) -> "AppServerClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def run_command(command: list[str], timeout: float = 4.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def local_metrics() -> dict:
    cpu_count = os.cpu_count() or 1
    load_1, load_5, load_15 = os.getloadavg()
    result = {
        "id": "local",
        "host": "local",
        "status": "ok",
        "cpu_count": cpu_count,
        "load_1": round(load_1, 2),
        "load_5": round(load_5, 2),
        "load_15": round(load_15, 2),
    }
    try:
        proc = run_command(["ps", "-A", "-o", "%cpu="], timeout=2)
        values = [float(value) for value in proc.stdout.split() if value]
        result["cpu_percent"] = round(min(sum(values) / cpu_count, 100.0), 1)
        result["process_count"] = len(values)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        result["cpu_percent"] = None
        result["process_count"] = None
    try:
        total_memory = int(run_command(["sysctl", "-n", "hw.memsize"], timeout=2).stdout.strip())
        vm = run_command(["vm_stat"], timeout=2).stdout
        page_size = 4096
        if "page size of" in vm:
            page_size = int(vm.split("page size of", 1)[1].split("bytes", 1)[0].strip())
        stats: dict[str, int] = {}
        for line in vm.splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            number = raw.strip().rstrip(".")
            if number.isdigit():
                stats[key.strip()] = int(number)
        free_pages = stats.get("Pages free", 0) + stats.get("Pages inactive", 0)
        available = free_pages * page_size
        result["memory_total_bytes"] = total_memory
        result["memory_available_bytes"] = available
        result["memory_percent"] = round((1 - available / total_memory) * 100, 1)
    except (OSError, ValueError, subprocess.TimeoutExpired, ZeroDivisionError):
        result["memory_total_bytes"] = None
        result["memory_available_bytes"] = None
        result["memory_percent"] = None
    try:
        disk = shutil.disk_usage("/")
        result["disk_percent"] = round(disk.used / disk.total * 100, 1)
        result["disk_total_bytes"] = disk.total
        result["disk_free_bytes"] = disk.free
    except OSError:
        result["disk_percent"] = None
    return result


def nvidia_probe(probe: dict) -> dict:
    target = probe.get("target", "local")
    query = "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits"
    if target == "local":
        command = ["sh", "-lc", query]
    else:
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=2",
            "-o",
            "ServerAliveInterval=2",
            "-o",
            "ServerAliveCountMax=1",
            target,
            query,
        ]
    started = time.monotonic()
    try:
        proc = run_command(command, timeout=float(probe.get("timeout_seconds", 4)))
    except subprocess.TimeoutExpired:
        return {"id": probe.get("id", target), "host": target, "status": "unavailable", "error": "probe timed out"}
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip().splitlines()
        return {
            "id": probe.get("id", target),
            "host": target,
            "status": "unavailable",
            "error": message[-1] if message else f"probe exited {proc.returncode}",
        }
    gpus = []
    for line in proc.stdout.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) < 5:
            continue
        try:
            gpus.append(
                {
                    "index": int(fields[0]),
                    "name": fields[1],
                    "utilization_percent": int(fields[2]),
                    "memory_used_mib": int(fields[3]),
                    "memory_total_mib": int(fields[4]),
                }
            )
        except ValueError:
            continue
    return {
        "id": probe.get("id", target),
        "host": target,
        "status": "ok",
        "gpus": gpus,
        "latency_ms": round((time.monotonic() - started) * 1000),
    }


_resource_lock = threading.Lock()
_resource_cache: tuple[float, dict] | None = None


def load_resource_config(path: Path) -> dict:
    if not path.exists():
        return {"probes": []}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("probes", []), list):
        raise ValueError(f"Malformed resource config: {path}")
    return data


def resource_snapshot(path: Path, force: bool = False) -> dict:
    global _resource_cache
    with _resource_lock:
        if not force and _resource_cache and time.monotonic() - _resource_cache[0] < RESOURCE_CACHE_SECONDS:
            return _resource_cache[1]
        probes = [{"id": "local", "host": "local", "type": "local", "enabled": True}]
        try:
            probes.extend(load_resource_config(path).get("probes", []))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            probes = [
                {"id": "local", "host": "local", "type": "local", "enabled": True},
                {"id": "config", "host": "config", "status": "error", "error": str(exc)},
            ]
        hosts = []
        for probe in probes:
            if not probe.get("enabled", True):
                continue
            probe_type = probe.get("type", "local")
            if probe.get("status") == "error":
                hosts.append(probe)
            elif probe_type == "local":
                hosts.append(local_metrics())
            elif probe_type in {"nvidia-smi", "ssh-nvidia-smi"}:
                hosts.append(nvidia_probe(probe))
        payload = {"hosts": hosts, "captured_at": iso_now()}
        _resource_cache = (time.monotonic(), payload)
        return payload


def app_server_snapshot(state_path: Path, limit: int, detail_limit: int, timeout: float) -> dict:
    registry = load_state(state_path)
    tasks = registry["tasks"]
    registered_ids = {task.get("thread_id") for task in tasks if task.get("thread_id")}
    data = {
        "ok": False,
        "error": None,
        "threads": [],
        "rate_limits": None,
        "account_usage": None,
        "thread_details": {},
    }
    try:
        with AppServerClient(timeout=timeout) as client:
            listed = client.request(
                "thread/list",
                {
                    "limit": limit,
                    "sortKey": "updated_at",
                    "sortDirection": "desc",
                    "useStateDbOnly": True,
                },
            )
            threads = listed.get("data", [])
            try:
                data["rate_limits"] = client.request(
                    "account/rateLimits/read", {"excludeResetCreditDetails": True}
                )
            except AppServerError as exc:
                data["rate_limits"] = {"error": str(exc)}
            try:
                data["account_usage"] = client.request("account/usage/read", {})
            except AppServerError as exc:
                data["account_usage"] = {"error": str(exc)}
            detail_threads = []
            seen = set()
            for thread in threads:
                if thread.get("id") in registered_ids:
                    detail_threads.append(thread)
                    seen.add(thread.get("id"))
            for thread in threads:
                if len(detail_threads) >= detail_limit:
                    break
                if thread.get("id") not in seen:
                    detail_threads.append(thread)
                    seen.add(thread.get("id"))
            for thread in detail_threads:
                thread_id = thread.get("id")
                detail = {"last_turn": None, "usage": None, "errors": []}
                try:
                    turns = client.request(
                        "thread/turns/list",
                        {
                            "threadId": thread_id,
                            "limit": 1,
                            "sortDirection": "desc",
                            "itemsView": "summary",
                        },
                    )
                    rows = turns.get("data", [])
                    detail["last_turn"] = rows[0] if rows else None
                except AppServerError as exc:
                    detail["errors"].append(f"turns: {exc}")
                if thread_id in registered_ids:
                    try:
                        usage = client.request("account/usage/read", {"threadId": thread_id})
                        detail["usage"] = usage.get("threadUsage")
                    except AppServerError as exc:
                        detail["errors"].append(f"usage: {exc}")
                data["thread_details"][thread_id] = detail
            data["threads"] = threads
            data["ok"] = True
    except (AppServerError, OSError, ValueError, json.JSONDecodeError) as exc:
        data["error"] = str(exc)
    return data


def derive_effective_status(task: dict | None, thread: dict | None, detail: dict | None) -> tuple[str, str | None]:
    registry_status = task.get("status") if task else None
    if not thread:
        return registry_status or "unknown", "登记的任务找不到对应线程" if task else None
    live = thread.get("status", {})
    live_type = live.get("type", "unknown")
    flags = live.get("activeFlags", [])
    last_turn = (detail or {}).get("last_turn") or {}
    turn_status = last_turn.get("status")
    if "waitingOnApproval" in flags:
        return "waiting", "线程正在等待审批"
    if "waitingOnUserInput" in flags:
        return "waiting", "线程正在等待用户输入"
    if live_type == "systemError" or turn_status == "failed":
        return "failed", "线程或最近一轮执行失败"
    if live_type == "active" or turn_status == "inProgress":
        return "running", None
    if registry_status in {"blocked", "waiting", "paused", "review", "done", "planned"}:
        return registry_status, None
    if registry_status == "running" and live_type in {"idle", "notLoaded"}:
        return "idle", "任务登记为运行中，但线程当前空闲"
    if turn_status == "interrupted":
        return "paused", "最近一轮被中断"
    if registry_status:
        return registry_status, None
    return "idle", None


def registry_conflicts(tasks: list[dict]) -> list[dict]:
    active = [task for task in tasks if task.get("status") in ACTIVE]
    issues = []
    for field, label in (("workspace", "writable workspace"), ("exclusive_resources", "exclusive resource")):
        owners: dict[str, list[str]] = {}
        for task in active:
            raw = task.get(field, [])
            values = raw if isinstance(raw, list) else [raw]
            for value in values:
                if value:
                    owners.setdefault(value, []).append(task["id"])
        for value, ids in owners.items():
            if len(ids) > 1:
                issues.append({"type": label, "value": value, "tasks": ids})
    return issues


def merge_snapshot(state_path: Path, resource_path: Path, limit: int, detail_limit: int, timeout: float, force_resources: bool) -> dict:
    registry = load_state(state_path)
    registry_tasks = registry["tasks"]
    codex = app_server_snapshot(state_path, limit, detail_limit, timeout)
    threads = {thread.get("id"): thread for thread in codex["threads"]}
    board_tasks = []
    matched_thread_ids = set()
    for task in registry_tasks:
        thread_id = task.get("thread_id")
        thread = threads.get(thread_id)
        detail = codex["thread_details"].get(thread_id, {})
        status, attention = derive_effective_status(task, thread, detail)
        if thread_id:
            matched_thread_ids.add(thread_id)
        last_turn = detail.get("last_turn") or {}
        board_tasks.append(
            {
                "id": task["id"],
                "title": task.get("title") or (thread or {}).get("name") or task["id"],
                "goal": task.get("goal"),
                "registered": True,
                "status": status,
                "registry_status": task.get("status"),
                "thread_id": thread_id,
                "thread_status": (thread or {}).get("status"),
                "last_turn": last_turn,
                "attention": attention,
                "workspace": task.get("workspace") or (thread or {}).get("cwd"),
                "host_id": task.get("host_id"),
                "project_id": task.get("project_id") or (thread or {}).get("projectId"),
                "resources": task.get("exclusive_resources", []),
                "next_action": task.get("next_action"),
                "last_summary": task.get("last_summary") or task.get("notes"),
                "updated_at": iso_from_epoch((thread or {}).get("updatedAt")) or task.get("updated_at"),
                "usage": (detail or {}).get("usage"),
            }
        )
    for thread in codex["threads"]:
        thread_id = thread.get("id")
        if thread_id in matched_thread_ids:
            continue
        detail = codex["thread_details"].get(thread_id, {})
        status, attention = derive_effective_status(None, thread, detail)
        board_tasks.append(
            {
                "id": f"thread:{thread_id}",
                "title": thread.get("name") or thread.get("preview", "Untitled thread").splitlines()[0][:100],
                "goal": None,
                "registered": False,
                "status": status,
                "registry_status": None,
                "thread_id": thread_id,
                "thread_status": thread.get("status"),
                "last_turn": detail.get("last_turn"),
                "attention": attention,
                "workspace": thread.get("cwd"),
                "host_id": None,
                "project_id": thread.get("projectId"),
                "resources": [],
                "next_action": None,
                "last_summary": None,
                "updated_at": iso_from_epoch(thread.get("updatedAt")),
                "usage": detail.get("usage"),
            }
        )
    conflicts = registry_conflicts(registry_tasks)
    conflict_tasks = {task_id for issue in conflicts for task_id in issue["tasks"]}
    for task in board_tasks:
        if task["id"] in conflict_tasks:
            task["attention"] = "存在工作区或独占资源冲突"
            if task["status"] not in {"failed", "waiting"}:
                task["status"] = "blocked"
    leases: dict[str, list[str]] = {}
    for task in registry_tasks:
        for resource in task.get("exclusive_resources", []):
            leases.setdefault(resource, []).append(task["id"])
    resources = resource_snapshot(resource_path, force=force_resources)
    resources["leases"] = [{"resource": key, "tasks": ids} for key, ids in sorted(leases.items())]
    resources["conflicts"] = conflicts
    summary = {
        "total": len(board_tasks),
        "registered": sum(1 for task in board_tasks if task["registered"]),
        "running": sum(1 for task in board_tasks if task["status"] == "running"),
        "attention": sum(1 for task in board_tasks if task["attention"] or task["status"] in {"blocked", "failed", "waiting"}),
        "done": sum(1 for task in board_tasks if task["status"] == "done"),
        "conflicts": len(conflicts),
    }
    return {
        "generated_at": iso_now(),
        "state_path": str(state_path),
        "codex": {
            "ok": codex["ok"],
            "error": codex["error"],
        },
        "rate_limits": codex["rate_limits"],
        "account_usage": codex["account_usage"],
        "summary": summary,
        "tasks": board_tasks,
        "resources": resources,
    }


class SnapshotCache:
    def __init__(self, state_path: Path, resource_path: Path, limit: int, detail_limit: int, timeout: float) -> None:
        self.state_path = state_path
        self.resource_path = resource_path
        self.limit = limit
        self.detail_limit = detail_limit
        self.timeout = timeout
        self.lock = threading.Lock()
        self.cached_at = 0.0
        self.payload: dict | None = None

    def get(self, force: bool = False) -> dict:
        with self.lock:
            if not force and self.payload is not None and time.monotonic() - self.cached_at < CACHE_SECONDS:
                return self.payload
            self.payload = merge_snapshot(
                self.state_path,
                self.resource_path,
                self.limit,
                self.detail_limit,
                self.timeout,
                force,
            )
            self.cached_at = time.monotonic()
            return self.payload


class Handler(BaseHTTPRequestHandler):
    cache: SnapshotCache

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/snapshot":
            force = parse_qs(parsed.query).get("refresh", ["0"])[0] == "1"
            try:
                payload = self.cache.get(force=force)
                self._json(200, payload)
            except Exception as exc:  # keep the dashboard alive and visible
                self._json(500, {"generated_at": iso_now(), "error": str(exc)})
            return
        if parsed.path == "/healthz":
            self._json(200, {"ok": True, "version": VERSION})
            return
        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if parsed.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        if os.environ.get("AGENT_CONTROL_ROOM_ACCESS_LOG") == "1":
            super().log_message(format, *args)


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent Control Room</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0d1117; --panel: #151b24; --panel2: #1b2330; --line: #293345;
      --text: #eef3fa; --muted: #93a0b4; --accent: #7dd3fc; --blue: #60a5fa;
      --green: #4ade80; --yellow: #fbbf24; --red: #fb7185; --purple: #c084fc;
      --shadow: 0 18px 60px rgba(0,0,0,.28);
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: radial-gradient(circle at 20% -10%, #1c3b54 0, transparent 34%), var(--bg); color: var(--text); font: 14px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; min-height: 100vh; }
    button, input { font: inherit; }
    .shell { max-width: 1500px; margin: 0 auto; padding: 28px; }
    header { display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; margin-bottom: 24px; }
    .eyebrow { color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: 11px; font-weight: 750; }
    h1 { margin: 5px 0 4px; font-size: clamp(28px, 4vw, 46px); line-height: 1.05; letter-spacing: -.04em; }
    .subtitle { margin: 0; color: var(--muted); max-width: 760px; }
    .live { display: flex; align-items: center; gap: 10px; padding: 9px 13px; background: rgba(74,222,128,.09); border: 1px solid rgba(74,222,128,.25); border-radius: 999px; color: var(--green); white-space: nowrap; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; box-shadow: 0 0 14px currentColor; }
    .stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 14px; }
    .card { background: linear-gradient(145deg, rgba(27,35,48,.92), rgba(21,27,36,.96)); border: 1px solid var(--line); border-radius: 16px; box-shadow: var(--shadow); }
    .stat { padding: 17px 18px; }
    .stat-label { color: var(--muted); font-size: 12px; }
    .stat-value { display: flex; gap: 9px; align-items: baseline; margin-top: 4px; font-size: 30px; font-weight: 760; letter-spacing: -.04em; }
    .stat-note { color: var(--muted); font-size: 12px; margin-top: 1px; }
    .usage-strip { padding: 16px 18px; display: grid; grid-template-columns: 1.2fr 1fr 1fr; gap: 20px; margin-bottom: 18px; }
    .usage-copy { min-width: 0; }
    .usage-copy strong { display: block; margin-bottom: 2px; }
    .bar { height: 7px; border-radius: 999px; background: #283141; overflow: hidden; margin: 9px 0 5px; }
    .bar > span { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--blue), var(--accent)); }
    .small { font-size: 12px; color: var(--muted); }
    .workspace { display: grid; grid-template-columns: minmax(0, 2.15fr) minmax(320px, .85fr); gap: 18px; align-items: start; }
    .board, .side { padding: 18px; }
    .section-head { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 14px; }
    .section-head h2 { margin: 0; font-size: 18px; letter-spacing: -.02em; }
    .filters { display: flex; gap: 6px; flex-wrap: wrap; }
    .filter { border: 1px solid var(--line); color: var(--muted); background: transparent; border-radius: 9px; padding: 6px 10px; cursor: pointer; }
    .filter.active { background: var(--text); color: var(--bg); border-color: var(--text); }
    .search { width: min(260px, 100%); padding: 9px 11px; border-radius: 10px; border: 1px solid var(--line); background: #101620; color: var(--text); outline: none; }
    .search:focus { border-color: var(--accent); }
    .notice { padding: 11px 13px; border: 1px solid rgba(251,191,36,.32); background: rgba(251,191,36,.08); color: #fde68a; border-radius: 11px; margin-bottom: 12px; }
    .notice.error { border-color: rgba(251,113,133,.32); background: rgba(251,113,133,.08); color: #fecdd3; }
    .task-list { display: grid; gap: 9px; }
    .task { display: grid; grid-template-columns: 100px minmax(0, 1fr) 138px; gap: 13px; align-items: center; padding: 14px; border-radius: 13px; border: 1px solid var(--line); background: rgba(13,17,23,.42); cursor: pointer; transition: border-color .15s, transform .15s, background .15s; }
    .task:hover { border-color: #40506b; transform: translateY(-1px); background: rgba(26,35,47,.74); }
    .badge { justify-self: start; padding: 4px 8px; border-radius: 999px; font-size: 11px; font-weight: 750; letter-spacing: .02em; border: 1px solid currentColor; }
    .running { color: var(--blue); background: rgba(96,165,250,.08); }
    .waiting, .blocked { color: var(--yellow); background: rgba(251,191,36,.08); }
    .failed { color: var(--red); background: rgba(251,113,133,.08); }
    .done { color: var(--green); background: rgba(74,222,128,.08); }
    .review { color: var(--purple); background: rgba(192,132,252,.08); }
    .planned, .paused, .idle, .unknown { color: var(--muted); background: rgba(147,160,180,.07); }
    .task-title { font-weight: 690; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .task-meta { margin-top: 3px; color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .task-side { text-align: right; }
    .task-cost { font-variant-numeric: tabular-nums; }
    .task-updated { color: var(--muted); font-size: 11px; margin-top: 3px; }
    .empty { border: 1px dashed var(--line); border-radius: 12px; padding: 28px; text-align: center; color: var(--muted); }
    .side-section + .side-section { border-top: 1px solid var(--line); margin-top: 17px; padding-top: 17px; }
    .side-section h3 { margin: 0 0 10px; font-size: 13px; color: var(--muted); font-weight: 650; }
    .host { padding: 12px; background: rgba(13,17,23,.4); border: 1px solid var(--line); border-radius: 11px; margin-bottom: 9px; }
    .host:last-child { margin-bottom: 0; }
    .host-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 8px; }
    .host-name { font-weight: 680; }
    .metric { display: grid; grid-template-columns: 78px minmax(0, 1fr) 44px; gap: 8px; align-items: center; margin-top: 6px; font-size: 12px; }
    .metric .track { height: 5px; background: #283141; border-radius: 999px; overflow: hidden; }
    .metric .fill { height: 100%; background: var(--blue); border-radius: inherit; }
    .metric .value { text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; }
    .lease { display: flex; justify-content: space-between; gap: 8px; padding: 8px 0; border-bottom: 1px solid rgba(41,51,69,.6); }
    .lease:last-child { border-bottom: 0; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; overflow-wrap: anywhere; }
    .conflict { color: var(--red); }
    .unmanaged { color: var(--muted); font-size: 11px; }
    .drawer-bg { position: fixed; inset: 0; background: rgba(0,0,0,.52); opacity: 0; pointer-events: none; transition: opacity .18s; z-index: 20; }
    .drawer-bg.open { opacity: 1; pointer-events: auto; }
    .drawer { position: absolute; top: 0; right: 0; bottom: 0; width: min(560px, 94vw); background: #131922; border-left: 1px solid var(--line); padding: 24px; overflow: auto; box-shadow: -20px 0 70px rgba(0,0,0,.4); }
    .drawer h2 { margin: 8px 0 3px; font-size: 24px; letter-spacing: -.03em; }
    .drawer .close { float: right; border: 1px solid var(--line); background: transparent; color: var(--text); border-radius: 9px; padding: 6px 10px; cursor: pointer; }
    .kv { display: grid; grid-template-columns: 110px minmax(0,1fr); gap: 10px; padding: 9px 0; border-bottom: 1px solid var(--line); }
    .kv dt { color: var(--muted); }
    .kv dd { margin: 0; overflow-wrap: anywhere; }
    .drawer h3 { margin: 22px 0 6px; font-size: 14px; }
    .thread-link { display: inline-block; margin-top: 18px; color: var(--bg); background: var(--accent); text-decoration: none; font-weight: 750; padding: 9px 13px; border-radius: 10px; }
    footer { color: var(--muted); font-size: 11px; margin-top: 16px; text-align: right; }
    @media (max-width: 980px) { .workspace { grid-template-columns: 1fr; } .usage-strip { grid-template-columns: 1fr 1fr; } }
    @media (max-width: 720px) { .shell { padding: 18px; } header { flex-direction: column; } .stats { grid-template-columns: 1fr 1fr; } .usage-strip { grid-template-columns: 1fr; } .task { grid-template-columns: 1fr; } .task-side { text-align: left; } .section-head { align-items: flex-start; flex-direction: column; } }
  </style>
</head>
<body>
<div class="shell">
  <header>
    <div>
      <div class="eyebrow">Single attention surface</div>
      <h1>Agent Control Room</h1>
      <p class="subtitle">把多个 Codex 对话收敛成一个可观察、可判断、可调度的控制面。数据来自本机 app-server，页面只读。</p>
    </div>
    <div class="live"><span class="dot"></span><span id="live-label">连接中</span></div>
  </header>

  <section class="stats">
    <div class="card stat"><div class="stat-label">正在运行</div><div class="stat-value"><span id="stat-running">—</span><span class="small">个任务</span></div><div class="stat-note">来自实时线程状态</div></div>
    <div class="card stat"><div class="stat-label">需要处理</div><div class="stat-value"><span id="stat-attention">—</span><span class="small">项</span></div><div class="stat-note">审批、故障、冲突或空闲异常</div></div>
    <div class="card stat"><div class="stat-label">已纳管</div><div class="stat-value"><span id="stat-registered">—</span><span class="small">/ <span id="stat-total">—</span></span></div><div class="stat-note">登记工作流 / 当前可见对话</div></div>
    <div class="card stat"><div class="stat-label">资源冲突</div><div class="stat-value"><span id="stat-conflicts">—</span><span class="small">项</span></div><div class="stat-note">工作区、GPU、端口等租约</div></div>
  </section>

  <section class="card usage-strip">
    <div class="usage-copy"><strong>Codex 额度</strong><div id="rate-primary-label" class="small">读取中</div><div class="bar"><span id="rate-primary" style="width:0%"></span></div><div id="rate-primary-note" class="small">—</div></div>
    <div class="usage-copy"><strong>周额度</strong><div id="rate-secondary-label" class="small">读取中</div><div class="bar"><span id="rate-secondary" style="width:0%"></span></div><div id="rate-secondary-note" class="small">—</div></div>
    <div class="usage-copy"><strong>账户用量</strong><div id="account-tokens" style="font-size:24px;font-weight:760;margin-top:3px">—</div><div id="account-note" class="small">按 app-server 返回的数据显示</div></div>
  </section>

  <main class="workspace">
    <section class="card board">
      <div class="section-head">
        <div><h2>任务流</h2><div class="small">点击任务查看线程、费用和下一步</div></div>
        <input id="search" class="search" placeholder="搜索任务、路径或线程 ID">
      </div>
      <div class="filters" style="margin-bottom:14px">
        <button class="filter active" data-filter="active">活跃</button>
        <button class="filter" data-filter="attention">需要处理</button>
        <button class="filter" data-filter="all">全部</button>
        <button class="filter" data-filter="done">已完成</button>
      </div>
      <div id="notices"></div>
      <div id="task-list" class="task-list"></div>
    </section>

    <aside class="card side">
      <div class="side-section"><h3>主机与算力</h3><div id="hosts"></div></div>
      <div class="side-section"><h3>资源租约</h3><div id="leases"></div></div>
      <div class="side-section"><h3>使用说明</h3><div class="small">每 10 秒自动刷新；线程费用由 app-server 按账户能力返回，缺失时保持“不可用”，不会用 token 数推算费用。</div></div>
    </aside>
  </main>
  <footer id="footer">—</footer>
</div>

<div id="drawer-bg" class="drawer-bg" onclick="if(event.target===this)closeDrawer()">
  <aside class="drawer">
    <button class="close" onclick="closeDrawer()">关闭</button>
    <div id="drawer-content"></div>
  </aside>
</div>

<script>
const statusLabels = { running:'运行中', waiting:'等待', blocked:'阻塞', failed:'失败', done:'完成', review:'待验收', planned:'已计划', paused:'已暂停', idle:'空闲', unknown:'未知' };
let snapshot = null;
let currentFilter = 'active';
let searchText = '';

function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}
function fmtInt(value) { return value == null ? '—' : new Intl.NumberFormat('zh-CN').format(value); }
function fmtTime(value) {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return esc(value);
  return d.toLocaleString('zh-CN', {hour12:false});
}
function fmtAge(value) {
  if (!value) return '—';
  const ms = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(ms)) return '—';
  const mins = Math.max(0, Math.round(ms / 60000));
  if (mins < 1) return '刚刚'; if (mins < 60) return mins + ' 分钟前';
  const hrs = Math.round(mins / 60); if (hrs < 48) return hrs + ' 小时前';
  return Math.round(hrs / 24) + ' 天前';
}
function usageText(usage) {
  if (!usage) return '费用不可用';
  if (usage.estimatedUsageUsdMicros != null) return '$' + (usage.estimatedUsageUsdMicros / 1e6).toFixed(4);
  if (usage.estimatedUsageCreditsMicros != null) return (usage.estimatedUsageCreditsMicros / 1e6).toFixed(2) + ' credits';
  return '费用不可用';
}
function turnText(turn) {
  if (!turn) return '无已存轮次';
  const duration = turn.durationMs == null ? '' : ` · ${(turn.durationMs/1000).toFixed(1)}s`;
  return `${turn.status || 'unknown'}${duration}`;
}
function taskMatches(task) {
  if (currentFilter === 'active' && ['done','paused','planned'].includes(task.status) && !task.attention) return false;
  if (currentFilter === 'attention' && !task.attention && !['waiting','blocked','failed'].includes(task.status)) return false;
  if (currentFilter === 'done' && task.status !== 'done') return false;
  if (searchText) {
    const blob = [task.title,task.workspace,task.thread_id,task.id,task.next_action,task.goal].join(' ').toLowerCase();
    if (!blob.includes(searchText)) return false;
  }
  return true;
}
function render() {
  if (!snapshot) return;
  const s = snapshot.summary || {};
  document.getElementById('stat-running').textContent = s.running ?? '—';
  document.getElementById('stat-attention').textContent = s.attention ?? '—';
  document.getElementById('stat-registered').textContent = s.registered ?? '—';
  document.getElementById('stat-total').textContent = s.total ?? '—';
  document.getElementById('stat-conflicts').textContent = s.conflicts ?? '—';
  document.getElementById('footer').textContent = `快照 ${fmtTime(snapshot.generated_at)} · ${snapshot.state_path || ''}`;
  const live = document.getElementById('live-label');
  live.textContent = snapshot.codex?.ok ? 'app-server 已连接' : '降级模式';
  live.parentElement.style.color = snapshot.codex?.ok ? 'var(--green)' : 'var(--yellow)';
  renderRates();
  renderNotices();
  renderTasks();
  renderResources();
}
function renderRates() {
  const limits = snapshot.rate_limits?.rateLimits || {};
  const pairs = [['primary','rate-primary-label','rate-primary','rate-primary-note'],['secondary','rate-secondary-label','rate-secondary','rate-secondary-note']];
  for (const [key,labelId,barId,noteId] of pairs) {
    const item = limits[key];
    const label = document.getElementById(labelId), bar = document.getElementById(barId), note = document.getElementById(noteId);
    if (!item) { label.textContent = '不可用'; bar.style.width = '0%'; note.textContent = '—'; continue; }
    label.textContent = `${item.windowDurationMins ? (item.windowDurationMins/60).toFixed(item.windowDurationMins%60?1:0)+' 小时窗口' : key} · 已用 ${item.usedPercent}%`;
    bar.style.width = Math.max(0, Math.min(100, item.usedPercent)) + '%';
    note.textContent = item.resetsAt ? `重置于 ${fmtTime(item.resetsAt*1000)}` : '未提供重置时间';
  }
  const summary = snapshot.account_usage?.summary || {};
  document.getElementById('account-tokens').textContent = fmtInt(summary.lifetimeTokens);
  const buckets = snapshot.account_usage?.dailyUsageBuckets;
  document.getElementById('account-note').textContent = buckets?.length ? `最近 ${buckets.length} 个日桶 · 峰值 ${fmtInt(summary.peakDailyTokens)}` : (summary.lifetimeTokens == null ? '账户级 token 明细不可用' : '账户累计 token');
}
function renderNotices() {
  const out = [];
  if (!snapshot.codex?.ok) out.push(`<div class="notice error">app-server 不可用：${esc(snapshot.codex?.error || '未知错误')}。下方面板继续显示任务登记。</div>`);
  const conflicts = snapshot.resources?.conflicts || [];
  if (conflicts.length) out.push(`<div class="notice">检测到 ${conflicts.length} 项资源冲突，相关任务已标为阻塞。</div>`);
  document.getElementById('notices').innerHTML = out.join('');
}
function renderTasks() {
  const tasks = (snapshot.tasks || []).filter(taskMatches);
  const root = document.getElementById('task-list');
  if (!tasks.length) { root.innerHTML = '<div class="empty">当前筛选下没有任务</div>'; return; }
  root.innerHTML = tasks.map(task => {
    const status = task.status || 'unknown';
    const metaParts = [task.attention, task.next_action, task.workspace].filter(Boolean);
    const registered = task.registered ? '' : ' · 未纳管';
    return `<article class="task" onclick="openDrawer('${esc(task.id)}')">
      <span class="badge ${esc(status)}">${esc(statusLabels[status] || status)}</span>
      <div><div class="task-title">${esc(task.title || 'Untitled')}<span class="unmanaged">${registered}</span></div><div class="task-meta">${esc(metaParts.join(' · ') || task.thread_id || '—')}</div></div>
      <div class="task-side"><div class="task-cost">${esc(usageText(task.usage))}</div><div class="task-updated">${esc(fmtAge(task.updated_at))}</div></div>
    </article>`;
  }).join('');
}
function metric(label, value, max=100, suffix='%') {
  const width = value == null ? 0 : Math.max(0, Math.min(100, value));
  return `<div class="metric"><span>${esc(label)}</span><div class="track"><div class="fill" style="width:${width}%"></div></div><span class="value">${value == null ? '—' : esc(value + suffix)}</span></div>`;
}
function renderResources() {
  const hostRoot = document.getElementById('hosts');
  const hosts = snapshot.resources?.hosts || [];
  hostRoot.innerHTML = hosts.length ? hosts.map(host => {
    if (host.status === 'unavailable' || host.status === 'error') {
      return `<div class="host"><div class="host-head"><span class="host-name">${esc(host.host || host.id)}</span><span class="badge failed">不可用</span></div><div class="small">${esc(host.error || 'probe unavailable')}</div></div>`;
    }
    if (host.gpus) {
      const gpuHtml = host.gpus.map(gpu => {
        const pct = Math.round(gpu.memory_used_mib / Math.max(1,gpu.memory_total_mib) * 100);
        return `<div class="host"><div class="host-head"><span class="host-name">${esc(host.host)} · GPU ${esc(gpu.index)}</span><span class="small">${esc(gpu.name)}</span></div>${metric('利用率',gpu.utilization_percent)}${metric('显存',pct)}<div class="small">${fmtInt(gpu.memory_used_mib)} / ${fmtInt(gpu.memory_total_mib)} MiB</div></div>`;
      }).join('');
      return gpuHtml || `<div class="host"><div class="host-head"><span class="host-name">${esc(host.host)}</span><span class="badge planned">无 GPU</span></div></div>`;
    }
    return `<div class="host"><div class="host-head"><span class="host-name">本机</span><span class="small">${esc(host.cpu_count)} cores</span></div>${metric('CPU',host.cpu_percent)}${metric('内存',host.memory_percent)}${metric('磁盘',host.disk_percent)}<div class="small">load ${esc(host.load_1)} / ${esc(host.load_5)} / ${esc(host.load_15)} · ${esc(host.process_count ?? '—')} processes</div></div>`;
  }).join('') : '<div class="small">没有资源探针</div>';
  const leaseRoot = document.getElementById('leases');
  const leases = snapshot.resources?.leases || [];
  const conflicts = new Set((snapshot.resources?.conflicts || []).map(x => x.value));
  leaseRoot.innerHTML = leases.length ? leases.map(item => `<div class="lease ${conflicts.has(item.resource)?'conflict':''}"><div><div class="mono">${esc(item.resource)}</div><div class="small">${esc(item.tasks.join(', '))}</div></div><span>${esc(item.tasks.length)}</span></div>`).join('') : '<div class="small">尚无独占资源租约</div>';
}
function openDrawer(id) {
  const task = snapshot?.tasks?.find(item => item.id === id);
  if (!task) return;
  const turn = task.last_turn || {};
  const usage = task.usage;
  const groups = usage?.groups || [];
  const tokenTotal = groups.reduce((sum,g) => sum + (g.totalTokens || 0), 0);
  const link = task.thread_id ? `<a class="thread-link" href="codex://threads/${encodeURIComponent(task.thread_id)}">在 Codex 中打开线程</a>` : '';
  document.getElementById('drawer-content').innerHTML = `
    <span class="badge ${esc(task.status || 'unknown')}">${esc(statusLabels[task.status] || task.status || '未知')}</span>
    <h2>${esc(task.title)}</h2><div class="small">${task.registered ? '已纳管任务' : '发现但尚未纳管'}${task.attention ? ' · ' + esc(task.attention) : ''}</div>
    <dl><div class="kv"><dt>目标</dt><dd>${esc(task.goal || '—')}</dd></div><div class="kv"><dt>下一步</dt><dd>${esc(task.next_action || '—')}</dd></div><div class="kv"><dt>工作区</dt><dd class="mono">${esc(task.workspace || '—')}</dd></div><div class="kv"><dt>主机</dt><dd>${esc(task.host_id || '—')}</dd></div><div class="kv"><dt>线程</dt><dd class="mono">${esc(task.thread_id || '—')}</dd></div><div class="kv"><dt>最近轮次</dt><dd>${esc(turnText(task.last_turn))}</dd></div><div class="kv"><dt>更新时间</dt><dd>${esc(fmtTime(task.updated_at))}</dd></div><div class="kv"><dt>资源</dt><dd>${esc((task.resources || []).join(', ') || '—')}</dd></div><div class="kv"><dt>估算费用</dt><dd>${esc(usageText(usage))}</dd></div><div class="kv"><dt>估算 token</dt><dd>${tokenTotal ? fmtInt(tokenTotal) : '不可用'}</dd></div></dl>
    ${task.last_summary ? `<h3>最近摘要</h3><div class="small">${esc(task.last_summary)}</div>` : ''}
    ${task.thread_id && turn.error ? `<h3>最近错误</h3><div class="notice error">${esc(turn.error.message || JSON.stringify(turn.error))}</div>` : ''}
    ${link}
  `;
  document.getElementById('drawer-bg').classList.add('open');
}
function closeDrawer() { document.getElementById('drawer-bg').classList.remove('open'); }
async function refresh(force=false) {
  try {
    const response = await fetch('/api/snapshot' + (force?'?refresh=1':''), {cache:'no-store'});
    snapshot = await response.json();
    if (snapshot.error) throw new Error(snapshot.error);
    render();
  } catch (error) {
    document.getElementById('live-label').textContent = '刷新失败';
    document.getElementById('live-label').parentElement.style.color = 'var(--red)';
    document.getElementById('notices').innerHTML = `<div class="notice error">${esc(error.message)}</div>`;
  }
}
document.querySelectorAll('.filter').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('.filter').forEach(item => item.classList.remove('active'));
  button.classList.add('active'); currentFilter = button.dataset.filter; renderTasks();
}));
document.getElementById('search').addEventListener('input', event => { searchText = event.target.value.trim().toLowerCase(); renderTasks(); });
document.addEventListener('keydown', event => { if (event.key === 'Escape') closeDrawer(); if ((event.metaKey || event.ctrlKey) && event.key === 'r') { event.preventDefault(); refresh(true); } });
refresh(); setInterval(() => refresh(false), 10000);
</script>
</body>
</html>"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=str(default_state_path()), help="task registry JSON")
    parser.add_argument("--resources", default=str(default_resource_path()), help="resource probe config JSON")
    parser.add_argument("--limit", type=int, default=100, help="maximum threads to list")
    parser.add_argument("--detail-limit", type=int, default=30, help="maximum threads to enrich with turns and usage")
    parser.add_argument("--timeout", type=float, default=12.0, help="app-server request timeout in seconds")
    sub = parser.add_subparsers(dest="command", required=True)

    snapshot = sub.add_parser("snapshot", help="print one JSON or compact snapshot")
    snapshot.add_argument("--format", choices=["json", "compact"], default="json")
    snapshot.add_argument("--force-resources", action="store_true")

    serve = sub.add_parser("serve", help="serve the read-only local dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    state_path = Path(args.state).expanduser()
    resource_path = Path(args.resources).expanduser()
    if args.command == "snapshot":
        payload = merge_snapshot(state_path, resource_path, args.limit, args.detail_limit, args.timeout, args.force_resources)
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            summary = payload["summary"]
            print(
                f"total={summary['total']} running={summary['running']} attention={summary['attention']} "
                f"conflicts={summary['conflicts']} codex_ok={payload['codex']['ok']}"
            )
            for task in payload["tasks"]:
                marker = "!" if task.get("attention") else " "
                print(f"{marker} [{task['status']}] {task['title']} ({task['id']})")
            return
    cache = SnapshotCache(state_path, resource_path, args.limit, args.detail_limit, args.timeout)
    Handler.cache = cache
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Agent Control Room: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
