# Control-room operations

## Dashboard

Start the read-only board from a terminal:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/agent-control-room/scripts/control_room_dashboard.py" serve
```

Open `http://127.0.0.1:8765`. The server binds to loopback and exposes only `GET /`, `GET /api/snapshot`, and `GET /healthz`.

Print a headless snapshot:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/agent-control-room/scripts/control_room_dashboard.py" snapshot --format compact
```

The app-server must be able to access the Codex home directory. Run the board in a normal user terminal if the active agent sandbox blocks app-server startup.

## Resource probes

The default state directory is `$CODEX_HOME/agent-control-room/`. An optional `resources.json` adds read-only host probes:

```json
{
  "probes": [
    {
      "id": "gpu-box",
      "host": "gpu-box",
      "type": "ssh-nvidia-smi",
      "target": "gpu-box",
      "enabled": true,
      "timeout_seconds": 4
    }
  ]
}
```

Local CPU, memory, and disk metrics are always collected without a dependency. SSH probes use batch mode, a two-second connection timeout, and no credential storage. An unreachable host is displayed as unavailable; it is never treated as idle or free.

## Data semantics

- Thread status and latest turn come from Codex app-server.
- Usage and estimated cost are shown only when `account/usage/read` returns per-thread usage for the active account and billing route.
- A null usage value means unavailable, not zero.
- Resource leases come from the task registry. The dashboard checks configured hosts for observed occupancy, but it does not create operating-system locks.
- Remote GPU evidence is separate from local metrics and from registry intent.
