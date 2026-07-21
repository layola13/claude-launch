import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import main  # noqa: E402


class TranslationTests(unittest.TestCase):
    def setUp(self) -> None:
        main.TARGET_MODEL = "gpt-5.4"
        main.MODEL_MAP = {"*": "gpt-5.4"}
        main.CLAUDE_CLI_MODEL = "claude-sonnet-5"
        main.CLAUDE_CLI_EFFORT = ""
        main.CLAUDE_SETTING_SOURCES = "user"
        main.DEFAULT_REASONING_EFFORT = ""
        main.LOCAL_MODEL_DISPLAY_NAME = "gpt-5.4"

    def test_anthropic_payload_maps_messages_tools_and_tool_results(self) -> None:
        payload = {
            "model": "claude-sonnet-5",
            "system": [{"type": "text", "text": "You are Claude Code."}],
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "先读文件。"},
                        {
                            "type": "tool_use",
                            "id": "toolu_prev",
                            "name": "Read",
                            "input": {"file_path": "/tmp/demo.txt"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_prev",
                            "content": [{"type": "text", "text": "body"}],
                        },
                        {"type": "text", "text": "继续"},
                    ],
                },
            ],
            "tools": [
                {
                    "name": "Bash",
                    "description": "Run shell commands",
                    "input_schema": {
                        "type": "OBJECT",
                        "properties": {"command": {"type": "STRING"}},
                        "required": ["command"],
                    },
                }
            ],
            "tool_choice": {"type": "tool", "name": "Bash", "disable_parallel_tool_use": True},
            "max_tokens": 2048,
        }

        request = main.anthropic_payload_to_openai(payload, stream=False)

        self.assertEqual(request["model"], "gpt-5.4")
        self.assertEqual(request["messages"][0]["role"], "system")
        self.assertEqual(request["messages"][1]["role"], "assistant")
        self.assertEqual(request["messages"][1]["tool_calls"][0]["function"]["name"], "Read")
        self.assertEqual(request["messages"][2]["role"], "tool")
        self.assertEqual(request["messages"][2]["tool_call_id"], "toolu_prev")
        self.assertEqual(request["messages"][3]["role"], "user")
        self.assertEqual(request["messages"][3]["content"], "继续")
        self.assertEqual(request["tools"][0]["function"]["parameters"]["type"], "object")
        self.assertEqual(request["tool_choice"], {"type": "function", "function": {"name": "Bash"}})
        self.assertFalse(request["parallel_tool_calls"])

    def test_output_config_effort_maps_to_reasoning_effort(self) -> None:
        payload = main.anthropic_payload_to_openai(
            {
                "model": "claude-sonnet-5",
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                "output_config": {"effort": "xhigh"},
            },
            stream=False,
        )

        self.assertEqual(payload["reasoning_effort"], "high")

    def test_request_model_maps_to_configured_upstream_model(self) -> None:
        main.MODEL_MAP = {
            "*": "fallback-model",
            "claude-opus-4.1": "gpt-5.4",
            "claude-opus-*": "gpt-5.4-opus",
            "sonnet": "gpt-5.4-mini",
        }

        payload = main.anthropic_payload_to_openai(
            {
                "model": "claude-opus-4.1",
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            },
            stream=False,
        )

        self.assertEqual(payload["model"], "gpt-5.4")

        wildcard_payload = main.anthropic_payload_to_openai(
            {
                "model": "claude-opus-4-8",
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            },
            stream=False,
        )

        self.assertEqual(wildcard_payload["model"], "gpt-5.4-opus")

    def test_exec_alias_becomes_print_mode_with_default_model(self) -> None:
        main.CLAUDE_CLI_MODEL = "claude-sonnet-5"
        main.CLAUDE_CLI_EFFORT = "high"

        args = main.prepare_claude_args(["exec", "hello"])

        self.assertEqual(
            args,
            [
                "--effort",
                "high",
                "--model",
                "claude-sonnet-5",
                "--setting-sources",
                "user",
                "-p",
                "hello",
            ],
        )

    def test_existing_setting_sources_are_preserved(self) -> None:
        main.CLAUDE_SETTING_SOURCES = "user"

        args = main.prepare_claude_args(["--setting-sources", "user,project,local", "-p", "hello"])

        self.assertEqual(
            args,
            ["--model", "claude-sonnet-5", "--setting-sources", "user,project,local", "-p", "hello"],
        )

    def test_openai_response_becomes_anthropic_message(self) -> None:
        response = main.openai_response_to_anthropic(
            {"model": "claude-sonnet-5"},
            {
                "choices": [
                    {
                        "message": {
                            "content": "最终答案",
                            "tool_calls": [
                                {
                                    "id": "call_123",
                                    "type": "function",
                                    "function": {
                                        "name": "Bash",
                                        "arguments": json.dumps({"command": "pwd"}, ensure_ascii=False),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
            },
        )

        self.assertEqual(response["type"], "message")
        self.assertEqual(response["model"], "claude-sonnet-5")
        self.assertEqual(response["stop_reason"], "tool_use")
        self.assertEqual(response["content"][0], {"type": "text", "text": "最终答案"})
        self.assertEqual(response["content"][1]["type"], "tool_use")
        self.assertEqual(response["content"][1]["id"], "toolu_call_123")
        self.assertEqual(response["content"][1]["input"], {"command": "pwd"})

    def test_stream_builder_emits_text_and_tool_events(self) -> None:
        builder = main.AnthropicStreamEventBuilder("claude-sonnet-5", initial_input_tokens=7)
        events = builder.start_events()
        events.extend(
            builder.feed_chunk(
                {
                    "choices": [
                        {
                            "delta": {"content": "hello "},
                            "finish_reason": None,
                        }
                    ]
                }
            )
        )
        events.extend(
            builder.feed_chunk(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "Bash", "arguments": "{\"command\":\"pwd\"}"},
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                }
            )
        )
        events.extend(builder.finish_events())

        names = [name for name, _ in events]
        self.assertEqual(names[0], "message_start")
        self.assertIn("content_block_start", names)
        self.assertIn("content_block_delta", names)
        self.assertIn("content_block_stop", names)
        self.assertEqual(names[-2], "message_delta")
        self.assertEqual(names[-1], "message_stop")

        tool_start = next(data for name, data in events if name == "content_block_start" and data["content_block"]["type"] == "tool_use")
        self.assertEqual(tool_start["content_block"]["name"], "Bash")
        message_delta = events[-2][1]
        self.assertEqual(message_delta["delta"]["stop_reason"], "tool_use")
        self.assertEqual(message_delta["usage"]["output_tokens"], 3)

    def test_candidate_env_paths_prefers_repo_env_over_user_env(self) -> None:
        old_here = main._HERE
        old_env = os.environ.copy()
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as home:
                repo_dir = os.path.join(home, "repo", "claude-launch")
                os.makedirs(repo_dir, exist_ok=True)
                main._HERE = repo_dir
                os.chdir(cwd)
                os.environ.clear()
                os.environ["HOME"] = home
                paths = main._candidate_env_paths()
                self.assertLess(
                    paths.index(os.path.join(repo_dir, ".env")),
                    paths.index(os.path.join(home, ".config", "claude-launch", ".env")),
                )
        finally:
            main._HERE = old_here
            os.chdir(old_cwd)
            os.environ.clear()
            os.environ.update(old_env)

    def test_token_defaults(self) -> None:
        main.MAX_COMPLETION_TOKENS = 4000
        main.MAX_TOKENS = 8000
        try:
            raw = {"model": "claude-3-opus-20240229", "messages": [{"role": "user", "content": "hello"}]}
            payload = main.anthropic_payload_to_openai(raw, stream=True)
            self.assertEqual(payload.get("max_completion_tokens"), 4000)
            self.assertEqual(payload.get("max_tokens"), 8000)
        finally:
            main.MAX_COMPLETION_TOKENS = 0
            main.MAX_TOKENS = 0

    def test_key_rotation(self) -> None:
        main.API_KEYS = ["key1", "key2", "key3"]
        main.FROZEN_KEYS = {}

        # 1. Initially first key is returned
        self.assertEqual(main.get_active_key(), "key1")

        # 2. Freeze key1 temporarily
        main.mark_key_failed("key1", 429)
        self.assertEqual(main.get_active_key(), "key2")

        # 3. Freeze key2 permanently
        main.mark_key_failed("key2", 401)
        self.assertEqual(main.get_active_key(), "key3")

        # 4. If all keys are frozen, the one that expires earliest (key1) is chosen
        main.mark_key_failed("key3", 429)
        self.assertEqual(main.get_active_key(), "key1")

        # Clean up
        main.API_KEYS = []
        main.FROZEN_KEYS = {}

    def test_candidate_env_paths_prefers_launcher_env_over_cwd_and_user_env(self) -> None:
        old_here = main._HERE
        old_env = os.environ.copy()
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as home:
                repo_dir = os.path.join(home, "repo", "claude-launch")
                os.makedirs(repo_dir, exist_ok=True)
                main._HERE = repo_dir
                os.chdir(cwd)
                os.environ.clear()
                os.environ["HOME"] = home
                paths = main._candidate_env_paths()
                self.assertLess(
                    paths.index(os.path.join(repo_dir, ".env")),
                    paths.index(os.path.join(cwd, ".env")),
                )
                self.assertLess(
                    paths.index(os.path.join(repo_dir, ".env")),
                    paths.index(os.path.join(home, ".config", "claude-launch", ".env")),
                )
        finally:
            main._HERE = old_here
            os.chdir(old_cwd)
            os.environ.clear()
            os.environ.update(old_env)

    def test_load_dotenv_files_launcher_env_overrides_stale_managed_env(self) -> None:
        old_here = main._HERE
        old_env = os.environ.copy()
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as repo_dir:
                main._HERE = repo_dir
                os.chdir(cwd)
                os.environ.clear()
                os.environ.update(
                    {
                        "CLAUDE_LAUNCH_API_KEY": "old-exported-key",
                        "CLAUDE_LAUNCH_MODEL": "old-exported-model",
                        "UNRELATED_SETTING": "shell-value",
                    }
                )
                with open(os.path.join(repo_dir, ".env"), "w", encoding="utf-8") as f:
                    f.write("CLAUDE_LAUNCH_API_KEY=new-launcher-key\n")
                    f.write("CLAUDE_LAUNCH_MODEL=new-launcher-model\n")
                    f.write("UNRELATED_SETTING=dotenv-value\n")

                main.load_dotenv_files()

                self.assertEqual(os.environ["CLAUDE_LAUNCH_API_KEY"], "new-launcher-key")
                self.assertEqual(os.environ["CLAUDE_LAUNCH_MODEL"], "new-launcher-model")
                self.assertEqual(os.environ["UNRELATED_SETTING"], "shell-value")
        finally:
            main._HERE = old_here
            os.chdir(old_cwd)
            os.environ.clear()
            os.environ.update(old_env)

    def test_load_dotenv_files_launcher_env_wins_over_cwd_env(self) -> None:
        old_here = main._HERE
        old_env = os.environ.copy()
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as repo_dir:
                main._HERE = repo_dir
                os.chdir(cwd)
                os.environ.clear()
                with open(os.path.join(repo_dir, ".env"), "w", encoding="utf-8") as f:
                    f.write("CLAUDE_LAUNCH_API_KEY=launcher-key\n")
                with open(os.path.join(cwd, ".env"), "w", encoding="utf-8") as f:
                    f.write("CLAUDE_LAUNCH_API_KEY=cwd-key\n")

                main.load_dotenv_files()

                self.assertEqual(os.environ["CLAUDE_LAUNCH_API_KEY"], "launcher-key")
        finally:
            main._HERE = old_here
            os.chdir(old_cwd)
            os.environ.clear()
            os.environ.update(old_env)


if __name__ == "__main__":
    unittest.main()
