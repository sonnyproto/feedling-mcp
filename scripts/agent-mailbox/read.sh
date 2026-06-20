#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  scripts/agent-mailbox/read.sh <agent> [message_id|latest|--all|--list]
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
selector="${2:---list}"

root="$(repo_root)"
mailbox="${AGENT_MAILBOX_DIR:-$root/.agents/mailbox}"
inbox="$mailbox/inbox/$agent"

if [ ! -d "$inbox" ]; then
  echo "no inbox for $agent"
  exit 0
fi

latest_file() {
  find "$inbox" -maxdepth 1 -type f -name '*.md' | sort | tail -n 1
}

case "$selector" in
  --list)
    find "$inbox" -maxdepth 1 -type f -name '*.md' -print | sort | sed 's#^.*/##; s#\.md$##'
    ;;
  --all)
    found=0
    for file in $(find "$inbox" -maxdepth 1 -type f -name '*.md' -print | sort); do
      found=1
      printf '\n===== %s =====\n' "$(basename "$file" .md)"
      cat "$file"
    done
    [ "$found" -eq 1 ] || echo "no messages for $agent"
    ;;
  latest)
    file="$(latest_file)"
    if [ -z "$file" ]; then
      echo "no messages for $agent"
      exit 0
    fi
    cat "$file"
    ;;
  *)
    id="$(clean_token "$selector")" || usage
    file="$inbox/${id%.md}.md"
    if [ ! -f "$file" ]; then
      echo "message not found: $id" >&2
      exit 1
    fi
    cat "$file"
    ;;
esac
