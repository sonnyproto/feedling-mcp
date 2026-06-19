#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  scripts/agent-mailbox/setup.sh [--codex-pane <target>] [--claude-pane <target>] [--self codex|claude]

Examples:
  tmux list-panes -a -F '#S:#I.#P #{pane_current_command}'
  scripts/agent-mailbox/setup.sh --codex-pane dev:0.1 --claude-pane dev:0.2
  scripts/agent-mailbox/setup.sh --self codex
USAGE
  exit 2
}

repo_root() {
  git rev-parse --show-toplevel 2>/dev/null || pwd
}

shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

root="$(repo_root)"
mailbox="${AGENT_MAILBOX_DIR:-$root/.agents/mailbox}"
mkdir -p "$mailbox"
config="$mailbox/config.env"

codex_pane=""
claude_pane=""
if [ -f "$config" ]; then
  # shellcheck disable=SC1090
  . "$config"
  codex_pane="${CODEX_PANE:-}"
  claude_pane="${CLAUDE_PANE:-}"
fi

while [ "$#" -gt 0 ]; do
  case "$1" in
    --codex-pane) [ "$#" -ge 2 ] || usage; codex_pane="$2"; shift 2 ;;
    --claude-pane) [ "$#" -ge 2 ] || usage; claude_pane="$2"; shift 2 ;;
    --self)
      [ "$#" -ge 2 ] || usage
      command -v tmux >/dev/null 2>&1 || { echo "tmux not found" >&2; exit 1; }
      current="$(tmux display-message -p '#S:#I.#P')"
      case "$2" in
        codex) codex_pane="$current" ;;
        claude) claude_pane="$current" ;;
        *) usage ;;
      esac
      shift 2
      ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

{
  printf 'CODEX_PANE=%s\n' "$(shell_quote "$codex_pane")"
  printf 'CLAUDE_PANE=%s\n' "$(shell_quote "$claude_pane")"
} > "$config"

mkdir -p "$mailbox/messages" "$mailbox/inbox/codex" "$mailbox/inbox/claude" \
  "$mailbox/outbox/codex" "$mailbox/outbox/claude" "$mailbox/archive/codex" "$mailbox/archive/claude"

echo "wrote $config"
echo "CODEX_PANE=${codex_pane:-<unset>}"
echo "CLAUDE_PANE=${claude_pane:-<unset>}"
