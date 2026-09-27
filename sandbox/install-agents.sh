#!/bin/bash
# Installs the four agent CLIs into the switchboard-home volume (docs/SANDBOX.md §3).
# Run it once as dev with the firewall off:
#   SWITCHBOARD_FIREWALL=0 docker compose up -d
#   docker compose exec -u dev box /opt/sandbox/install-agents.sh
# To upgrade, run it again with new pins. It logs in to nothing (SANDBOX.md §4).
set -euo pipefail
# Only inside the box: on the Mac it would pipe four installers into bash and
# write approval_policy = "never" / danger-full-access into your real ~/.codex.
if [ ! -f /opt/sandbox/.switchboard-box ]; then
  echo "install-agents.sh runs only inside the switchboard box (docs/SANDBOX.md §3)" >&2; exit 1; fi
if [ "$(id -u)" = 0 ] || [ "$HOME" != /home/dev ]; then
  echo "run this as dev (HOME=/home/dev): docker compose exec -u dev box $0" >&2; exit 1; fi
export PATH="$HOME/.local/bin:$PATH"   # where the installers put the CLIs (not on the image PATH)
# Sources (checked 2026-09-25):
# - Claude: code.claude.com/docs/en/setup; `bash -s <version>` pins the version;
#   DISABLE_AUTOUPDATER=1 (image env) turns its updater off.
# - Codex: chatgpt.com/codex/install.sh redirects to releases.openai.com/codex/install.sh,
#   which reads CODEX_RELEASE and CODEX_NON_INTERACTIVE; the daemon settings path and
#   format come from its source (codex-rs/app-server-daemon/src/settings.rs @ rust-v0.156.1).
# - Cursor: cursor.com/docs/cli/installation. It can't be pinned (the script hard-codes
#   its build) and it keeps auto-updating: there is no documented way to turn that off.
# - Devin: the versioned setup.sh URL was observed, not documented (the documented
#   cli.devin.ai/install.sh installs only the latest); `auto_update` is documented in
#   docs.devin.ai/cli/reference/configuration/config-file.
# The CLIs go into the switchboard-home volume, not the image: their installers write under
# $HOME, and Docker copies image files into a volume only while it is empty.
CLAUDE_VER=2.1.282 CODEX_VER=0.156.1 DEVIN_VER=3000.11.3
curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_VER"
curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 CODEX_RELEASE="$CODEX_VER" sh
curl -fsS https://cursor.com/install | bash      # can't pin: the script hard-codes its build
mkdir -p ~/.codex/app-server-daemon ~/.config/devin
test -e ~/.codex/config.toml || printf '%s\n' 'approval_policy = "never"' \
  'sandbox_mode = "danger-full-access"' 'check_for_update_on_startup = false' \
  'cli_auth_credentials_store = "file"' > ~/.codex/config.toml
test -e ~/.codex/app-server-daemon/settings.json ||   # the daemon's own updater (from source)
  echo '{"updater":{"autoUpdateEnabled":false}}' > ~/.codex/app-server-daemon/settings.json
codex features enable daemon_auto_start               # experimental, undocumented
d=$(mktemp -d)
curl -fsSLo "$d/devin.sh" "https://static.devin.ai/cli/$DEVIN_VER/setup.sh" && bash "$d/devin.sh"  # not piped: its closing `devin setup` needs the TTY
f="$HOME/.config/devin/config.json"; test -e "$f" || echo '{}' > "$f"
jq '.auto_update = false' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
