#!/usr/bin/env python3
# Copyright (c) 2026 claude-launch contributors
# 仅供学习与研究使用；其他用途后果自负。
# For learning and research only; any other use is at your own risk.
"""claude-launch: local Claude Code shim that forwards to OpenAI chat completions.

Starts a local proxy, points Claude Code at it via ANTHROPIC_BASE_URL, and
translates:
  Anthropic Messages API  <->  OpenAI /v1/chat/completions

Configuration is loaded from environment variables and/or .env files.
No secrets or private endpoints are hard-coded.
"""

from __future__ import annotations

import json
import os
import fnmatch
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))

_REQUIRED_KEYS = (
    "CLAUDE_LAUNCH_BASE_URL",
    "CLAUDE_LAUNCH_MODEL",
    "CLAUDE_LAUNCH_API_KEY",
)

TARGET_BASE_URL: str = ""
TARGET_MODEL: str = ""
TARGET_API_KEY: str = ""
MODEL_MAP: dict[str, str] = {}
CLAUDE_CLI_MODEL: str = "claude-sonnet-5"
CLAUDE_CLI_EFFORT: str = ""
CLAUDE_SETTING_SOURCES: str = "user"
CLAUDE_BIN: str = "claude"
DEBUG_DIR: str = ""
CAPTURE_LOG_ENABLED: bool = False
CAPTURE_LOG_FILE: str = ""
UPSTREAM_USER_AGENT: str = "curl/8.5.0"
DEFAULT_REASONING_EFFORT: str = ""
LOCAL_PROXY_API_KEY: str = "claude-launch-local"
LOCAL_MODEL_DISPLAY_NAME: str = ""
_LOADED_ENV_FILES: list[str] = []

_VALID_CLAUDE_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
_VALID_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
_CLAUDE_TO_OPENAI_REASONING = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


class ClientDisconnectedError(Exception):
    pass


