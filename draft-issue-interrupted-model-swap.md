# Bootstrap full-model swap interrupted by host reboot leaves llama-server `Created` and status stuck at "swapping"

## Description

On a fresh install, the background bootstrap upgrade (`ods/scripts/bootstrap-upgrade.sh`, log `~/ods/logs/model-upgrade.log`) was interrupted mid-swap when the host shut down (WSL terminated the distro). Nothing reconciled the half-finished swap after reboot, so the stack came back without an LLM.

Observed (Ubuntu 24.04 on WSL2, GTX 1650, current `main`, `install-core.sh --non-interactive`, optional services off):

- The installer exited rc=0. The background upgrade then promoted the "full" model. Here the full model was the same GGUF as the bootstrap model (`Qwen3.5-2B-Q4_K_M.gguf`), but the script still ran the whole swap.
- Last log lines:
  ```
  05:04:10 Restarting llama-server container (backend: nvidia)...
  05:04:10 Promoting .env to full model (pre-compose full-model promotion)...
  05:04:39 Reopened model-router request admission.
  ```
- The host shut down during the swap window.
- After reboot:
  - `ods-llama-server` stayed in Docker state `Created`. Every other container came back through its restart policy.
  - `data/bootstrap-status.json` stayed `{"status":"swapping","percent":100}` permanently.
  - `logs/background-tasks.json` kept the task as `"status":"running"` with a dead pid.
  - `ods status` kept printing `Model Upgrade: ... (100% downloaded)` and did not show that llama-server had never started.
  - Running `docker start ods-llama-server` by hand fixed it.

### Root cause (upstream/main)

1. **The recreate can be cut off between create and start.** `ods/scripts/bootstrap-upgrade.sh:482` runs `docker compose up -d --force-recreate --no-deps llama-server` (called from `:1931`). Compose stops and removes the old container, creates a new one, then starts it. If the process or host dies after the create and before the start, the new container is left `Created`. Docker's restart policy only restarts containers that were running, so it never starts a `Created` container on daemon start. The rollback path (`restore_docker_llama_server_after_swap_failure`, `:738`) and the `EXIT` trap (`:565`) cannot run when the host is gone.
2. **Nothing reconciles at startup.**
   - `write_status "swapping"` (`:1788`) is only replaced by a later `write_status` from the same process. No startup path rewrites it.
   - `ods-cli` has `_ods_cli_bootstrap_active_status_settled` (`ods/ods-cli:274`). It only lets `start` and `restart` skip waiting (`:352`, `:376`). It never clears the file and never starts llama-server.
   - `cmd_status` (`ods/ods-cli:1503-1512`) prints `Model Upgrade: ... downloaded` for any `swapping` status without checking whether the upgrade process is alive.
   - The host agent only reads the file to gate route proof (`ods/bin/ods-host-agent.py:3062-3072`). In fact `swapping` is treated as allowing it. No reconciliation runs in `main()` startup.
   - The dashboard API also treats `swapping` as active (`dashboard-api/routers/models.py:148`, `helpers.py:1373`).
   - `installers/lib/background-tasks.sh` only works out liveness when it is queried (`bg_task_status`, `:100`). It never rewrites a stale `running` record.
3. **There is no same-file short-circuit.** `bootstrap_needed` (`ods/installers/lib/bootstrap-model.sh:37-58`) skips the bootstrap only for tier 0, or when the full GGUF is already on disk before the install. It does not check `GGUF_FILE == BOOTSTRAP_GGUF_FILE`. As a result, `11-services.sh:900` launches the upgrade, and `bootstrap-upgrade.sh` goes through the drain, the snapshot, the `.env` promotion and a forced recreate (`:1788-1931`) for a model that is already loaded. This opens a needless outage window.
4. **Status does not show `Created`.** `cmd_status` lists containers with `docker compose ps` without `-a` (`ods/ods-cli` around `:1408`), which hides `Created` containers. The HTTP health check only reports llama-server as unhealthy. Nothing says the container exists but was never started, and the stale "Model Upgrade" line makes the outage look like an upgrade still in progress.

## Steps to Reproduce

1. Install on a tier >= 1 NVIDIA host where the bootstrap upgrade runs (in our case the tier's model equaled the bootstrap GGUF).
2. While `model-upgrade.log` shows `Restarting llama-server container ...`, reboot the host or `kill -9` the upgrade process together with dockerd (or `wsl --shutdown`).
3. Boot again and let Docker start.
4. Run `docker ps -a --filter name=ods-llama-server`, `cat data/bootstrap-status.json` and `ods status`.

## Expected

- After reboot, llama-server is running, on either the promoted model or a rolled-back one.
- A stale `swapping` status whose process is dead is cleared or marked failed or interrupted.
- `ods status` reports llama-server as not started.
- No swap runs when the full model is the same file as the bootstrap model.

## Actual

- `ods-llama-server` stays `Created` indefinitely, and the stack has no LLM.
- `bootstrap-status.json` stays at `swapping` / 100% forever.
- `background-tasks.json` still shows `running` for a dead pid.
- `ods status` shows a perpetual "Model Upgrade ... 100% downloaded" line.

## Suggested fix

- Add a startup reconciliation, in the host agent `main()` or the `ods start` path. When `bootstrap-status.json` is in an active state and no `bootstrap-upgrade.sh` process is alive (the existing `_ods_cli_bootstrap_upgrade_process_running` logic):
  - If `ods-llama-server` is `Created` or `exited`, run `docker start` on it, or recreate it from the current `.env`.
  - If `.env` and the running `--model` match the full model, mark the status `complete`. Otherwise restore the snapshot and mark it `failed` with an "interrupted" message.
  - Mark the background task record `failed` / `interrupted`.
- Short-circuit `bootstrap_needed` (or the top of `bootstrap-upgrade.sh`) when `GGUF_FILE == BOOTSTRAP_GGUF_FILE`. Write `complete` and skip the drain and recreate.
- In `ods status`, flag a llama-server container whose state is not `running` (for example `docker compose ps -a`). Suppress the "Model Upgrade" line when the upgrade process is not running.

## Environment

- OS: Ubuntu 24.04 on WSL2 (Windows 11)
- GPU: NVIDIA GTX 1650
- ODS: current `main` (upstream/main at time of report)
- Install: `install-core.sh --non-interactive`, optional services off
- Interruption: the host shut down and WSL terminated the distro during the swap window. The same thing happens with any host reboot or kill during that window.
