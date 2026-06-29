#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  scripts/agent-mailbox/ack.sh <agent> [message_id|latest]
USAGE
  exit 2
}

repo_root() {
  git rev-parse --show-toplevel 2>/dev/null || pwd
}

clean_token() {
  case "$1" in
    ""|*[!A-Za-z0-9_.:-]*) return 1 ;;
    *) printf '%s' "$1" ;;
  esac
}

[ "$#" -ge 1 ] || usage
agent="$(clean_token "$1")" || usage
selector="${2:-latest}"

root="$(repo_root)"
mailbox="${AGENT_MAILBOX_DIR:-$root/.agents/mailbox}"
inbox="$mailbox/inbox/$agent"
archive="$mailbox/archive/$agent"
mkdir -p "$archive"

if [ "$selector" = "latest" ]; then
  file="$(find "$inbox" -maxdepth 1 -type f -name '*.md' 2>/dev/null | sort | tail -n 1)"
else
  id="$(clean_token "$selector")" || usage
  file="$inbox/${id%.md}.md"
fi

if [ -z "${file:-}" ] || [ ! -f "$file" ]; then
  echo "message not found" >&2
  exit 1
fi

dest="$archive/$(basename "$file")"
mv "$file" "$dest"
echo "acked $(basename "$dest" .md)"
