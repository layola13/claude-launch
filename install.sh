#!/usr/bin/env bash
# Copyright (c) 2026 claude-launch contributors
# 仅供学习与研究使用；其他用途后果自负。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${CLAUDE_LAUNCH_BIN_DIR:-$HOME/.local/bin}"
XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
CONFIG_DIR="${CLAUDE_LAUNCH_CONFIG_DIR:-$XDG_CONFIG_HOME/claude-launch}"
USER_ENV="$CONFIG_DIR/.env"
EXAMPLE="$ROOT/.env.example"
WRAPPER="$BIN_DIR/claude-launch"

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

  Install claude-launch into ~/.local/bin and optionally create a user .env.

Options:
  --bin-dir DIR     Install wrapper here (default: ~/.local/bin)
  --config-dir DIR  Config directory (default: ~/.config/claude-launch)
  --force-env       Overwrite existing user .env from .env.example
  --no-env          Skip creating user .env
  --skip-cli        Do not auto-install the upstream CLI if missing
  --link            Symlink package claude-launch instead of a small wrapper
  -h, --help        Show this help

After install:
  1. Edit ~/.config/claude-launch/.env  (or project ./.env)
  2. Ensure ~/.local/bin is on PATH
  3. Run: claude-launch
EOF
}

FORCE_ENV=0
SKIP_CLI=0
NO_ENV=0
USE_LINK=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bin-dir) BIN_DIR="$2"; shift 2 ;;
    --config-dir) CONFIG_DIR="$2"; USER_ENV="$CONFIG_DIR/.env"; shift 2 ;;
    --force-env) FORCE_ENV=1; shift ;;
    --no-env) NO_ENV=1; shift ;;
    --skip-cli) SKIP_CLI=1; shift ;;
 --link) USE_LINK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done



WRAPPER="$BIN_DIR/claude-launch"
USER_ENV="$CONFIG_DIR/.env"

if [[ ! -f "$ROOT/main.py" ]]; then
  echo "error: main.py not found in $ROOT" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required" >&2
  exit 1
fi

mkdir -p "$BIN_DIR" "$CONFIG_DIR"

# --- ensure required CLI is installed (auto-install when missing) ---
ensure_path_bin() {
  local dir="$1"
  [[ -n "${dir:-}" ]] || return 0
  case ":$PATH:" in
    *":$dir:"*) ;;
    *) export PATH="$dir:$PATH" ;;
  esac
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

run_npm_global() {
  local pkg="$1"
  if ! have_cmd npm; then
    echo "error: npm is required to install $pkg. Install Node.js/npm first:" >&2
    echo "  https://nodejs.org/  or  https://docs.npmjs.com/downloading-and-installing-node-js-and-npm" >&2
    return 1
  fi
  echo "installing CLI via npm: $pkg"
  if [[ "$(id -u)" -ne 0 ]]; then
    npm install -g "$pkg" --prefix "${HOME}/.local" || npm install -g "$pkg"
  else
    npm install -g "$pkg" || {
      echo "retry npm install with --prefix ${HOME}/.local"
      npm install -g "$pkg" --prefix "${HOME}/.local"
    }
  fi
  local npm_bin
  npm_bin="$(npm prefix -g 2>/dev/null)/bin"
  [[ -d "$npm_bin" ]] && ensure_path_bin "$npm_bin"
  ensure_path_bin "${HOME}/.local/bin"
  hash -r 2>/dev/null || true
}

run_curl_bash() {
  local url="$1"
  local label="$2"
  if ! have_cmd curl && ! have_cmd wget; then
    echo "error: curl or wget required to install $label" >&2
    return 1
  fi
  echo "installing $label via $url"
  if have_cmd curl; then
    curl -fsSL "$url" | bash
  else
    wget -qO- "$url" | bash
  fi
  ensure_path_bin "${HOME}/.local/bin"
  ensure_path_bin "${HOME}/.grok/bin"
  hash -r 2>/dev/null || true
}

