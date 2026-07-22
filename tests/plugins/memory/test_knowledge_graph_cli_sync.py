from __future__ import annotations

import argparse
from unittest import mock

from plugins.memory.knowledge_graph import cli


def _parse(*argv: str):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    kg = sub.add_parser("knowledge-graph")
    cli.register_cli(kg)
    return parser.parse_args(["knowledge-graph", *argv])


def test_import_sessions_parser_defaults_to_dry_run():
    args = _parse("import-sessions", "--db", "/tmp/state.db")
    assert args.kg_action == "import_sessions"
    assert args.write is False
    assert args.no_embed is False
    assert args.limit == 0


def test_import_sessions_dry_run_does_not_initialize_embedder(tmp_path, monkeypatch, capsys):
    db = tmp_path / "state.db"
    db.touch()
    store = mock.MagicMock()
    store.connect.return_value = True
    store_cls = mock.MagicMock(return_value=store)
    make_embed = mock.MagicMock()
    monkeypatch.setattr(cli, "_load_bits", lambda: (store_cls, lambda: {}, make_embed))

    imported = mock.MagicMock(return_value={"dry_run": True, "counts": {"sessions": 2}})
    monkeypatch.setattr(
        "plugins.memory.knowledge_graph.session_backfill.import_state_db", imported
    )

    cli.knowledge_graph_command(_parse("import-sessions", "--db", str(db)))

    make_embed.assert_not_called()
    imported.assert_called_once_with(
        store,
        None,
        str(db),
        since_ts=None,
        limit_sessions=None,
        embed_tools=False,
        dry_run=True,
        progress=mock.ANY,
    )
    assert '"dry_run": true' in capsys.readouterr().out
    store.close.assert_called_once()


def test_import_sessions_write_can_be_graph_only(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    db.touch()
    store = mock.MagicMock()
    store.connect.return_value = True
    make_embed = mock.MagicMock()
    monkeypatch.setattr(
        cli, "_load_bits", lambda: (mock.MagicMock(return_value=store), lambda: {}, make_embed)
    )
    imported = mock.MagicMock(return_value={"dry_run": False})
    monkeypatch.setattr(
        "plugins.memory.knowledge_graph.session_backfill.import_state_db", imported
    )

    cli.knowledge_graph_command(
        _parse("import-sessions", "--db", str(db), "--write", "--no-embed", "--limit", "7")
    )

    make_embed.assert_not_called()
    assert imported.call_args.kwargs["dry_run"] is False
    assert imported.call_args.kwargs["limit_sessions"] == 7
    assert imported.call_args.args[1] is None
