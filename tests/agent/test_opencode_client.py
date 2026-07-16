"""Unit tests for the OpenCode CLI subagent transport.

These mock ``subprocess.run`` so no real ``opencode`` binary is required.
We assert on (a) the command line built and (b) the OpenAI-shaped response.
"""

from __future__ import annotations

from unittest import mock

from agent.opencode_client import OpenCodeClient, _clean_response


def _fake_run(stdout: str, returncode: int = 0, stderr: str = ""):
    proc = mock.MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_create_chat_completion_returns_openai_shape():
    with mock.patch.object(OpenCodeClient, "_run_prompt", return_value="PONG") as rp:
        c = OpenCodeClient(model="lmstudio/qwen", command="opencode")
        ns = c._create_chat_completion(
            model="lmstudio/qwen",
            messages=[{"role": "user", "content": "ping"}],
        )
        # _run_prompt should receive the flattened prompt.
        rp.assert_called_once()
        assert rp.call_args.args[0] == "User: ping"
        msg = ns.choices[0].message
        assert msg.content == "PONG"
        assert msg.tool_calls == []
        assert ns.choices[0].finish_reason == "stop"
        assert ns.model == "lmstudio/qwen"


def test_run_prompt_builds_positional_command_and_model():
    with mock.patch("agent.opencode_client.subprocess.run",
                    return_value=_fake_run("answer\n")) as run:
        c = OpenCodeClient(command="opencode", model="lmstudio/qwen", cwd="/tmp/proj")
        out = c._run_prompt("do the thing", timeout_seconds=10)
        assert out == "answer"
        cmd = run.call_args.args[0]
        assert cmd[0] == "opencode"
        assert cmd[1] == "run"
        # message is positional, before the flags
        assert cmd[2] == "do the thing"
        assert "--auto" in cmd
        assert "--format" in cmd and "default" in cmd
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "lmstudio/qwen"
        # cwd scoping
        assert run.call_args.kwargs["cwd"] == "/tmp/proj"
        assert run.call_args.kwargs["timeout"] == 10


def test_run_prompt_no_model_flag_when_unspecified():
    with mock.patch("agent.opencode_client.subprocess.run",
                    return_value=_fake_run("ok")) as run:
        c = OpenCodeClient(command="opencode")
        c._run_prompt("x", timeout_seconds=5)
        cmd = run.call_args.args[0]
        assert "--model" not in cmd


def test_run_prompt_raises_on_nonzero_exit():
    with mock.patch("agent.opencode_client.subprocess.run",
                    return_value=_fake_run("boom", returncode=3, stderr="nope")):
        c = OpenCodeClient(command="opencode")
        try:
            c._run_prompt("x", timeout_seconds=5)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "exited 3" in str(e)


def test_clean_response_strips_banner_and_footer():
    raw = (
        "\x1b[0mPONG\n"
        "\x1b[0m\n"
        "> build · tencent/hy3:free\n"
    )
    assert _clean_response(raw) == "PONG"