def _parse_dotenv(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if not key:
                    continue
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                out[key] = val
    except OSError:
        pass
    return out


def _parse_model_map(raw: str, *, fallback_model: str) -> dict[str, str]:
    text = (raw or "").strip()
    if not text:
        return {"*": fallback_model}

    parsed: Any
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = {}
        for item in text.split(","):
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                key, value = item.split(":", 1)
            elif "=" in item:
                key, value = item.split("=", 1)
            else:
                continue
            key = key.strip()
            value = value.strip()
            if key and value:
                parsed[key] = value

    if not isinstance(parsed, dict):
        return {"*": fallback_model}

    model_map: dict[str, str] = {}
    for key, value in parsed.items():
        normalized_key = str(key or "").strip()
        normalized_value = str(value or "").strip()
        if normalized_key and normalized_value:
            model_map[normalized_key] = normalized_value
    if "*" not in model_map:
        model_map["*"] = fallback_model
    return model_map


def _env_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _candidate_env_paths() -> list[str]:
    paths: list[str] = []
    explicit = os.environ.get("CLAUDE_LAUNCH_ENV")
    if explicit:
        paths.append(os.path.expanduser(explicit))

    cwd = os.getcwd()
    paths.append(os.path.join(cwd, ".env"))
    paths.append(os.path.join(cwd, ".claude-launch.env"))

    parent = os.path.dirname(cwd)
    for _ in range(6):
        if not parent or parent == os.path.dirname(parent):
            break
        paths.append(os.path.join(parent, ".env"))
        paths.append(os.path.join(parent, ".claude-launch.env"))
        parent = os.path.dirname(parent)

    paths.append(os.path.join(_HERE, ".env"))

    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    paths.append(os.path.join(xdg, "claude-launch", ".env"))
    paths.append(os.path.expanduser("~/.claude-launch.env"))

    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        ap = os.path.abspath(path)
        if ap in seen:
            continue
        seen.add(ap)
        unique.append(ap)
    return unique


def load_dotenv_files(*, override_existing: bool = False) -> list[str]:
    loaded: list[str] = []
    claimed: set[str] = set()
    if not override_existing:
        claimed |= set(os.environ.keys())

    for path in _candidate_env_paths():
        if not os.path.isfile(path):
            continue
        data = _parse_dotenv(path)
        if not data:
            continue
        any_applied = False
        for key, value in data.items():
            if key in claimed:
                continue
            os.environ[key] = value
            claimed.add(key)
            any_applied = True
        if any_applied or data:
            loaded.append(path)
    return loaded


def load_config() -> None:
    global TARGET_BASE_URL, TARGET_MODEL, TARGET_API_KEY, MODEL_MAP
    global CLAUDE_CLI_MODEL, CLAUDE_CLI_EFFORT, CLAUDE_SETTING_SOURCES
    global CLAUDE_BIN, DEBUG_DIR
    global CAPTURE_LOG_ENABLED, CAPTURE_LOG_FILE
    global UPSTREAM_USER_AGENT, DEFAULT_REASONING_EFFORT, LOCAL_PROXY_API_KEY
    global LOCAL_MODEL_DISPLAY_NAME, _LOADED_ENV_FILES

    _LOADED_ENV_FILES = load_dotenv_files()

    missing = [key for key in _REQUIRED_KEYS if not (os.environ.get(key) or "").strip()]
    if missing:
        example = os.path.join(_HERE, ".env.example")
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        user_env = os.path.join(xdg, "claude-launch", ".env")
        print("error: missing required configuration:", ", ".join(missing), file=sys.stderr)
        print(file=sys.stderr)
        print("Set them in a .env file or environment variables.", file=sys.stderr)
        print("  project:  ./.env   (copy from .env.example)", file=sys.stderr)
        print(f"  user:     {user_env}", file=sys.stderr)
        print(f"  template: {example}", file=sys.stderr)
        print(file=sys.stderr)
        print("Example:", file=sys.stderr)
        print("  cp .env.example .env   # then edit", file=sys.stderr)
        print("  # or:  ./install.sh", file=sys.stderr)
        if _LOADED_ENV_FILES:
            print("loaded env files (still missing keys):", file=sys.stderr)
            for path in _LOADED_ENV_FILES:
                print(f"  - {path}", file=sys.stderr)
        sys.exit(2)

    TARGET_BASE_URL = os.environ["CLAUDE_LAUNCH_BASE_URL"].strip().rstrip("/")
    TARGET_MODEL = os.environ["CLAUDE_LAUNCH_MODEL"].strip()
    TARGET_API_KEY = os.environ["CLAUDE_LAUNCH_API_KEY"].strip()
    MODEL_MAP = _parse_model_map(
        os.environ.get("CLAUDE_LAUNCH_MODEL_MAP_JSON")
        or os.environ.get("CLAUDE_LAUNCH_MODEL_MAP")
        or "",
        fallback_model=TARGET_MODEL,
    )
    CLAUDE_CLI_MODEL = (os.environ.get("CLAUDE_LAUNCH_CLI_MODEL") or "claude-sonnet-5").strip()
    CLAUDE_CLI_EFFORT = (os.environ.get("CLAUDE_LAUNCH_CLI_EFFORT") or "").strip().lower()
    CLAUDE_SETTING_SOURCES = (
        os.environ.get("CLAUDE_LAUNCH_SETTING_SOURCES") or "user"
    ).strip()
    CLAUDE_BIN = (os.environ.get("CLAUDE_BIN") or "claude").strip()
    UPSTREAM_USER_AGENT = (os.environ.get("CLAUDE_LAUNCH_USER_AGENT") or "curl/8.5.0").strip()
    DEFAULT_REASONING_EFFORT = (os.environ.get("CLAUDE_LAUNCH_REASONING_EFFORT") or "").strip().lower()
    LOCAL_PROXY_API_KEY = (
        os.environ.get("CLAUDE_LAUNCH_LOCAL_API_KEY") or "claude-launch-local"
    ).strip()
    LOCAL_MODEL_DISPLAY_NAME = (os.environ.get("CLAUDE_LAUNCH_MODEL_DISPLAY_NAME") or TARGET_MODEL).strip()
    DEBUG_DIR = (
        os.environ.get("CLAUDE_LAUNCH_DEBUG_DIR")
        or os.path.join(tempfile.gettempdir(), "claude-launch")
    ).strip()
    CAPTURE_LOG_ENABLED = _env_truthy(os.environ.get("CLAUDE_LAUNCH_CAPTURE_LOG"))
    CAPTURE_LOG_FILE = (
        os.environ.get("CLAUDE_LAUNCH_CAPTURE_LOG_FILE")
        or os.path.join(DEBUG_DIR, "capture.jsonl")
    ).strip()

    if CLAUDE_CLI_EFFORT and CLAUDE_CLI_EFFORT not in _VALID_CLAUDE_EFFORTS:
        print(
            "error: CLAUDE_LAUNCH_CLI_EFFORT must be one of: "
            + ", ".join(sorted(_VALID_CLAUDE_EFFORTS)),
            file=sys.stderr,
        )
        sys.exit(2)
    if DEFAULT_REASONING_EFFORT and DEFAULT_REASONING_EFFORT not in _VALID_REASONING_EFFORTS:
        print(
            "error: CLAUDE_LAUNCH_REASONING_EFFORT must be one of: "
            + ", ".join(sorted(_VALID_REASONING_EFFORTS)),
            file=sys.stderr,
        )
        sys.exit(2)


def _debug_write(name: str, obj: Any) -> None:
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        path = os.path.join(DEBUG_DIR, name)
        with open(path, "w", encoding="utf-8") as f:
            if isinstance(obj, (bytes, bytearray)):
                f.write(obj.decode("utf-8", errors="replace"))
            elif isinstance(obj, str):
                f.write(obj)
            else:
                json.dump(obj, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _redact_headers(headers: Any) -> dict[str, str]:
    sensitive = {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "anthropic-api-key",
        "anthropic-auth-token",
    }
    out: dict[str, str] = {}
    try:
        items = headers.items()
    except Exception:
        items = []
    for key, value in items:
        key_str = str(key)
        if key_str.lower() in sensitive:
            out[key_str] = "[redacted]"
        else:
            out[key_str] = str(value)
    return out


def _payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    messages = payload.get("messages") if isinstance(payload, dict) else None
    tools = payload.get("tools") if isinstance(payload, dict) else None
    return {
        "model": payload.get("model"),
        "stream": payload.get("stream"),
        "max_tokens": payload.get("max_tokens"),
        "reasoning_effort": payload.get("reasoning_effort"),
        "output_config": payload.get("output_config"),
        "message_count": len(messages) if isinstance(messages, list) else 0,
        "tool_count": len(tools) if isinstance(tools, list) else 0,
    }


def _capture_log(event: str, data: dict[str, Any]) -> None:
    if not CAPTURE_LOG_ENABLED:
        return
    try:
        log_dir = os.path.dirname(CAPTURE_LOG_FILE)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
            **data,
        }
        with open(CAPTURE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass


def read_body(handler: BaseHTTPRequestHandler) -> bytes:
    te = (handler.headers.get("Transfer-Encoding") or "").lower()
    if "chunked" in te:
        body = bytearray()
        rfile = handler.rfile
        while True:
            line = rfile.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                chunk_size = int(line.split(b";")[0], 16)
            except ValueError:
                break
            if chunk_size == 0:
                while True:
                    trailer = rfile.readline()
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        break
                break
            body.extend(rfile.read(chunk_size))
            rfile.read(2)
        return bytes(body)

    length = int(handler.headers.get("Content-Length") or 0)
    return handler.rfile.read(length) if length > 0 else b""


def _normalize_json_value(node: Any) -> Any:
    if isinstance(node, dict):
        return {str(k): _normalize_json_value(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_normalize_json_value(item) for item in node]
    if node is None or isinstance(node, (str, int, float, bool)):
        return node
    return str(node)


def _normalize_schema(node: Any) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "type" and isinstance(value, str):
                out[key] = value.lower()
            else:
                out[key] = _normalize_schema(value)
        return out
    if isinstance(node, list):
        return [_normalize_schema(item) for item in node]
    return node


def normalize_anthropic_content_blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]
    if isinstance(content, dict):
        return [content]
    return [{"type": "text", "text": str(content)}]


def block_text_value(block: dict[str, Any], *, include_thinking: bool = False) -> str:
    block_type = str(block.get("type") or "").strip().lower()
    if block_type == "text":
        return str(block.get("text") or "")
    if include_thinking and block_type == "thinking":
        return str(block.get("thinking") or "")
    return ""


def join_text_chunks(chunks: list[str]) -> str:
    return "".join(chunk for chunk in chunks if chunk)


def convert_anthropic_system_to_text(system: Any) -> str:
    if isinstance(system, str):
        return system.strip()
    if isinstance(system, list):
        return join_text_chunks(
            [block_text_value(item, include_thinking=False) for item in system if isinstance(item, dict)]
        ).strip()
    return ""


def extract_tool_result_content(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = join_text_chunks(
            [block_text_value(item, include_thinking=False) for item in content if isinstance(item, dict)]
        )
        if not text:
            text = json.dumps(_normalize_json_value(content), ensure_ascii=False)
    elif content is None:
        text = ""
    else:
        text = json.dumps(_normalize_json_value(content), ensure_ascii=False)
    if block.get("is_error"):
        return "[tool error]\n" + text if text else "[tool error]"
    return text


def make_tool_use_id(raw_id: str | None = None) -> str:
    normalized = str(raw_id or "").strip()
    if not normalized:
        normalized = uuid.uuid4().hex
    if normalized.startswith("toolu_"):
        return normalized
    return "toolu_" + normalized


def _append_user_content_part(parts: list[dict[str, Any]], block: dict[str, Any]) -> None:
    block_type = str(block.get("type") or "").strip().lower()
    if block_type == "text":
        text = str(block.get("text") or "")
        if text:
            parts.append({"type": "text", "text": text})
        return
    if block_type == "image":
        source = block.get("source") or {}
        if (
            isinstance(source, dict)
            and str(source.get("type") or "").strip().lower() == "base64"
            and source.get("data")
            and source.get("media_type")
        ):
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": (
                            f"data:{source['media_type']};base64,{source['data']}"
                        )
                    },
                }
            )