ensure_required_cli() {
  if [[ "${SKIP_CLI:-0}" -eq 1 ]]; then
    echo "skip CLI auto-install (--skip-cli)"
    return 0
  fi
  ensure_path_bin "${HOME}/.local/bin"
  ensure_path_bin "${BIN_DIR:-${HOME}/.local/bin}"
  if have_cmd "claude"; then
    echo "CLI present: claude -> $(command -v claude 2>/dev/null || command -v claude)"
    return 0
  fi
  echo
  echo "missing CLI: claude (Claude Code)"
  echo "docs: https://docs.anthropic.com/en/docs/claude-code/setup"
  echo "attempting automatic install..."

  # Official path: native installer (strongly recommended). npm package is deprecated.
  if ! have_cmd curl && ! have_cmd wget; then
    echo "error: curl or wget required for official Claude Code install:" >&2
    echo "  curl -fsSL https://claude.ai/install.sh | bash" >&2
    echo "docs: https://docs.anthropic.com/en/docs/claude-code/setup" >&2
    return 1
  fi
  if ! run_curl_bash "https://claude.ai/install.sh" "Claude Code (native)"; then
    echo "error: official Claude Code installer failed." >&2
    echo "  curl -fsSL https://claude.ai/install.sh | bash" >&2
    echo "docs: https://docs.anthropic.com/en/docs/claude-code/setup" >&2
    return 1
  fi
  # Native installer may place binary under ~/.local/bin or Claude's own bin dir
  ensure_path_bin "${HOME}/.local/bin"
  ensure_path_bin "${HOME}/.claude/bin"
  hash -r 2>/dev/null || true
  if ! have_cmd claude; then
    echo "warning: native install finished but 'claude' not on PATH yet." >&2
    echo "trying deprecated npm fallback (@anthropic-ai/claude-code) only as last resort..." >&2
    if ! run_npm_global "@anthropic-ai/claude-code"; then
      echo "error: 'claude' still not on PATH after official install." >&2
      echo "  curl -fsSL https://claude.ai/install.sh | bash" >&2
      echo "docs: https://docs.anthropic.com/en/docs/claude-code/setup" >&2
      return 1
    fi
  fi
  if ! have_cmd claude; then
    echo "error: 'claude' still not on PATH. See https://docs.anthropic.com/en/docs/claude-code/setup" >&2
    return 1
  fi
  echo "ok: $(command -v claude)"

}


chmod +x "$ROOT/main.py"
if [[ -f "$ROOT/claude-launch" ]]; then
  chmod +x "$ROOT/claude-launch"
fi

if [[ "$USE_LINK" -eq 1 ]]; then
  ln -sfn "$ROOT/claude-launch" "$WRAPPER"
  echo "linked $WRAPPER -> $ROOT/claude-launch"
else
  cat >"$WRAPPER" <<EOF
#!/usr/bin/env bash
# Generated by claude-launch install.sh — do not put secrets here.
exec python3 "$ROOT/main.py" "\$@"
EOF
  chmod +x "$WRAPPER"
  echo "installed $WRAPPER"
fi

if [[ "$NO_ENV" -eq 0 ]]; then
  if [[ -f "$USER_ENV" && "$FORCE_ENV" -eq 0 ]]; then
    echo "keep existing config: $USER_ENV"
  else
    if [[ ! -f "$EXAMPLE" ]]; then
      echo "warning: missing $EXAMPLE — writing minimal template" >&2
      cat >"$USER_ENV" <<'EOT'
CLAUDE_LAUNCH_BASE_URL=
CLAUDE_LAUNCH_MODEL=
CLAUDE_LAUNCH_API_KEY=
# CLAUDE_LAUNCH_CLI_MODEL=claude-sonnet-5
# CLAUDE_LAUNCH_CLI_EFFORT=high
EOT
    else
      cp -f "$EXAMPLE" "$USER_ENV"
    fi
    chmod 600 "$USER_ENV" 2>/dev/null || true
    echo "wrote $USER_ENV  (edit this file — fill in BASE_URL / MODEL / API_KEY)"
  fi
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo
    echo "note: $BIN_DIR is not on PATH. Add to your shell rc, e.g.:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac



ensure_required_cli

echo
echo "Done."
echo "  1) Edit config:  $USER_ENV"
echo "     (or put a project .env in the directory you run from)"
echo "  2) Run:          claude-launch -p \"hi\""
echo "  3) Streaming:    claude-launch -p --verbose --output-format stream-json \"hi\""
