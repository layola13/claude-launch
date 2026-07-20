# claude-launch

Launcher for Claude Code CLI that routes its **Anthropic Messages API** traffic through a local translation proxy to a **standard OpenAI Chat Completions** endpoint.

This is not an OpenAI-style env wrapper. Claude Code speaks Anthropic routes like `/v1/messages`, so `claude-launch` translates both request and response formats, including streaming SSE and tool calls.

## Copyright / 免责声明

Copyright (c) 2026 claude-launch contributors

**仅供学习与研究使用；其他用途后果自负。**

This project is for learning and research only. Any other use is at your own risk.
See [COPYRIGHT](COPYRIGHT).

## What it does

1. Loads config from env / `.env`.
2. Starts a local HTTP proxy on a free port.
3. Sets `ANTHROPIC_BASE_URL` so Claude Code talks to the proxy.
4. Translates `POST /v1/messages` to `POST {base}/chat/completions`.
5. Converts OpenAI responses back into Anthropic message JSON or Anthropic SSE.

## Why claude-launch

`claude-launch` is intentionally small: no Docker, no LiteLLM, no FastAPI, no uv runtime, and no desktop app. It is a pure Python standard-library launcher/proxy that keeps the common path to three core settings:

- `CLAUDE_LAUNCH_BASE_URL`
- `CLAUDE_LAUNCH_MODEL`
- `CLAUDE_LAUNCH_API_KEY`

Run `./install.sh`, then use `claude-launch` as the command that starts Claude Code through the local translator. It only launches Claude Code and routes API traffic; it does not automate repository operations such as `git push`.

### Compared with proxy-style alternatives

| Project | Config complexity | Dependencies | Install / start | Lightweight | Best for |
|---------|-------------------|--------------|-----------------|-------------|----------|
| `claude-launch` | Almost zero: mainly 3 env vars | Almost zero: Python standard library only | `./install.sh` -> `claude-launch` | ★★★★★ | Users who want the simplest local CLI proxy |
| `1rgs/claude-code-proxy` | Medium: `.env` plus LiteLLM config | LiteLLM plus uv / Docker | `uv run` / Docker | ★★★ | Users who need multi-model routing or Gemini support |
| LiteLLM Proxy | Medium-high: many gateway options | LiteLLM stack | pip / uv plus uvicorn | ★★ | Teams building a full AI gateway |
| Other small proxies | Medium | Often LiteLLM / FastAPI based | Usually a separate server process | ★★★ | Varies by proxy |

`claude-launch` gives up full gateway features in exchange for the shortest path from Claude Code to an OpenAI-compatible `/chat/completions` backend.

### Compared with cc-switch

| Dimension | `claude-launch` | cc-switch |
|-----------|-----------------|-----------|
| Type | Lightweight local proxy + CLI launcher | Cross-platform desktop GUI manager built with Tauri |
| Core job | Translate Anthropic Messages API traffic to OpenAI-compatible Chat Completions and launch Claude Code | Manage provider configs, keys, MCP, and presets across tools like Claude Code, Codex, OpenCode, and Gemini CLI |
| Config complexity | Almost zero: 3 core env vars | Low-medium: GUI switching and many presets, but requires installing a desktop app |
| Dependencies | Almost zero: Python standard library only | Tauri / Rust desktop application stack |
| Lightweight | ★★★★★ | ★★ |
| Best for | Pure terminal users who want Claude Code plus a custom OpenAI-compatible backend | Users who want one visual app to manage multiple AI CLIs and providers |
| Main advantage | Minimal terminal flow, no separate UI, no heavy proxy framework | Rich one-stop provider management, presets, hot switching, and visualization |

cc-switch is a configuration manager; `claude-launch` is a protocol translation layer plus launcher. If you want a GUI to manage many AI tools, cc-switch is a better fit. If you want the smallest pure-terminal path for Claude Code to talk to a custom OpenAI-compatible backend, `claude-launch` is the focused option.

## Quick start

```bash
./install.sh


$EDITOR ~/.config/claude-launch/.env
export PATH="$HOME/.local/bin:$PATH"

claude-launch
claude-launch -p "hello"
claude-launch -p --verbose --output-format stream-json "hello"
```
`./install.sh` also auto-installs the upstream CLI when it is missing (use `--skip-cli` to opt out).

### Project-local `.env`

```bash
cp .env.example .env
claude-launch
```

When both project `.env` and `~/.config/claude-launch/.env` exist, project keys win.  
Shell `export` still wins over any `.env` file.

## Configuration

### Required

| Variable | Meaning |
|----------|---------|
| `CLAUDE_LAUNCH_BASE_URL` | OpenAI-compatible base, e.g. `https://gateway.example/v1` |
| `CLAUDE_LAUNCH_MODEL` | Default real upstream model string sent to `/chat/completions` |
| `CLAUDE_LAUNCH_API_KEY` | Bearer token for the upstream gateway |

### Optional

