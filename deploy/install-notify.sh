#!/usr/bin/env bash
# Turn on the gateway's physical-event log, at paths that do not depend on HOME.
#
# --- Why this is a script and not two lines in the README ---
#
# The gateway resolves notify.yml against its own HOME by default:
# $XDG_CONFIG_HOME/stackchan-mcp/notify.yml, else $HOME/.config/... And its
# HOME is whatever the unit's drop-in says, which is deployment state that
# lives on one machine and is written down nowhere.
#
# Guessing it cost two rounds: first an install command naming a
# `stackchan-gateway` user that does not exist (the gateway runs as the same
# user as everything else), then a file installed under a HOME that may not be
# the one the process has. Both were assumptions presented as instructions.
#
# So both paths are set EXPLICITLY instead, using the overrides the gateway
# already provides:
#
#   STACKCHAN_NOTIFY_CONFIG   where to read notify.yml from
#   STACKCHAN_EVENTS_PATH     where to append the JSONL event log
#
# The first is worth having for its own sake: when it points at a missing file
# the gateway logs "STACKCHAN_NOTIFY_CONFIG points to a non-existent file",
# where the HOME-relative default just silently finds nothing. A wrong path
# that says so beats a right path that might not be.
#
# --- What it does ---
#
#   /etc/cubie/notify.yml                    the config, root-owned, 0644
#   /var/lib/cubie/stackchan-events.jsonl    the log, owned by the gateway's user
#
# and appends the two variables to the gateway's env file if they are not
# already there. Idempotent: re-running reports what is already correct.
#
# Run it on office-server:
#   bash ~/cubie/deploy/install-notify.sh
#   sudo systemctl restart stackchan-gateway
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="${UNIT:-stackchan-gateway}"
ENV_FILE="${ENV_FILE:-/etc/stackchan-gateway.env}"
CONFIG_PATH="${CONFIG_PATH:-/etc/cubie/notify.yml}"
EVENTS_PATH="${EVENTS_PATH:-/var/lib/cubie/stackchan-events.jsonl}"

[ -f "$HERE/stackchan-notify.yml" ] || {
  echo "ERROR: $HERE/stackchan-notify.yml not found" >&2; exit 1; }
[ -f "$ENV_FILE" ] || {
  echo "ERROR: $ENV_FILE not found -- is the gateway installed?" >&2; exit 1; }

# Whoever the gateway runs as has to be able to WRITE the log, and the
# character stack has to READ it. Asked rather than assumed: the last two
# attempts here both failed on a guessed identity.
RUN_AS="$(systemctl show -p User --value "$UNIT" || true)"
# An empty User= means the unit runs as root; systemd reports it as blank
# rather than as "root".
[ -n "$RUN_AS" ] || RUN_AS=root
echo "$UNIT runs as: $RUN_AS"

# Check the account exists before handing it to install(1). `install -o` dies
# with a bare "invalid user 'x'", which is the exact error that sent this whole
# thing round twice -- once for a username I invented, and once here when a
# test environment had no such account. A named user that does not exist
# deserves a sentence, not two words.
if ! id -u "$RUN_AS" >/dev/null 2>&1; then
  echo "ERROR: $UNIT reports User=$RUN_AS, but no such account exists." >&2
  echo "       Check the unit: systemctl cat $UNIT | grep -i '^User='" >&2
  exit 1
fi

echo "installing $CONFIG_PATH"
sudo install -d -m 0755 "$(dirname "$CONFIG_PATH")"
sudo install -m 0644 "$HERE/stackchan-notify.yml" "$CONFIG_PATH"

EVENTS_DIR="$(dirname "$EVENTS_PATH")"
if [ -d "$EVENTS_DIR" ] && [ "$(stat -c '%U' "$EVENTS_DIR")" = "$RUN_AS" ]; then
  echo "  = $EVENTS_DIR already owned by $RUN_AS"
else
  echo "preparing $EVENTS_DIR for $RUN_AS to write"
  sudo install -d -m 0755 -o "$RUN_AS" "$EVENTS_DIR"
fi

# Append only what is missing, and never rewrite the file: it carries the
# gateway's token, and a script that rewrites a secrets file is a script that
# can lose one.
for pair in "STACKCHAN_NOTIFY_CONFIG=$CONFIG_PATH" "STACKCHAN_EVENTS_PATH=$EVENTS_PATH"; do
  key="${pair%%=*}"
  if sudo grep -qE "^${key}=" "$ENV_FILE"; then
    current="$(sudo sed -nE "s/^${key}=//p" "$ENV_FILE" | tr -d '"'"'")"
    if [ "$current" = "${pair#*=}" ]; then
      echo "  = $key already set correctly"
    else
      echo "  ! $key is set to something else: $current" >&2
      echo "    leaving it alone -- change it by hand if that is wrong" >&2
    fi
  else
    printf '%s\n' "$pair" | sudo tee -a "$ENV_FILE" >/dev/null
    echo "  + $pair"
  fi
done

# The character stack has to TAIL the same file the gateway WRITES. Its own
# default matches EVENTS_PATH above, but only while both stay in step -- so
# when they can be pinned they are, rather than relying on two defaults
# agreeing. A gateway writing events nobody tails looks exactly like a device
# that is not reporting, which is the failure this script exists to remove.
CHARACTER_ENV="${CHARACTER_ENV:-/etc/cubie-character.env}"
if [ -f "$CHARACTER_ENV" ]; then
  if sudo grep -qE "^STACKCHAN_EVENTS_PATH=" "$CHARACTER_ENV"; then
    echo "  = STACKCHAN_EVENTS_PATH already set in $(basename "$CHARACTER_ENV")"
  else
    printf 'STACKCHAN_EVENTS_PATH=%s\n' "$EVENTS_PATH" | sudo tee -a "$CHARACTER_ENV" >/dev/null
    echo "  + STACKCHAN_EVENTS_PATH in $(basename "$CHARACTER_ENV")"
  fi
  RESTART_CHARACTER=yes
else
  echo "  . $CHARACTER_ENV not present, so the character stack is not installed"
  echo "    yet -- its default already matches $EVENTS_PATH"
  RESTART_CHARACTER=no
fi

echo
echo "Done. Now:"
echo "  sudo systemctl restart $UNIT"
[ "$RESTART_CHARACTER" = yes ] && echo "  sudo systemctl restart cubie-character"
echo "then stroke his head and check:"
echo "  tail -3 $EVENTS_PATH"
