#!/bin/sh
# The remote browser's login page (docs/browser.md, Authelia), run on the machine that runs
# the agent, from anywhere in the repository:
#   scripts/browser-auth.sh user [name]   set the user's password (asked here, never echoed)
#   scripts/browser-auth.sh code          the latest one-time code, to register a passkey
#                                         or an authenticator app
set -eu
cd "$(dirname "$0")/.."
# The volume of compose/browser-login.yml, named after the Compose project.
volume="${COMPOSE_PROJECT_NAME:-intelligent-home-agent}_browser_auth"
image=$(awk '/image: authelia\/authelia:/ { print $2; exit }' compose/browser-login.yml)
helper=alpine:3.20

case "${1:-}" in
  user)
    name=${2:-me}
    echo "Password for $name (at least 12 characters), asked twice by Authelia itself:"
    # Authelia prompts on its own terminal, so the password never sits in a variable or
    # an argument; the prompt shows through tee, the hash is read from its last line.
    digest=$(docker run --rm -it "$image" authelia crypto hash generate argon2 |
      tee /dev/tty | tr -d '\r' | sed -n 's/^Digest: //p')
    [ -n "$digest" ] || { echo "Authelia gave no hash." >&2; exit 1; }
    printf 'users:\n  %s:\n    displayname: %s\n    email: %s@localhost.invalid\n    password: %s\n' \
      "$name" "$name" "$name" "'$digest'" |
      docker run --rm -i -v "$volume:/data" "$helper" sh -c 'cat > /data/users.yml && chmod 600 /data/users.yml'
    echo "Saved. Restart the login page: docker compose restart browser-auth"
    ;;
  code)
    docker run --rm -v "$volume:/data:ro" "$helper" sh -c \
      'tail -n 40 /data/notification.txt 2>/dev/null || echo "No code sent yet."'
    ;;
  *)
    echo "usage: $0 user [name] | code" >&2
    exit 2
    ;;
esac