| Variable | Meaning |
|----------|---------|
| `CLAUDE_LAUNCH_CLI_MODEL` | Injected as `claude --model` when you do not pass `--model`; default `claude-sonnet-5` |
| `CLAUDE_LAUNCH_MODEL_MAP_JSON` | Maps Claude request model names to upstream model names; defaults to `{"*":"CLAUDE_LAUNCH_MODEL"}` |
| `CLAUDE_LAUNCH_CLI_EFFORT` | Injected as `claude --effort` when you do not pass `--effort` |
| `CLAUDE_LAUNCH_SETTING_SOURCES` | Injected as `claude --setting-sources`; default `user` |
| `CLAUDE_LAUNCH_MODEL_DISPLAY_NAME` | Displayed by the local `/v1/models` route |
| `CLAUDE_LAUNCH_USER_AGENT` | Upstream HTTP `User-Agent`; default `curl/8.5.0` |
| `CLAUDE_LAUNCH_REASONING_EFFORT` | Fallback upstream `reasoning_effort` if the Claude request does not include one |
| `CLAUDE_LAUNCH_LOCAL_API_KEY` | Dummy API key used between Claude Code and the local proxy |
| `CLAUDE_BIN` | Path to Claude Code CLI; default `claude` |
| `CLAUDE_LAUNCH_ENV` | Force a specific env file path |
| `CLAUDE_LAUNCH_VERBOSE=1` | Log proxy details |
| `CLAUDE_LAUNCH_PORT` | Fixed proxy port |
| `CLAUDE_LAUNCH_DEBUG_DIR` | Directory for request/response debug dumps |
| `CLAUDE_LAUNCH_CAPTURE_LOG=1` | Append redacted JSONL capture logs |
| `CLAUDE_LAUNCH_CAPTURE_LOG_FILE` | Capture log path; default `CLAUDE_LAUNCH_DEBUG_DIR/capture.jsonl` |

### `.env` load priority

1. `CLAUDE_LAUNCH_ENV`
2. `./.env` or `./.claude-launch.env`
3. Parent directories up to 6 levels
4. Repo-local `.env`
5. `~/.config/claude-launch/.env`
6. `~/.claude-launch.env`

## Effort mapping

Claude Code sends effort in Anthropic-style `output_config.effort`. `claude-launch` maps that to upstream `reasoning_effort` like this:

| Claude | Upstream |
|--------|----------|
| `low` | `low` |
| `medium` | `medium` |
| `high` | `high` |
| `xhigh` | `high` |
| `max` | `high` |

Ways to set it:

```bash
claude-launch --effort high
CLAUDE_LAUNCH_CLI_EFFORT=high claude-launch
```

If the upstream rejects `reasoning_effort`, `claude-launch` retries once without that field.

## Model mapping

Claude Code still sends Anthropic model names such as `claude-sonnet-5`, `opus`, or a full Claude model id. `claude-launch` maps those names before calling the upstream OpenAI-compatible API.

The default map is:

```env
CLAUDE_LAUNCH_MODEL_MAP_JSON={"*":"${CLAUDE_LAUNCH_MODEL}"}
```

For explicit aliases:

```env
CLAUDE_LAUNCH_MODEL=gpt-5.5
CLAUDE_LAUNCH_CLI_MODEL=claude-sonnet-5
CLAUDE_LAUNCH_MODEL_MAP_JSON={"*":"gpt-5.5","opus":"gpt-5.5","sonnet":"gpt-5.5","claude-opus-*":"gpt-5.5","claude-sonnet-*":"gpt-5.5"}
```

`CLAUDE_LAUNCH_VERBOSE=1` prints the active model map at startup.

## Settings isolation

By default `claude-launch` starts Claude Code with:

```bash
--setting-sources user
```

This is intentional. Project settings can contain Anthropic auth env vars such as `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_BASE_URL`; if Claude Code loads those after `claude-launch` injects the local proxy env, the UI reports both auth methods and may bypass the proxy.

To restore Claude Code's default settings loading:

```env
CLAUDE_LAUNCH_SETTING_SOURCES=default
```

To explicitly include project settings:

```env
CLAUDE_LAUNCH_SETTING_SOURCES=user,project,local
```

## Streaming

`claude-launch` supports both:

- non-stream Anthropic message responses
- Anthropic SSE output converted from upstream OpenAI SSE

This covers Claude Code `-p` and `--output-format stream-json` flows.

## Usage

```bash
claude-launch
claude-launch -c
claude-launch -p "reply with ok"
claude-launch exec "reply with ok"
claude-launch -p --verbose --output-format stream-json "reply with ok"
claude-launch --model claude-opus-4.1 --effort max
CLAUDE_LAUNCH_VERBOSE=1 claude-launch
```

Direct run without install:

```bash
python3 main.py -p "hi"
```

## Debug

With `CLAUDE_LAUNCH_VERBOSE=1`, stderr shows proxy config and loaded env files.

Optional debug files under `CLAUDE_LAUNCH_DEBUG_DIR`:

- `incoming_request.json`
- `outgoing_openai_request.json`
- `failed_request.json`
- `<request_id>-incoming_request.json`
- `<request_id>-outgoing_openai_request-attempt-1.json`
- `<request_id>-failed_request.json`

For model-routing issues, enable:

```env
CLAUDE_LAUNCH_DEBUG_DIR=/root/projects/claude-launch/logs
CLAUDE_LAUNCH_CAPTURE_LOG=1
CLAUDE_LAUNCH_CAPTURE_LOG_FILE=/root/projects/claude-launch/logs/capture.jsonl
```

Then inspect:

```bash
tail -n 50 /root/projects/claude-launch/logs/capture.jsonl
```

## Notes

- Claude Code does not speak OpenAI wire format directly. A plain `OPENAI_BASE_URL` style wrapper is not enough here.
- The local proxy also exposes `HEAD /`, `GET /v1/models`, and `POST /v1/messages/count_tokens` for Claude gateway compatibility.
