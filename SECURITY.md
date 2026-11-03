# Security and privacy

## Data sensitivity

The dashboard can display local paths, Codex thread titles, thread IDs, usage information, account token totals, resource names, and local host metrics. Treat the dashboard as private local data even though it is read-only.

The HTTP server binds to `127.0.0.1` by default and exposes no write endpoint. Do not bind it to a public interface or reverse-proxy it without adding authentication, authorization, transport security, and an explicit review of the data exposed.

## Repository hygiene

Never commit:

- `$CODEX_HOME/agent-control-room/state.json`
- `$CODEX_HOME/agent-control-room/resources.json` when it contains private hostnames
- Codex conversation exports or thread IDs
- credentials, access tokens, SSH keys, cookies, or account identifiers
- private project names, paths, screenshots, datasets, checkpoints, or logs

The included `.gitignore` excludes default runtime files and Python artifacts. It is not a guarantee that arbitrary local data is safe to publish. Review every commit and publish from a clean, purpose-built repository.

## App-server access

The dashboard starts `codex app-server` as the current user and can read the local Codex thread store and account usage available to that user. Run it only on a trusted local machine and protect `$CODEX_HOME` with normal operating-system file permissions.

## Remote probes

Remote GPU probes execute a fixed `nvidia-smi` query through SSH in batch mode. They do not read, store, or display SSH credentials. The target host, account, and metrics remain local dashboard data and should not be committed.

## Reporting

If you discover a security issue, avoid posting secrets or private data in a public issue. Contact the repository owner through the private security-reporting channel configured for the repository.