def _serialize_user_content(parts: list[dict[str, Any]]) -> Any:
    if not parts:
        return ""
    if all(part.get("type") == "text" for part in parts):
        return "".join(str(part.get("text") or "") for part in parts)
    return parts


def convert_anthropic_messages_to_openai(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []

    for raw_message in messages or []:
        role = str(raw_message.get("role") or "").strip().lower()
        blocks = normalize_anthropic_content_blocks(raw_message.get("content"))

        if role == "user":
            user_parts: list[dict[str, Any]] = []

            def flush_user_parts() -> None:
                content = _serialize_user_content(user_parts)
                if content:
                    converted.append({"role": "user", "content": content})
                user_parts.clear()

            for block in blocks:
                block_type = str(block.get("type") or "").strip().lower()
                if block_type == "tool_result":
                    flush_user_parts()
                    converted.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(block.get("tool_use_id") or ""),
                            "content": extract_tool_result_content(block),
                        }
                    )
                    continue
                _append_user_content_part(user_parts, block)

            flush_user_parts()
            continue

        if role == "assistant":
            text_chunks: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in blocks:
                block_type = str(block.get("type") or "").strip().lower()
                if block_type == "tool_use":
                    tool_calls.append(
                        {
                            "id": make_tool_use_id(str(block.get("id") or "")),
                            "type": "function",
                            "function": {
                                "name": str(block.get("name") or ""),
                                "arguments": json.dumps(
                                    _normalize_json_value(block.get("input") or {}),
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    )
                    continue
                text = block_text_value(block, include_thinking=False)
                if text:
                    text_chunks.append(text)

            text_content = join_text_chunks(text_chunks)
            if text_content or tool_calls:
                message: dict[str, Any] = {
                    "role": "assistant",
                    "content": text_content or None,
                }
                if tool_calls:
                    message["tool_calls"] = tool_calls
                converted.append(message)
            continue

        if role == "system":
            text_content = join_text_chunks(
                [block_text_value(block, include_thinking=False) for block in blocks]
            ).strip()
            if text_content:
                converted.append({"role": "system", "content": text_content})
            continue

        text_content = join_text_chunks(
            [block_text_value(block, include_thinking=False) for block in blocks]
        )
        if text_content:
            converted.append({"role": role or "user", "content": text_content})

    return converted


def convert_anthropic_tools_to_openai(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools or []:
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description") or "",
                    "parameters": _normalize_schema(
                        tool.get("input_schema") or {"type": "object", "properties": {}}
                    ),
                },
            }
        )
    return converted


def convert_anthropic_tool_choice(tool_choice: Any) -> tuple[Any, bool | None]:
    if not isinstance(tool_choice, dict):
        return None, None

    choice_type = str(tool_choice.get("type") or "").strip().lower()
    disable_parallel = tool_choice.get("disable_parallel_tool_use")
    parallel_tool_calls = None if disable_parallel is None else not bool(disable_parallel)

    if choice_type == "auto":
        return "auto", parallel_tool_calls
    if choice_type == "any":
        return "required", parallel_tool_calls
    if choice_type == "none":
        return "none", parallel_tool_calls
    if choice_type == "tool":
        name = str(tool_choice.get("name") or "").strip()
        if name:
            return {"type": "function", "function": {"name": name}}, parallel_tool_calls
    return None, parallel_tool_calls


def resolve_reasoning_effort(payload: dict[str, Any]) -> str:
    output_config = payload.get("output_config")
    if isinstance(output_config, dict):
        raw = str(output_config.get("effort") or "").strip().lower()
        if raw in _CLAUDE_TO_OPENAI_REASONING:
            return _CLAUDE_TO_OPENAI_REASONING[raw]
    return DEFAULT_REASONING_EFFORT


def resolve_target_model(requested_model: Any) -> str:
    requested = str(requested_model or "").strip()
    if requested and requested in MODEL_MAP:
        return MODEL_MAP[requested]
    requested_lower = requested.lower()
    for key, value in MODEL_MAP.items():
        if key.lower() == requested_lower:
            return value
    for key, value in MODEL_MAP.items():
        if key != "*" and "*" in key and fnmatch.fnmatchcase(requested_lower, key.lower()):
            return value
    return MODEL_MAP.get("*") or TARGET_MODEL


def anthropic_payload_to_openai(payload: dict[str, Any], *, stream: bool) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    system_text = convert_anthropic_system_to_text(payload.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})
    messages.extend(convert_anthropic_messages_to_openai(payload.get("messages") or []))

    if not messages:
        messages = [{"role": "user", "content": "(empty request)"}]

    openai_payload: dict[str, Any] = {
        "model": resolve_target_model(payload.get("model")),
        "messages": messages,
        "stream": stream,
    }

    max_tokens = payload.get("max_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        openai_payload["max_tokens"] = max_tokens

    temperature = payload.get("temperature")
    if isinstance(temperature, (int, float)):
        openai_payload["temperature"] = temperature

    stop_sequences = payload.get("stop_sequences")
    if isinstance(stop_sequences, list) and stop_sequences:
        openai_payload["stop"] = [str(item) for item in stop_sequences if str(item)]

    reasoning_effort = resolve_reasoning_effort(payload)
    if reasoning_effort:
        openai_payload["reasoning_effort"] = reasoning_effort

    tools = convert_anthropic_tools_to_openai(payload.get("tools") or [])
    if tools:
        openai_payload["tools"] = tools
        tool_choice, parallel_tool_calls = convert_anthropic_tool_choice(payload.get("tool_choice"))
        if tool_choice is not None:
            openai_payload["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            openai_payload["parallel_tool_calls"] = parallel_tool_calls

    return openai_payload


def extract_openai_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text") is not None:
                chunks.append(str(item.get("text")))
        return "".join(chunks)
    return ""


def extract_openai_reasoning_text(node: dict[str, Any] | None) -> str:
    payload = node or {}
    if isinstance(payload.get("reasoning"), str) and payload.get("reasoning"):
        return str(payload["reasoning"])
    if isinstance(payload.get("reasoning_content"), str) and payload.get("reasoning_content"):
        return str(payload["reasoning_content"])
    details = payload.get("reasoning_details") or []
    chunks: list[str] = []
    for item in details:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            chunks.append(text)
    return "".join(chunks)


def parse_openai_tool_arguments(raw_arguments: Any) -> Any:
    if isinstance(raw_arguments, dict):
        return _normalize_json_value(raw_arguments)
    if isinstance(raw_arguments, list):
        return {"value": _normalize_json_value(raw_arguments)}
    raw = str(raw_arguments or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {"_raw": raw}
    if isinstance(parsed, dict):
        return _normalize_json_value(parsed)
    return {"value": _normalize_json_value(parsed)}


def build_anthropic_usage_payload(
    usage: dict[str, Any] | None,
    *,
    fallback_input_tokens: int = 0,
    fallback_output_tokens: int = 0,
    include_input_tokens: bool = True,
) -> dict[str, int]:
    usage = usage or {}
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or fallback_input_tokens or 0)
    output_tokens = int(
        usage.get("completion_tokens") or usage.get("output_tokens") or fallback_output_tokens or 0
    )
    payload: dict[str, int] = {"output_tokens": output_tokens}
    if include_input_tokens:
        payload["input_tokens"] = input_tokens
    cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    if cache_creation > 0:
        payload["cache_creation_input_tokens"] = cache_creation
    if cache_read > 0:
        payload["cache_read_input_tokens"] = cache_read
    return payload


def openai_finish_reason_to_anthropic_stop_reason(finish_reason: str | None) -> str:
    lowered = str(finish_reason or "").strip().lower()
    if lowered in {"tool_calls", "function_call"}:
        return "tool_use"
    if lowered == "length":
        return "max_tokens"
    return "end_turn"


def openai_response_to_anthropic(
    request_payload: dict[str, Any],
    openai_response: dict[str, Any],
) -> dict[str, Any]:
    choice = ((openai_response.get("choices") or [{}])[0]) if isinstance(openai_response, dict) else {}
    message = choice.get("message") or {}
    usage = openai_response.get("usage") or {}
    content_blocks: list[dict[str, Any]] = []

    reasoning = extract_openai_reasoning_text(message)
    if reasoning:
        content_blocks.append({"type": "thinking", "thinking": reasoning, "signature": ""})

    text_content = extract_openai_text_content(message.get("content")).strip()
    if text_content:
        content_blocks.append({"type": "text", "text": text_content})

    tool_calls = message.get("tool_calls") or []
    for tool_call in tool_calls:
        function = tool_call.get("function") or {}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": make_tool_use_id(tool_call.get("id")),
                "name": str(function.get("name") or "").strip(),
                "input": parse_openai_tool_arguments(function.get("arguments")),
            }
        )

    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    stop_reason = openai_finish_reason_to_anthropic_stop_reason(choice.get("finish_reason"))
    if tool_calls:
        stop_reason = "tool_use"

    return {
        "id": "msg_" + uuid.uuid4().hex,
        "type": "message",
        "role": "assistant",
        "model": request_payload.get("model") or CLAUDE_CLI_MODEL,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": build_anthropic_usage_payload(usage),
    }


