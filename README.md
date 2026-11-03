# Agent Control Room

A Codex Skill for coordinating several long-running conversations from one control thread.

Agent Control Room keeps the user-facing conversation as a single control surface while task threads retain their own context, artifacts, and audit trail. It adds a persistent task registry, workspace and resource conflict checks, a quiet heartbeat workflow, and a read-only local dashboard backed by Codex app-server.

It is an orchestration workflow, not a kernel-level agent operating system. It cannot reserve hardware that the host does not expose, and it does not claim continuous execution without a heartbeat or scheduler.

## Features

- Route independent work into durable Codex task threads.
- Track status, objective, next action, workspace, host, and exclusive resources in a small JSON registry.
- Detect concurrent writes to the same checkout and duplicate GPU, port, device, or service leases.
- Monitor registered threads with a Codex heartbeat and stay quiet when nothing actionable changed.
- Read live thread status and latest turns through Codex app-server.
- Display Codex rate-limit windows and account usage when the active account exposes them.
- Show per-thread estimated usage only when the billing route returns it. A null value remains unavailable, not zero.
- Collect local CPU, memory, disk, and process metrics without third-party Python packages.
- Optionally probe GPU hosts with a read-only SSH command.

## Requirements

- Codex desktop or Codex CLI with `codex app-server` available.
- Python 3.11 or newer.
- macOS or Linux for the built-in local resource collector.
- Optional: passwordless SSH access for remote GPU probes.

The registry helper and dashboard use only the Python standard library.

## Install as a Skill

Clone the repository, then copy the Skill folder into your Codex home:

```bash
git clone https://github.com/YOUR_ACCOUNT/agent-control-room.git
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R agent-control-room "${CODEX_HOME:-$HOME/.codex}/skills/"
```

Start a new Codex conversation and invoke it explicitly:

```text
$agent-control-room Manage these tasks from this one control thread. Isolate writable workspaces, prevent duplicate exclusive-resource use, and report only actionable changes.
```

## Start the dashboard

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/agent-control-room/scripts/control_room_dashboard.py" serve
```

Open `http://127.0.0.1:8765`.

The dashboard is read-only and binds to loopback by default. It needs access to `$CODEX_HOME` so it can start Codex app-server and query the local thread store. If an agent sandbox blocks app-server startup, run the dashboard from a normal user terminal.

Print a headless snapshot instead:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/agent-control-room/scripts/control_room_dashboard.py" snapshot --format compact
```

## Registry

The default registry is `$CODEX_HOME/agent-control-room/state.json`:

```bash
python3 scripts/agent_os_state.py add \
  --id build-docs \
  --title "Build documentation" \
  --goal "Publish reviewed documentation" \
  --status running \
  --thread-id THREAD_ID \
  --workspace /path/to/project \
  --exclusive-resource host:local/port:8080 \
  --next-action "Run the documentation checks"

python3 scripts/agent_os_state.py check
python3 scripts/agent_os_state.py summary
```

The registry stores coordination metadata, not conversation bodies or credentials.

## Resource probes

Local CPU, memory, and disk metrics are enabled by default. Add `$CODEX_HOME/agent-control-room/resources.json` to probe a GPU host:

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

The probe uses SSH batch mode, a short connection timeout, and no credential storage. An unreachable host is displayed as unavailable rather than idle.

## Privacy and security

Do not commit runtime state, resource configuration with private hostnames, Codex thread IDs, account details, SSH configuration, credentials, or screenshots of the dashboard. The included `.gitignore` excludes the default runtime files, but the agent operator remains responsible for reviewing changes before publication.

See [SECURITY.md](SECURITY.md) for the threat model and deployment guidance.

## Architecture

```text
user
  -> control thread
       -> task thread A
       -> task thread B
       -> task thread C
       -> JSON task registry
       -> conflict checks
       -> heartbeat sweep
       -> Codex app-server dashboard
```

Codex app-server provides thread, turn, rate-limit, and usage reads. The dashboard merges that live data with the registry. Resource probes report observed host health separately from registry intent.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/agent_os_state.py scripts/control_room_dashboard.py
```

## License

MIT
