"""Local embeddings client for the knowledge-graph memory provider.

Talks to any OpenAI-compatible ``/v1/embeddings`` endpoint — the primary
target is a locally-running LM Studio node (or Ollama) so the "second
brain" stays private and offline. The base URL, model name, and optional
API key are all configurable (config.yaml ``knowledge_graph.embeddings`` or
the ``LMSTUDIO_EMBEDDINGS_*`` env vars).

Embeddings are cached by content hash so repeated capture of the same text
(identical tool results, session re-ingest) never re-hits the model.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://localhost:1234/v1"
_DEFAULT_MODEL = "nomic-embed-text"


class LocalEmbeddingClient:
    """OpenAI-compatible embeddings client with an in-process LRU-ish cache."""

    def __init__(
        self,
        base_url: str = "",
        model: str = "",
        api_key: str = "",
        timeout: float = 30.0,
        batch_size: int = 32,
        registry_path: str = "",
    ) -> None:
        self.base_url = (base_url or os.environ.get("LMSTUDIO_EMBEDDINGS_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("LMSTUDIO_EMBEDDINGS_MODEL") or _DEFAULT_MODEL
        self.api_key = api_key or os.environ.get("LMSTUDIO_EMBEDDINGS_API_KEY") or ""
        self.registry_path = registry_path or os.environ.get("EMBEDDING_REGISTRY") or ""
        self.timeout = timeout
        self.batch_size = max(1, int(batch_size))
        self._dimension: Optional[int] = None
        self._cache: Dict[str, List[float]] = {}
        self._cache_lock = threading.Lock()
        self._clients: Dict[str, Any] = {}
        self._active_base_url = self.base_url

    # -- client ---------------------------------------------------------------

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        value = (url or "").rstrip("/")
        if value.endswith("/embeddings"):
            value = value[: -len("/embeddings")]
        return value

    def _candidate_base_urls(self) -> List[str]:
        urls = [self._active_base_url, self.base_url]
        if self.registry_path:
            try:
                data = json.loads(Path(self.registry_path).read_text(encoding="utf-8"))
                endpoints = sorted(data.get("endpoints") or [], key=lambda item: item.get("priority", 9999))
                urls.extend(str(item.get("url") or "") for item in endpoints)
            except Exception as exc:
                logger.warning("Embedding registry unavailable (%s): %s", self.registry_path, exc)
        normalized: List[str] = []
        for url in urls:
            candidate = self._normalize_base_url(url)
            if candidate and candidate not in normalized:
                normalized.append(candidate)
        return normalized

    def _get_client(self, base_url: str):
        client = self._clients.get(base_url)
        if client is None:
            from openai import OpenAI

            kwargs = {"base_url": base_url, "timeout": self.timeout}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            else:
                # LM Studio / local endpoints typically need a non-empty key.
                kwargs["api_key"] = "not-needed"
            client = OpenAI(**kwargs)
            self._clients[base_url] = client
        return client

    def _create_embeddings(self, inputs):
        errors = []
        for base_url in self._candidate_base_urls():
            try:
                response = self._get_client(base_url).embeddings.create(model=self.model, input=inputs)
                self._active_base_url = base_url
                return response
            except Exception as exc:
                errors.append(f"{base_url}: {exc}")
        raise RuntimeError("all embedding endpoints failed: " + "; ".join(errors))

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()

    @property
    def dimension(self) -> Optional[int]:
        return self._dimension

    def _embed_one(self, text: str) -> Optional[List[float]]:
        text = (text or "").strip()
        if not text:
            return None
        h = self._hash(text)
        with self._cache_lock:
            cached = self._cache.get(h)
        if cached is not None:
            return cached
        try:
            resp = self._create_embeddings(text)
            vec = list(resp.data[0].embedding)
        except Exception as exc:
            logger.warning("Local embedding failed for %d-char text: %s", len(text), exc)
            return None
        if self._dimension is None and vec:
            self._dimension = len(vec)
        with self._cache_lock:
            self._cache[h] = vec
        return vec

    # -- public API -----------------------------------------------------------

    def embed(self, text: str) -> Optional[List[float]]:
        """Embed a single string. Returns None if embedding is unavailable."""
        return self._embed_one(text)

    def embed_many(self, texts: List[str]) -> List[Optional[List[float]]]:
        """Embed many strings in batches. Missing/empty entries yield None."""
        results: List[Optional[List[float]]] = []
        # Serve cache hits first without touching the model.
        to_embed: List[int] = []
        for i, t in enumerate(texts):
            h = self._hash((t or "").strip())
            with self._cache_lock:
                hit = self._cache.get(h) if (t or "").strip() else None
            if hit is not None:
                results.append(hit)
            else:
                results.append(None)
                to_embed.append(i)

        if not to_embed:
            return results

        for start in range(0, len(to_embed), self.batch_size):
            batch_idx = to_embed[start : start + self.batch_size]
            batch_texts = [(texts[i] or "").strip() for i in batch_idx]
            # Retry transient local-endpoint failures (e.g. LM Studio
            # unloading the model mid-queue) with a short backoff.
            for attempt in range(3):
                try:
                    resp = self._create_embeddings(batch_texts)
                    for j, i in enumerate(batch_idx):
                        vec = list(resp.data[j].embedding)
                        if self._dimension is None and vec:
                            self._dimension = len(vec)
                        with self._cache_lock:
                            self._cache[self._hash(batch_texts[j])] = vec
                        results[i] = vec
                    break
                except Exception as exc:
                    if attempt == 2:
                        logger.warning("Local embedding batch failed: %s", exc)
                    else:
                        time.sleep(0.5 * (attempt + 1))
        return results

    def available(self) -> bool:
        """Best-effort reachability probe (single tiny embedding)."""
        try:
            return self.embed("ping") is not None
        except Exception:
            return False