def estimate_anthropic_input_tokens(payload: dict[str, Any]) -> int:
    serialized = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
    return max(1, len(serialized) // 4)


class AnthropicStreamEventBuilder:
    def __init__(self, response_model: str, *, initial_input_tokens: int = 0) -> None:
        self.response_model = response_model
        self.initial_input_tokens = max(0, int(initial_input_tokens or 0))
        self.message_id = "msg_" + uuid.uuid4().hex
        self.input_tokens = self.initial_input_tokens
        self.output_tokens = 0
        self.stop_reason = "end_turn"
        self.next_block_index = 0
        self.text_block_index: int | None = None
        self.pending_tool_calls: dict[int, dict[str, Any]] = {}

    def start_events(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self.message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.response_model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": build_anthropic_usage_payload(
                            None,
                            fallback_input_tokens=self.input_tokens,
                            fallback_output_tokens=0,
                        ),
                    },
                },
            )
        ]

    def feed_chunk(self, chunk: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        usage = chunk.get("usage") or {}
        if usage:
            self.input_tokens = int(
                usage.get("prompt_tokens") or usage.get("input_tokens") or self.input_tokens or 0
            )
            self.output_tokens = int(
                usage.get("completion_tokens") or usage.get("output_tokens") or self.output_tokens or 0
            )

        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or choice.get("message") or {}

            text_delta = extract_openai_text_content(delta.get("content"))
            if text_delta:
                events.extend(self._emit_text_delta(text_delta))

            tool_calls = delta.get("tool_calls") or []
            if tool_calls:
                self.stop_reason = "tool_use"
                events.extend(self._close_text_block())
                for tool_call in tool_calls:
                    events.extend(self._merge_tool_call(tool_call))

            finish_reason = choice.get("finish_reason")
            if finish_reason:
                self.stop_reason = openai_finish_reason_to_anthropic_stop_reason(finish_reason)
                if self.stop_reason == "tool_use":
                    events.extend(self._close_text_block())

        return events

    def finish_events(self) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        events.extend(self._close_text_block())
        events.extend(self._finalize_tool_calls())
        events.append(
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": self.stop_reason,
                        "stop_sequence": None,
                    },
                    "usage": build_anthropic_usage_payload(
                        {
                            "input_tokens": self.input_tokens,
                            "output_tokens": self.output_tokens,
                        },
                        include_input_tokens=False,
                    ),
                },
            )
        )
        events.append(("message_stop", {"type": "message_stop"}))
        return events

    def _emit_text_delta(self, text: str) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        if self.text_block_index is None:
            self.text_block_index = self.next_block_index
            self.next_block_index += 1
            events.append(
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": self.text_block_index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
        events.append(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.text_block_index,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )
        return events

    def _close_text_block(self) -> list[tuple[str, dict[str, Any]]]:
        if self.text_block_index is None:
            return []
        index = self.text_block_index
        self.text_block_index = None
        return [("content_block_stop", {"type": "content_block_stop", "index": index})]

    def _merge_tool_call(self, tool_call: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        try:
            slot_index = int(tool_call.get("index") or 0)
        except Exception:
            slot_index = len(self.pending_tool_calls)

        slot = self.pending_tool_calls.setdefault(
            slot_index,
            {
                "id": "",
                "name": "",
                "arguments": "",
                "emitted_length": 0,
                "block_index": None,
            },
        )

        if tool_call.get("id"):
            slot["id"] = str(tool_call.get("id") or "")

        function = tool_call.get("function") or {}
        if function.get("name"):
            slot["name"] = str(slot.get("name") or "") + str(function.get("name") or "")
        if function.get("arguments"):
            slot["arguments"] = str(slot.get("arguments") or "") + str(function.get("arguments") or "")

        events.extend(self._ensure_tool_slot_started(slot))
        return events

    def _ensure_tool_slot_started(self, slot: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        if slot.get("block_index") is None:
            slot["block_index"] = self.next_block_index
            self.next_block_index += 1
            events.append(
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": slot["block_index"],
                        "content_block": {
                            "type": "tool_use",
                            "id": make_tool_use_id(slot.get("id") or slot.get("name") or None),
                            "name": str(slot.get("name") or "tool"),
                            "input": {},
                        },
                    },
                )
            )

        emitted_length = int(slot.get("emitted_length") or 0)
        arguments = str(slot.get("arguments") or "")
        if arguments and emitted_length < len(arguments):
            partial = arguments[emitted_length:]
            slot["emitted_length"] = len(arguments)
            events.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": slot["block_index"],
                        "delta": {"type": "input_json_delta", "partial_json": partial},
                    },
                )
            )
        return events

    def _finalize_tool_calls(self) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        for _, slot in sorted(self.pending_tool_calls.items(), key=lambda item: item[0]):
            events.extend(self._ensure_tool_slot_started(slot))
            if slot.get("block_index") is not None:
                events.append(
                    (
                        "content_block_stop",
                        {
                            "type": "content_block_stop",
                            "index": int(slot["block_index"]),
                        },
                    )
                )
        self.pending_tool_calls.clear()
        return events


