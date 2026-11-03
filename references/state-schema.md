# Control-room state

The helper stores a small JSON registry with one record per durable workstream. It is intentionally advisory: Codex thread lists and live host checks remain the source of truth for execution state and capacity.

## Fields

| Field | Purpose |
| --- | --- |
| `id` | Stable short identifier chosen by the coordinator |
| `title` | Concise task label |
| `goal` | Concrete deliverable or completion condition |
| `status` | `planned`, `running`, `waiting`, `blocked`, `review`, `done`, or `paused` |
| `thread_id`, `host_id` | Codex thread and execution host when known |
| `project_id` | Codex project when known |
| `workspace` | Writable checkout or working directory |
| `depends_on` | Task ids that must finish first |
| `exclusive_resources` | Canonical labels for singleton resources |
| `next_action` | The next concrete step, if any |
| `notes` | Non-secret coordination notes |
| `thread_status`, `last_turn_status`, `last_turn_at` | Last observed live thread state |
| `last_summary` | Short, evidence-based progress note for heartbeat comparisons |
| `updated_at` | Automatically maintained UTC timestamp |

## Conflict checks

`check` reports duplicate writable workspaces and duplicate exclusive resources across tasks whose status is `planned`, `running`, `waiting`, `blocked`, or `review`. It does not inspect the operating system. Confirm GPU, port, process, robot, and device state on the actual host before launching or retrying work.

## Example

```bash
python3 scripts/agent_os_state.py add \
  --id example-calibration \
  --title "Calibration audit" \
  --goal "Verify the baseline without changing it" \
  --status running \
  --thread-id 01abc \
  --host-id remote-host-id \
  --workspace /path/to/example \
  --exclusive-resource host:gpu-box/gpu:0 \
  --next-action "Inspect the fixed-baseline correspondence report"
```
