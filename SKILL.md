---
name: agent-control-room
description: Coordinate several long-running Codex tasks from one control thread, including thread creation, status monitoring, workspace isolation, resource conflicts, and one consolidated report back to the user.
---

# Agent Control Room

Use this skill when the user wants one conversation to manage several independent Codex tasks, reduce thread switching, coordinate shared hosts or workspaces, or receive one consolidated status report.

The control thread is the user's single attention surface. Its job is to route, monitor, and summarize work without carrying every task's full implementation context. It is not a kernel-level scheduler and cannot reserve hardware that the environment does not expose.

## Run the control loop

1. Read the active registry first. Use `scripts/agent_os_state.py list` with the state path configured by the user, or the default `$CODEX_HOME/agent-control-room/state.json`. Inspect live projects and threads when their status may have changed.
2. Turn the user's request into durable workstreams. Use one task thread for an independent objective that benefits from its own context. Keep short work, dependent reasoning, and quick decision points in the control thread.
3. Before creating a thread, define its objective, expected deliverable, completion condition, writable scope, host, and exclusive resources. A task is not ready to launch if two active tasks may write to the same checkout or use the same GPU, port, device, lock, or service without coordination.
4. Create each approved workstream as a Codex thread when the user asked for multi-task delegation. Put enough context in the initial prompt for the worker to act without needing the control conversation, and make its final response concise: outcome, artifacts, validation, blockers, and next action.
5. Prefer a managed worktree for concurrent writable Git work when the user authorized isolated worktrees. Otherwise serialize work in the shared checkout. Read-only tasks may share a workspace. Do not copy unrelated uncommitted changes or discard checkpoints, outputs, failures, or other worktree state.
6. Label exclusive resources consistently, for example `host:local/gpu:0`, `host:gpu-box/port:8000`, or `workspace:/path/repo#writes`. Verify live availability from the host instead of treating the registry as proof that a resource is free.
7. Record each task with the helper after creation and again when its phase, thread, resources, or next action changes. Do not store credentials or secret values in the registry.
8. Monitor with bounded waits and event snapshots instead of repeatedly polling. When a thread finishes or needs attention, read its result, update the registry, and tell the user only the decision-relevant changes.
9. Integrate completed work in the control thread. Check that artifacts exist, validation ran, and shared-resource assumptions still hold. Keep the user's one-chat interface while preserving each task thread's audit trail.

## Choose the execution unit

- Use a Codex thread for durable work that should remain visible, resumable, and independently reviewable.
- Use a subagent only when the task is a bounded branch of the current turn and the user has authorized delegation or parallel work.
- Use no delegation when the work is small, order-dependent, or would require frequent edits to the same files.
- Use a heartbeat or scheduled automation only when the user explicitly asks for continued unattended monitoring. A skill by itself runs only while Codex is invoked.

## Resource rules

Treat these as hard conflicts unless the tasks have an explicit coordination protocol:

- two writable task threads using the same checkout
- two jobs using the same GPU, accelerator, serial device, camera, robot, database migration lock, or singleton service
- two jobs binding the same host port
- one task modifying files that another task is validating

Read-only access can be shared. CPU and RAM may be shared when each task stays within observed headroom. If capacity is unknown, serialize the work or ask for a concrete measurement; never invent free capacity.

## Communication contract

Use a compact report shaped by the user's need:

- **需要你处理**: decisions, permissions, or blockers, ordered by urgency
- **正在运行**: task, current phase, resource, and latest useful result
- **可验收**: artifacts and validation evidence
- **刚完成/失败**: outcome and link to the task thread or artifact

Do not narrate unchanged polling. Do not mark work complete from a running thread, a plan, or a smoke test alone. Do not archive threads or close agents merely to tidy the list unless the user asked.

## State helper

Use `scripts/agent_os_state.py` for the cross-session registry. Run `python3 scripts/agent_os_state.py --help` from this skill directory when needed. The state file stores task metadata, not conversation histories or secrets.

Read `references/state-schema.md` when configuring task fields, documenting a resource lease, or interpreting a conflict result.

## Heartbeat sweep

When the user asks for unattended monitoring, create a heartbeat attached to the control thread. A practical default is every 10 minutes. During each sweep, read the registry, inspect each registered thread's live status and latest turn, update only changed task fields, and run the conflict check. Remain quiet when nothing actionable changed. Notify for a newly completed or failed task, a new approval/user-input request, a newly detected resource conflict, or a promised deadline. Do not restart workers, archive threads, or alter task files during a sweep without explicit authorization.

The heartbeat is the monitoring trigger. It does not make the Skill a daemon and should not be described as guaranteeing continuous execution.

## Dashboard

Run `scripts/control_room_dashboard.py serve` from a normal local terminal or an approved local command. It serves a read-only board at `http://127.0.0.1:8765` by default. Use `snapshot` for a headless status check.

The dashboard reads `thread/list`, `thread/turns/list`, `account/rateLimits/read`, and `account/usage/read` through Codex app-server. It merges those results with the task registry, reports unavailable values honestly, and probes resources described in `$CODEX_HOME/agent-control-room/resources.json`. The app-server process may need access to `$CODEX_HOME`; if the current sandbox rejects it, run the board outside the sandbox rather than weakening the dashboard's security.

See `references/operations.md` for resource probe configuration and operational limits.