def iter_sse_payloads(response: Any):
    data_lines: list[str] = []

    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if not data_lines:
                continue
            data = "\n".join(data_lines)
            data_lines = []
            if data == "[DONE]":
                break
            try:
                yield json.loads(data)
            except Exception:
                continue
            continue

        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    if data_lines:
        data = "\n".join(data_lines)
        if data != "[DONE]":
            try:
                yield json.loads(data)
            except Exception:
                pass


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _has_flag(args: list[str], name: str) -> bool:
    return any(arg == name or arg.startswith(name + "=") for arg in args)


def prepare_claude_args(args: list[str]) -> list[str]:
    prepared = list(args)
    if prepared and prepared[0] == "exec":
        prepared = prepared[1:]
        if not _has_flag(prepared, "--print") and "-p" not in prepared:
            prepared = ["-p", *prepared]

    if (
        CLAUDE_SETTING_SOURCES
        and CLAUDE_SETTING_SOURCES.lower() not in {"default", "inherit"}
        and not _has_flag(prepared, "--setting-sources")
    ):
        prepared = ["--setting-sources", CLAUDE_SETTING_SOURCES, *prepared]
    if not _has_flag(prepared, "--model") and CLAUDE_CLI_MODEL:
        prepared = ["--model", CLAUDE_CLI_MODEL, *prepared]
    if not _has_flag(prepared, "--effort") and CLAUDE_CLI_EFFORT:
        prepared = ["--effort", CLAUDE_CLI_EFFORT, *prepared]
    return prepared


class TranslationProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("CLAUDE_LAUNCH_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Connection", "close")
        self.end_headers()

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path or "").path
        if path in ("", "/", "/v1", "/healthz"):
            self._send_json(
                {
                    "ok": True,
                    "base": TARGET_BASE_URL,
                    "model": TARGET_MODEL,
                    "cli_model": CLAUDE_CLI_MODEL,
                }
            )
            return
        if path in ("/v1/models", "/models"):
            self._send_json(
                {
                    "data": [
                        {
                            "type": "model",
                            "id": CLAUDE_CLI_MODEL,
                            "display_name": LOCAL_MODEL_DISPLAY_NAME or TARGET_MODEL or CLAUDE_CLI_MODEL,
                            "created_at": "2026-01-01T00:00:00Z",
                        }
                    ],
                    "first_id": CLAUDE_CLI_MODEL,
                    "last_id": CLAUDE_CLI_MODEL,
                    "has_more": False,
                }
            )
            return
        self._send_json({"type": "error", "error": {"type": "not_found_error", "message": "Not found"}}, status=404)

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path or "").path
        body = read_body(self)

        if path in ("/v1/messages", "/messages"):
            self._handle_messages(body)
            return
        if path in ("/v1/messages/count_tokens", "/messages/count_tokens"):
            self._handle_count_tokens(body)
            return

        self._send_json({"type": "error", "error": {"type": "not_found_error", "message": "Not found"}}, status=404)

    def _send_json(self, obj: Any, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _begin_sse(self) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ClientDisconnectedError() from exc

    def _send_sse_event(self, event: str, payload: dict[str, Any]) -> None:
        data = (
            f"event: {event}\n"
            f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        ).encode("utf-8")
        try:
            self.wfile.write(f"{len(data):X}\r\n".encode("utf-8"))
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ClientDisconnectedError() from exc

    def _finish_sse(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass

    def _send_anthropic_stream_from_dict(self, payload: dict[str, Any]) -> None:
        builder = AnthropicStreamEventBuilder(
            str(payload.get("model") or CLAUDE_CLI_MODEL),
            initial_input_tokens=int((payload.get("usage") or {}).get("input_tokens") or 0),
        )
        self._begin_sse()
        try:
            for event, data in builder.start_events():
                self._send_sse_event(event, data)

            for idx, block in enumerate(payload.get("content") or []):
                block_type = str(block.get("type") or "").strip().lower()
                if block_type == "thinking":
                    continue
                if block_type == "text":
                    self._send_sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": idx,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                    self._send_sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": idx,
                            "delta": {"type": "text_delta", "text": str(block.get("text") or "")},
                        },
                    )
                    self._send_sse_event(
                        "content_block_stop",
                        {"type": "content_block_stop", "index": idx},
                    )
                    continue
                if block_type == "tool_use":
                    self._send_sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": idx,
                            "content_block": {
                                "type": "tool_use",
                                "id": make_tool_use_id(block.get("id")),
                                "name": str(block.get("name") or "tool"),
                                "input": {},
                            },
                        },
                    )
                    raw_input = json.dumps(_normalize_json_value(block.get("input") or {}), ensure_ascii=False)
                    if raw_input and raw_input != "{}":
                        self._send_sse_event(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": idx,
                                "delta": {"type": "input_json_delta", "partial_json": raw_input},
                            },
                        )
                    self._send_sse_event(
                        "content_block_stop",
                        {"type": "content_block_stop", "index": idx},
                    )

            self._send_sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": payload.get("stop_reason") or "end_turn",
                        "stop_sequence": None,
                    },
                    "usage": build_anthropic_usage_payload(
                        payload.get("usage") or {},
                        include_input_tokens=False,
                    ),
                },
            )
            self._send_sse_event("message_stop", {"type": "message_stop"})
        finally:
            self._finish_sse()

    def _send_stream_error(self, request_payload: dict[str, Any], message: str) -> None:
        self._send_anthropic_stream_from_dict(
            {
                "id": "msg_" + uuid.uuid4().hex,
                "type": "message",
                "role": "assistant",
                "model": request_payload.get("model") or CLAUDE_CLI_MODEL,
                "content": [{"type": "text", "text": message}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": estimate_anthropic_input_tokens(request_payload), "output_tokens": 0},
            }
        )

    def _handle_count_tokens(self, raw_body: bytes) -> None:
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except Exception as exc:
            self._send_json({"type": "error", "error": {"type": "invalid_request_error", "message": str(exc)}}, status=400)
            return
        self._send_json({"input_tokens": estimate_anthropic_input_tokens(payload)})

    def _perform_upstream_request(
        self,
        openai_payload: dict[str, Any],
        *,
        stream: bool,
        request_id: str,
    ) -> Any:
        api_url = f"{TARGET_BASE_URL}/chat/completions"
        attempts = [openai_payload]
        if "reasoning_effort" in openai_payload:
            retry_payload = dict(openai_payload)
            retry_payload.pop("reasoning_effort", None)
            attempts.append(retry_payload)

        last_error: Exception | None = None
        for idx, payload in enumerate(attempts):
            _debug_write("outgoing_openai_request.json", payload)
            _debug_write(f"{request_id}-outgoing_openai_request-attempt-{idx + 1}.json", payload)
            _capture_log(
                "upstream_request",
                {
                    "request_id": request_id,
                    "attempt": idx + 1,
                    "url": api_url,
                    "stream": stream,
                    "summary": _payload_summary(payload),
                },
            )
            req = urllib.request.Request(
                api_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {TARGET_API_KEY}",
                    "Accept": "text/event-stream" if stream else "application/json",
                    "User-Agent": UPSTREAM_USER_AGENT,
                },
                method="POST",
            )
            try:
                response = urllib.request.urlopen(req, timeout=600)
                _capture_log(
                    "upstream_response",
                    {
                        "request_id": request_id,
                        "attempt": idx + 1,
                        "status": getattr(response, "status", None),
                        "headers": _redact_headers(response.headers),
                    },
                )
                return response
            except urllib.error.HTTPError as exc:
                last_error = exc
                _capture_log(
                    "upstream_http_error",
                    {
                        "request_id": request_id,
                        "attempt": idx + 1,
                        "status": exc.code,
                        "reason": str(exc),
                    },
                )
                if idx + 1 >= len(attempts):
                    raise
                if exc.code != 400:
                    raise
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    body = ""
                if os.environ.get("CLAUDE_LAUNCH_VERBOSE"):
                    print(
                        "[claude-launch] retrying upstream without reasoning_effort after 400:",
                        body[:400],
                        file=sys.stderr,
                    )
                continue
            except Exception as exc:
                last_error = exc
                raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("unreachable")

    def _handle_messages(self, raw_body: bytes) -> None:
        request_id = uuid.uuid4().hex[:12]
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except Exception as exc:
            self._send_json({"type": "error", "error": {"type": "invalid_request_error", "message": str(exc)}}, status=400)
            return

        _debug_write("incoming_request.json", payload)
        _debug_write(f"{request_id}-incoming_request.json", payload)
        _capture_log(
            "anthropic_request",
            {
                "request_id": request_id,
                "path": self.path,
                "headers": _redact_headers(self.headers),
                "summary": _payload_summary(payload),
            },
        )

        wants_stream = bool(payload.get("stream")) or "text/event-stream" in (
            self.headers.get("Accept") or ""
        ).lower()
        openai_payload = anthropic_payload_to_openai(payload, stream=wants_stream)
        _capture_log(
            "model_mapping",
            {
                "request_id": request_id,
                "anthropic_model": payload.get("model"),
                "upstream_model": openai_payload.get("model"),
                "model_map": MODEL_MAP,
                "wants_stream": wants_stream,
            },
        )

        try:
            with self._perform_upstream_request(
                openai_payload,
                stream=wants_stream,
                request_id=request_id,
            ) as response:
                content_type = (response.headers.get("Content-Type") or "").lower()

                if wants_stream:
                    if "application/json" in content_type:
                        response_payload = json.loads(response.read().decode("utf-8") or "{}")
                        anthropic_payload = openai_response_to_anthropic(payload, response_payload)
                        self._send_anthropic_stream_from_dict(anthropic_payload)
                        return

                    builder = AnthropicStreamEventBuilder(
                        str(payload.get("model") or CLAUDE_CLI_MODEL),
                        initial_input_tokens=estimate_anthropic_input_tokens(payload),
                    )
                    self._begin_sse()
                    try:
                        for event, data in builder.start_events():
                            self._send_sse_event(event, data)
                        for chunk in iter_sse_payloads(response):
                            for event, data in builder.feed_chunk(chunk):
                                self._send_sse_event(event, data)
                        for event, data in builder.finish_events():
                            self._send_sse_event(event, data)
                    finally:
                        self._finish_sse()
                    return

                response_payload = json.loads(response.read().decode("utf-8") or "{}")
                self._send_json(openai_response_to_anthropic(payload, response_payload))

        except urllib.error.HTTPError as exc:
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = ""
            _debug_write(
                "failed_request.json",
                {
                    "error": str(exc),
                    "status": exc.code,
                    "response_body": err_body,
                    "payload": openai_payload,
                },
            )
            _debug_write(
                f"{request_id}-failed_request.json",
                {
                    "error": str(exc),
                    "status": exc.code,
                    "response_body": err_body,
                    "payload": openai_payload,
                },
            )
            _capture_log(
                "request_failed",
                {
                    "request_id": request_id,
                    "status": exc.code,
                    "error": str(exc),
                    "response_body_preview": err_body[:1000],
                },
            )
            message = f"Upstream API Error: HTTP {exc.code}"
            if err_body:
                message += f"\n{err_body}"
            if wants_stream:
                self._send_stream_error(payload, message)
                return
            self._send_json(
                {"type": "error", "error": {"type": "api_error", "message": message}},
                status=502,
            )
        except ClientDisconnectedError:
            return
        except Exception as exc:
            _debug_write(
                "failed_request.json",
                {
                    "error": str(exc),
                    "payload": openai_payload,
                },
            )
            _debug_write(
                f"{request_id}-failed_request.json",
                {
                    "error": str(exc),
                    "payload": openai_payload,
                },
            )
            _capture_log(
                "request_failed",
                {
                    "request_id": request_id,
                    "error": str(exc),
                },
            )
            message = f"Upstream API Error: {exc}"
            if wants_stream:
                self._send_stream_error(payload, message)
                return
            self._send_json(
                {"type": "error", "error": {"type": "api_error", "message": message}},
                status=502,
            )


