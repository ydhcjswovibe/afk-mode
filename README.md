# AFK Mode

AFK Mode is a Codex plugin for bounded unattended repository work.

## Repository

- Source repo path: `/home/ydhcjswo/projects/afk-mode`
- Deployment plugin path: `/home/ydhcjswo/plugins/afk-mode`
- Canonical GitHub repository: `git@github.com:ydhcjswovibe/afk-mode.git`
- GitHub web URL: `https://github.com/ydhcjswovibe/afk-mode`
- Local `origin` may use an SSH host alias such as `github-afk-mode` from `~/.ssh/config`

## Verification

- Repo-owned verification command: `cd scripts && python3 -m unittest test_afk_mode.py`

## Publish

- Develop from `/home/ydhcjswo/projects/afk-mode`
- Sync the deployment copy with `scripts/publish_plugin.sh`
- Do not edit `/home/ydhcjswo/plugins/afk-mode` directly

## Notes

- Keep private SSH keys outside this repository.
- Checked-in repo policy lives at `.codex/plugin-profile.yaml` or `.codex-plugin.yaml`.
- Core behavior and maintenance scope are described in `SPEC.md`.
