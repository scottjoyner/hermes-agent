from __future__ import annotations

from agent.cache_foundation import (
    CacheRouteCandidate,
    CacheStateStore,
    build_cache_headers,
    build_manifest,
    hash_json,
    is_cache_eligible_endpoint,
    route_score,
    select_best_route,
)


def _request(system_content, *, user: str = "hello", tools=None):
    return {
        "model": "local/qwen",
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user},
        ],
        "tools": tools or [],
    }


def test_hash_json_sorts_mapping_keys_but_preserves_list_order():
    assert hash_json({"b": 2, "a": 1}) == hash_json({"a": 1, "b": 2})
    assert hash_json(["a", "b"]) != hash_json(["b", "a"])


def test_checkpoint_and_prior_prefix_ignore_newest_user_item():
    first = build_manifest(
        _request("stable system", user="first"),
        session_id="session-a",
        model="local/qwen",
        provider="lmstudio",
        api_mode="chat_completions",
        base_url="http://127.0.0.1:1234/v1",
    )
    second = build_manifest(
        _request("stable system", user="second"),
        session_id="session-b",
        model="local/qwen",
        provider="lmstudio",
        api_mode="chat_completions",
        base_url="http://127.0.0.1:1234/v1",
    )

    assert first.checkpoint_id == second.checkpoint_id
    assert first.request_prefix_id == second.request_prefix_id
    assert first.session_id != second.session_id


def test_tool_schema_changes_invalidate_checkpoint():
    first = build_manifest(
        _request(
            "system",
            tools=[{"type": "function", "function": {"name": "read_file"}}],
        ),
        model="model",
        provider="ollama",
        api_mode="chat_completions",
        base_url="http://localhost:11434/v1",
    )
    second = build_manifest(
        _request(
            "system",
            tools=[{"type": "function", "function": {"name": "write_file"}}],
        ),
        model="model",
        provider="ollama",
        api_mode="chat_completions",
        base_url="http://localhost:11434/v1",
    )

    assert first.tool_schema_hash != second.tool_schema_hash
    assert first.checkpoint_id != second.checkpoint_id


def test_decorated_static_prefix_excludes_volatile_system_suffix():
    static = {
        "type": "text",
        "text": "stable identity and tools",
        "cache_control": {"type": "ephemeral"},
    }
    first = build_manifest(
        _request([static, {"type": "text", "text": "session A"}]),
        model="model",
        provider="custom",
        api_mode="chat_completions",
        base_url="http://x1-370:8080/v1",
    )
    second = build_manifest(
        _request([static, {"type": "text", "text": "session B"}]),
        model="model",
        provider="custom",
        api_mode="chat_completions",
        base_url="http://x1-370:8080/v1",
    )

    assert first.source == "decorated-static"
    assert first.static_prefix_hash == second.static_prefix_hash
    assert first.system_hash != second.system_hash
    assert first.checkpoint_id == second.checkpoint_id


def test_runtime_static_prefix_override_supports_local_plain_system_messages():
    first = build_manifest(
        _request("stable prefix\n\nworkspace A\n\nsession A"),
        stable_system_prefix="stable prefix",
        model="model",
        provider="custom",
        api_mode="chat_completions",
        base_url="http://x1-370:8080/v1",
    )
    second = build_manifest(
        _request("stable prefix\n\nworkspace B\n\nsession B"),
        stable_system_prefix="stable prefix",
        model="model",
        provider="custom",
        api_mode="chat_completions",
        base_url="http://x1-370:8080/v1",
    )

    assert first.source == "runtime-static"
    assert first.static_prefix_hash == second.static_prefix_hash
    assert first.system_hash != second.system_hash
    assert first.checkpoint_id == second.checkpoint_id


def test_headers_contain_identifiers_not_prompt_text():
    manifest = build_manifest(
        _request("TOP SECRET PROMPT"),
        session_id="session-1",
        model="model",
        provider="lmstudio",
        api_mode="chat_completions",
        base_url="http://localhost:1234/v1",
    )

    headers = build_cache_headers(manifest)
    serialized = str(headers)
    assert "TOP SECRET PROMPT" not in serialized
    assert headers["X-Hermes-Cache-Checkpoint-Id"] == manifest.checkpoint_id
    assert headers["X-Hermes-Cache-Session-Id"] == "session-1"


def test_endpoint_eligibility_is_host_based_and_private_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_CACHE_ALLOW_REMOTE", raising=False)

    assert is_cache_eligible_endpoint("http://localhost:8080/v1")
    assert is_cache_eligible_endpoint("http://100.64.43.123:8080/v1")
    assert is_cache_eligible_endpoint("http://x1-370.lan:8080/v1")
    assert is_cache_eligible_endpoint("http://x1-370.kipnerter.ts.net:8080/v1")
    assert not is_cache_eligible_endpoint("https://api.example.com/v1")
    assert not is_cache_eligible_endpoint(
        "https://api.example.com/v1",
        provider="lmstudio",
    )


def test_route_scoring_prefers_affinity_over_small_latency_difference():
    sticky = CacheRouteCandidate(
        endpoint="http://slow:8080",
        session_affinity=True,
        checkpoint_present=True,
        model_loaded=True,
        latency_ms=80,
    )
    cold = CacheRouteCandidate(
        endpoint="http://fast:8080",
        checkpoint_present=False,
        model_loaded=True,
        prefill_tokens_per_second=100,
        estimated_prefix_tokens=20_000,
        latency_ms=5,
    )

    assert route_score(sticky) < route_score(cold)
    assert select_best_route([cold, sticky]) == sticky


def test_state_store_persists_hashes_and_metrics_without_prompt_text(tmp_path):
    database = tmp_path / "cache.db"
    state = CacheStateStore(database)
    manifest = build_manifest(
        _request("do not persist this prompt"),
        session_id="session-1",
        model="model",
        provider="lmstudio",
        api_mode="chat_completions",
        base_url="http://localhost:1234/v1",
    )

    state.bind_session(manifest)
    state.register_checkpoint(manifest, state="hit", prefix_tokens=123)
    state.record_request(
        api_request_id="request-1",
        manifest=manifest,
        duration_ms=25,
        success=True,
        prompt_tokens=500,
        completion_tokens=20,
        cache_read_tokens=123,
    )

    affinity = state.affinity("session-1")
    assert affinity is not None
    assert affinity["checkpoint_id"] == manifest.checkpoint_id
    assert state.summary()["cache_read_tokens"] == 123
    assert state.checkpoints()[0]["state"] == "hit"
    assert state.recent_requests(session_id="session-1")[0]["success"] == 1
    assert b"do not persist this prompt" not in database.read_bytes()