def main() -> None:
    load_config()

    port = int(os.environ.get("CLAUDE_LAUNCH_PORT") or 0) or find_free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), TranslationProxy)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    env["ANTHROPIC_API_KEY"] = LOCAL_PROXY_API_KEY
    env["CLAUDE_CODE_USE_GATEWAY"] = "0"
    env.pop("ANTHROPIC_AUTH_TOKEN", None)

    args = prepare_claude_args(sys.argv[1:])

    if os.environ.get("CLAUDE_LAUNCH_VERBOSE"):
        env_note = ", ".join(_LOADED_ENV_FILES) if _LOADED_ENV_FILES else "(none)"
        map_note = ", ".join(f"{key}->{value}" for key, value in sorted(MODEL_MAP.items()))
        print(
            f"[claude-launch] proxy=http://127.0.0.1:{port} "
            f"upstream={TARGET_BASE_URL}/chat/completions "
            f"default_upstream_model={TARGET_MODEL} cli_model={CLAUDE_CLI_MODEL} "
            f"setting_sources={CLAUDE_SETTING_SOURCES or '(claude default)'}\n"
            f"[claude-launch] model map: {map_note}\n"
            f"[claude-launch] env files: {env_note}",
            file=sys.stderr,
        )

    try:
        result = subprocess.run([CLAUDE_BIN, *args], env=env)
        sys.exit(result.returncode)
    except FileNotFoundError:
        print(f"error: cannot find claude binary ({CLAUDE_BIN})", file=sys.stderr)
        sys.exit(127)
    except KeyboardInterrupt:
        sys.exit(130)
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
