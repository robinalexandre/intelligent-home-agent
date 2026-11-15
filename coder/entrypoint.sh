#!/bin/sh
# git pushes and clones authenticate through gh, which reads GH_TOKEN; commits carry the
# user's identity. Then, in the background: Claude Code's Remote Control (once turned on)
# and, with CODER_SYNC_REPOS=true, the repositories kept in sync for it; in front, the
# jobs service.
set -eu
if [ -n "${GH_TOKEN:-}" ]; then
  gh auth setup-git >/dev/null 2>&1 || echo "gh auth setup-git failed" >&2
fi
git config --global user.name "${GIT_AUTHOR_NAME:-coder}"
git config --global user.email "${GIT_AUTHOR_EMAIL:-coder@invalid}"
git config --global init.defaultBranch main
python -m coder.setup || echo "coder setup failed" >&2
if [ -n "${GH_TOKEN:-}" ] && [ "${CODER_SYNC_REPOS:-false}" = "true" ]; then
  python -m coder.sync_repos --loop &
fi
python -m coder.remote_control &
exec uvicorn coder.app:app --host 0.0.0.0 --port 8100
