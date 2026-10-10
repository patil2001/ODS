Title: Windows local install exposes cloud routes through the LiteLLM switchboard until the first model switch

### Description

On a fresh **Windows local** install, LiteLLM serves the `cloud`, `minimax` and `minimax-fast` routes. A client holding the gateway key that picks one of them has its chat sent to Anthropic or MiniMax whenever `ANTHROPIC_API_KEY` or `MINIMAX_API_KEY` is set in `.env`, even though the install is local mode.

Linux and macOS are not affected, because their installers render the switchboard for the actual mode.

### Root cause

- `scripts/render-runtime-configs.py` (around `:595-616`) adds the `cloud`/`minimax`/`minimax-fast` routes **only when `ods_mode == "hybrid"`**.
- The checked-in `config/litellm/switchboard.yaml` is the hybrid render. It includes those routes, and `tests/test-cloud-model-ids.py::test_litellm_configs_route_cloud_default` deliberately keeps it that way.
- Linux (`installers/phases/06-directories.sh`, around `:1932`) and macOS (`installers/macos/install-macos.sh`, around `:2124`) run the renderer during install, which replaces the template with the mode-correct file.
- The Windows installer never calls the renderer. `installers/windows/phases/06-directories.ps1:91` only copies `config\litellm\switchboard.yaml`.
- `extensions/services/litellm/select-config.sh` selects `switchboard.yaml` for every non-cloud mode when the switchboard is enabled, which is the default.
- The host agent re-renders the switchboard only on model activation (`bin/ods-host-agent.py`, `_render_model_router_runtime_configs`, around `:17158`). Until the owner switches models, the hybrid routes stay live.

### Steps to reproduce

1. Run a Windows local install with the default switchboard enabled.
2. Before any model switch, call `GET http://localhost:4000/v1/models` with the LiteLLM key.

### Expected behavior

Only local aliases are listed (`ods/current`, `local`, `default`, `*`), matching `render-runtime-configs.py --surface litellm-switchboard --ods-mode local`.

### Actual behavior

`cloud`, `minimax` and `minimax-fast` are also listed and routable.

### Suggested fix

Have the Windows install render `litellm-switchboard` for the configured mode, the same way the Linux and macOS installers do. Do this at the end of phase 06, or through the host agent's existing render path. Keep the checked-in template unchanged, because the existing contract expects it to be the hybrid render.

The comparison is verifiable without Windows: diff the checked-in file against `render-runtime-configs.py --surface litellm-switchboard --switchboard-mode enabled`. The only differences are the three cloud routes plus blank lines.

**Operating System:** Windows 11 (logic bug; found by code reading)
**GPU:** any
