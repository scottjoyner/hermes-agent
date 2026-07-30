from __future__ import annotations

from pathlib import Path

from agent.prompt_builder import build_context_files_prompt


REPO_ROOT = Path(__file__).resolve().parents[1]
_HERMES_CONTEXT_MAX_CHARS = 8_000


def test_root_hermes_context_is_lean_and_preferred_over_agents_md():
    hermes_path = REPO_ROOT / ".hermes.md"
    agents_path = REPO_ROOT / "AGENTS.md"

    assert hermes_path.is_file()
    assert agents_path.is_file()

    hermes_content = hermes_path.read_text(encoding="utf-8")
    assert len(hermes_content) <= _HERMES_CONTEXT_MAX_CHARS
    assert len(hermes_content) < len(agents_path.read_text(encoding="utf-8"))

    prompt = build_context_files_prompt(
        cwd=str(REPO_ROOT),
        skip_soul=True,
        context_length=8_192,
        allow_install_tree_fallback=True,
    )

    assert "## .hermes.md" in prompt
    assert "## AGENTS.md" not in prompt
    assert "project operating brief" in prompt
    assert len(prompt) <= _HERMES_CONTEXT_MAX_CHARS + 500
