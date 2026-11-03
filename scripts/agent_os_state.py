#!/usr/bin/env python3
"""Small JSON registry for Codex agent-control-room coordination."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ACTIVE = {"planned", "running", "waiting", "blocked", "review"}
STATUSES = ACTIVE | {"done", "paused"}
VERSION = 1


def default_state_path() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home / "agent-control-room" / "state.json"


def empty_state() -> dict:
    return {"version": VERSION, "tasks": []}


def load_state(path: Path) -> dict:
    if not path.exists():
        return empty_state()
    with path.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    if state.get("version") != VERSION or not isinstance(state.get("tasks"), list):
        raise ValueError(f"Unsupported or malformed state file: {path}")
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def find_task(state: dict, task_id: str) -> dict:
    for task in state["tasks"]:
        if task.get("id") == task_id:
            return task
    raise KeyError(task_id)


def read_list(values: list[str] | None) -> list[str]:
    return [item.strip() for value in values or [] for item in value.split(",") if item.strip()]


def validate_status(status: str) -> str:
    if status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(sorted(STATUSES))}")
    return status


def command_init(args: argparse.Namespace) -> None:
    path = Path(args.state).expanduser()
    if path.exists() and not args.force:
        print(json.dumps(load_state(path), ensure_ascii=False, indent=2))
        return
    save_state(path, empty_state())
    print(path)


def command_list(args: argparse.Namespace) -> None:
    state = load_state(Path(args.state).expanduser())
    tasks = state["tasks"]
    if args.status:
        selected = set(read_list(args.status))
        tasks = [task for task in tasks if task.get("status") in selected]
    print(json.dumps(tasks, ensure_ascii=False, indent=2))


def command_show(args: argparse.Namespace) -> None:
    state = load_state(Path(args.state).expanduser())
    print(json.dumps(find_task(state, args.id), ensure_ascii=False, indent=2))


def command_add(args: argparse.Namespace) -> None:
    path = Path(args.state).expanduser()
    state = load_state(path)
    try:
        find_task(state, args.id)
    except KeyError:
        pass
    else:
        raise ValueError(f"task already exists: {args.id}; use update")
    task = {
        "id": args.id,
        "title": args.title,
        "goal": args.goal,
        "status": validate_status(args.status),
        "thread_id": args.thread_id,
        "host_id": args.host_id,
        "project_id": args.project_id,
        "workspace": args.workspace,
        "depends_on": read_list(args.depends_on),
        "exclusive_resources": read_list(args.exclusive_resource),
        "next_action": args.next_action,
        "notes": args.notes,
        "updated_at": now(),
    }
    state["tasks"].append(task)
    save_state(path, state)
    print(json.dumps(task, ensure_ascii=False, indent=2))


def command_update(args: argparse.Namespace) -> None:
    path = Path(args.state).expanduser()
    state = load_state(path)
    task = find_task(state, args.id)
    scalar = [
        "title",
        "goal",
        "status",
        "thread_id",
        "host_id",
        "project_id",
        "workspace",
        "next_action",
        "notes",
        "thread_status",
        "last_turn_status",
        "last_turn_at",
        "last_summary",
    ]
    for field in scalar:
        value = getattr(args, field)
        if value is not None:
            task[field] = validate_status(value) if field == "status" else value
    if args.depends_on is not None:
        task["depends_on"] = read_list(args.depends_on)
    if args.exclusive_resource is not None:
        task["exclusive_resources"] = read_list(args.exclusive_resource)
    task["updated_at"] = now()
    save_state(path, state)
    print(json.dumps(task, ensure_ascii=False, indent=2))


def command_check(args: argparse.Namespace) -> None:
    state = load_state(Path(args.state).expanduser())
    active = [task for task in state["tasks"] if task.get("status") in ACTIVE]
    issues = []
    for field in ("workspace", "exclusive_resources"):
        owners: dict[str, list[str]] = {}
        for task in active:
            raw = task.get(field, [])
            values = raw if isinstance(raw, list) else [raw]
            for value in values:
                if value:
                    owners.setdefault(value, []).append(task["id"])
        for value, task_ids in owners.items():
            if len(task_ids) > 1:
                resource = "writable workspace" if field == "workspace" else "exclusive resource"
                issues.append({"type": resource, "value": value, "tasks": task_ids})
    result = {"ok": not issues, "issues": issues}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if issues:
        raise SystemExit(2)


def command_summary(args: argparse.Namespace) -> None:
    state = load_state(Path(args.state).expanduser())
    order = {"blocked": 0, "review": 1, "running": 2, "waiting": 3, "planned": 4, "paused": 5, "done": 6}
    tasks = sorted(state["tasks"], key=lambda task: (order.get(task.get("status"), 99), task.get("id", "")))
    if not tasks:
        print("No registered tasks.")
        return
    for task in tasks:
        resource = ", ".join(task.get("exclusive_resources", [])) or "none"
        next_action = task.get("next_action") or "none"
        print(f"- [{task.get('status', '?')}] {task.get('title', task.get('id'))} (`{task.get('id')}`)")
        print(f"  goal: {task.get('goal', '')}")
        print(f"  resource: {resource}; next: {next_action}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--state", default=str(default_state_path()), help="registry JSON path")
    sub = root.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a registry")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=command_init)

    show_list = sub.add_parser("list", help="print task records")
    show_list.add_argument("--status", nargs="+")
    show_list.set_defaults(func=command_list)

    show = sub.add_parser("show", help="print one task")
    show.add_argument("id")
    show.set_defaults(func=command_show)

    add = sub.add_parser("add", help="add a task")
    add.add_argument("--id", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--goal", required=True)
    add.add_argument("--status", default="planned")
    add.add_argument("--thread-id")
    add.add_argument("--host-id")
    add.add_argument("--project-id")
    add.add_argument("--workspace")
    add.add_argument("--depends-on", action="append")
    add.add_argument("--exclusive-resource", action="append")
    add.add_argument("--next-action")
    add.add_argument("--notes")
    add.set_defaults(func=command_add)

    update = sub.add_parser("update", help="update task fields")
    update.add_argument("id")
    for field in (
        "title",
        "goal",
        "status",
        "thread-id",
        "host-id",
        "project-id",
        "workspace",
        "next-action",
        "notes",
        "thread-status",
        "last-turn-status",
        "last-turn-at",
        "last-summary",
    ):
        update.add_argument(f"--{field}", dest=field.replace("-", "_"))
    update.add_argument("--depends-on", action="append")
    update.add_argument("--exclusive-resource", action="append")
    update.set_defaults(func=command_update)

    check = sub.add_parser("check", help="find active workspace/resource conflicts")
    check.set_defaults(func=command_check)

    summary = sub.add_parser("summary", help="print a compact text summary")
    summary.set_defaults(func=command_summary)
    return root


def main() -> None:
    args = parser().parse_args()
    try:
        args.func(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
