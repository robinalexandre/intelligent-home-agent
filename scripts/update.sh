#!/bin/sh
# Updates the agent where it runs: pulls the repository, then rebuilds the stack, leaving the
# coder container (compose/coder.yml, docs/jobs.md) alone unless what it is made of changed:
# its files (coder/, agents/) since the commit it was last built from, or its compose
# settings. Recreating the coder ends every Claude Code session running in it (the Remote
# Control ones included), so an update of something else must not touch it. Without the
# coder it is `git pull && docker compose up -d --build`.
#   scripts/update.sh                      pull, the stack, the coder only when it changed
#   UPDATE_CODER=always scripts/update.sh  the coder too, whatever changed
#   UPDATE_PULL=no scripts/update.sh       rebuild what is checked out, without pulling
# Which compose files make the stack is COMPOSE_FILE in .env, as for `docker compose`.
# UPDATE_COMPOSE names another compose command ("podman compose", "docker-compose").
# What the coder was last built from is kept in .coder-deployed (commit and compose hash).
set -eu
cd "$(dirname "$0")/.."
compose=${UPDATE_COMPOSE:-docker compose}
STATE=.coder-deployed

if [ "${UPDATE_PULL:-yes}" != no ] && [ "${UPDATE_PULLED:-}" != yes ]; then
  git pull --ff-only
  # The pull may have changed this very script: run the new one.
  UPDATE_PULLED=yes exec sh "$0" "$@"
fi

services=$($compose config --services)
head=$(git rev-parse HEAD 2>/dev/null || true)
if ! printf '%s\n' "$services" | grep -qx coder || [ -z "$head" ]; then
  # No coder in this stack (or no git history to compare with): nothing to keep running.
  $compose up -d --build
  exit 0
fi
hash=$($compose config --hash coder | awk '{print $2}')
last_commit=""
last_hash=""
if [ -f "$STATE" ]; then
  read -r last_commit last_hash < "$STATE" || true
fi
changed=yes
if [ "${UPDATE_CODER:-auto}" != always ] && [ -n "$last_commit" ] \
  && git cat-file -e "$last_commit^{commit}" 2>/dev/null \
  && git diff --quiet "$last_commit" "$head" -- coder agents \
  && [ "$hash" = "$last_hash" ]; then
  changed=no
fi
if [ "$changed" = yes ]; then
  echo "coder: changed since it was last built, rebuilt with the rest (its sessions end)"
  $compose up -d --build
  printf '%s %s\n' "$head" "$hash" > "$STATE"
else
  echo "coder: unchanged, left running (its sessions keep going)"
  # shellcheck disable=SC2086  # one service per word
  $compose up -d --build $(printf '%s\n' "$services" | grep -vx coder)
fi
