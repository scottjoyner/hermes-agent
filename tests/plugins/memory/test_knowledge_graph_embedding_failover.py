from __future__ import annotations

import json
from types import SimpleNamespace

from plugins.memory.knowledge_graph.embeddings import LocalEmbeddingClient


class _Embeddings:
    def __init__(self, *, vector=None, error=None):
        self.vector = vector
        self.error = error
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        inputs = kwargs["input"]
        count = len(inputs) if isinstance(inputs, list) else 1
        return SimpleNamespace(data=[SimpleNamespace(embedding=self.vector) for _ in range(count)])


def test_registry_candidates_are_priority_ordered_and_normalized(tmp_path):
    registry = tmp_path / "endpoints.json"
    registry.write_text(
        json.dumps(
            {
                "endpoints": [
                    {"priority": 9, "url": "http://slow:1235/v1/embeddings"},
                    {"priority": 2, "url": "http://fast:1235/v1/embeddings"},
                ]
            }
        )
    )
    client = LocalEmbeddingClient(base_url="http://local:1234/v1", registry_path=str(registry))

    assert client._candidate_base_urls() == [
        "http://local:1234/v1",
        "http://fast:1235/v1",
        "http://slow:1235/v1",
    ]


def test_embedding_fails_over_to_registry_and_pins_live_endpoint(tmp_path, monkeypatch):
    registry = tmp_path / "endpoints.json"
    registry.write_text(
        json.dumps({"endpoints": [{"priority": 1, "url": "http://fleet:1235/v1/embeddings"}]})
    )
    client = LocalEmbeddingClient(
        base_url="http://dead:1234/v1",
        registry_path=str(registry),
        model="nomic",
    )
    dead = SimpleNamespace(embeddings=_Embeddings(error=ConnectionError("down")))
    live = SimpleNamespace(embeddings=_Embeddings(vector=[0.1, 0.2, 0.3]))
    clients = {
        "http://dead:1234/v1": dead,
        "http://fleet:1235/v1": live,
    }
    monkeypatch.setattr(client, "_get_client", lambda url: clients[url])

    assert client.embed("hello") == [0.1, 0.2, 0.3]
    assert client._active_base_url == "http://fleet:1235/v1"
    assert client.dimension == 3
    assert dead.embeddings.calls == 1
    assert live.embeddings.calls == 1

    # A repeated text is served from cache without touching either endpoint.
    assert client.embed("hello") == [0.1, 0.2, 0.3]
    assert dead.embeddings.calls == 1
    assert live.embeddings.calls == 1


def test_malformed_registry_keeps_configured_endpoint(tmp_path):
    registry = tmp_path / "endpoints.json"
    registry.write_text("not-json")
    client = LocalEmbeddingClient(base_url="http://local:1234/v1", registry_path=str(registry))

    assert client._candidate_base_urls() == ["http://local:1234/v1"]
