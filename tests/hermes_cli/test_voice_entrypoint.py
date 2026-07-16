import os
import sys


def test_voice_status_subcommand_dispatches(monkeypatch):
    import hermes_cli.main as main_mod

    captured = {}

    def fake_voice_status(args):
        captured["command"] = args.command
        captured["voice_command"] = getattr(args, "voice_command", None)

    monkeypatch.setattr(main_mod, "cmd_voice_status", fake_voice_status)
    monkeypatch.setattr(sys, "argv", ["hermes", "voice", "status"])

    main_mod.main()

    assert captured == {"command": "voice", "voice_command": "status"}


def test_voice_off_subcommand_routes_to_chat_and_clears_voice_env(monkeypatch):
    import hermes_cli.main as main_mod

    captured = {}

    def fake_prepare_agent_startup(args):
        captured["prepared"] = True

    def fake_cmd_chat(args):
        captured["command"] = args.command
        captured["tui"] = args.tui
        captured["env"] = (
            os.environ.get("HERMES_TUI"),
            os.environ.get("HERMES_VOICE"),
            os.environ.get("HERMES_VOICE_TTS"),
        )

    monkeypatch.setenv("HERMES_TUI", "1")
    monkeypatch.setenv("HERMES_VOICE", "1")
    monkeypatch.setenv("HERMES_VOICE_TTS", "1")
    monkeypatch.setattr(main_mod, "_prepare_agent_startup", fake_prepare_agent_startup)
    monkeypatch.setattr(main_mod, "cmd_chat", fake_cmd_chat)
    monkeypatch.setattr(sys, "argv", ["hermes", "voice", "off"])

    main_mod.main()

    assert captured == {
        "prepared": True,
        "command": "chat",
        "tui": False,
        "env": (None, None, None),
    }