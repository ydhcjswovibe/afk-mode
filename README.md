# AFK Mode

AFK Mode is a Codex plugin for bounded unattended repository work.

## Repository

- Canonical GitHub repository: `git@github.com:ydhcjswovibe/afk-mode.git`
- GitHub web URL: `https://github.com/ydhcjswovibe/afk-mode`
- Default deployment path: `${AFK_MODE_PLUGIN_DIR:-$HOME/plugins/afk-mode}`
- Portable wrapper command: `${AFK_MODE_CMD:-${AFK_MODE_PLUGIN_DIR:-$HOME/plugins/afk-mode}/afk-mode}`

## Public Usage

- The public UX should be the explicit skill invocation: `$afk-mode`
- Prefer `$afk-mode <budget>` such as `$afk-mode 4h`
- `begin-run`, `advance-run`, `open-slice`, and related helper commands are internal runtime details, not the normal user-facing interface

## Verification

- Repo-owned verification command: `cd scripts && python3 -m unittest test_afk_mode.py`

## Publish

- Develop from this repository
- Sync the deployment copy with `./scripts/publish_plugin.sh`
- Override the deploy location with `AFK_MODE_PLUGIN_DIR=/some/path ./scripts/publish_plugin.sh`
- Do not edit the deployed copy directly

## Notes

- Keep private SSH keys outside this repository.
- Checked-in repo policy lives at `.codex/plugin-profile.yaml` or `.codex-plugin.yaml`.
- Use `${AFK_MODE_CMD:-${AFK_MODE_PLUGIN_DIR:-$HOME/plugins/afk-mode}/afk-mode} repair-registry --reset-corrupt` if the active-run registry becomes unreadable.
- Core behavior and maintenance scope are described in `SPEC.md`.
