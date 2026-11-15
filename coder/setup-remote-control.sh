#!/bin/sh
# Turn on the coder's Remote Control, once, from a terminal on the server:
#   docker compose exec -it coder sh /app/coder/setup-remote-control.sh
# 1. Logs Claude Code in to the user's Claude account (open the link, sign in, paste the
#    code). The login stays on the /work volume; Claude Code warns days before it expires,
#    and this script logs in again.
# 2. Starts Remote Control once in this terminal, to answer its first questions (enable,
#    spawn mode). Once it shows its link, stop it with Ctrl-C: the service takes over.
set -eu
unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
claude auth login
cd "${CODER_REPOS:-/work/repos}"
# Ctrl-C stops Remote Control, not this script (a trap with a command leaves the child the
# default handling).
trap 'echo' INT
claude remote-control --name "${CODER_RC_NAME:-Home agent code}" --spawn same-dir \
  --permission-mode default || true
trap - INT
touch "${CLAUDE_CONFIG_DIR:-/work/.claude}/remote-control-on"
echo "Remote Control is on: the coder keeps it running from now on."
