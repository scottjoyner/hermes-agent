# DELIVERY_RESULT — Autonomous Fleet Delivery 3/5 (Hermes-SensorGraft trace and receipts)

STATUS: PASS

Scope note: PASS covers scoped research validation only, in this isolated
worktree, with synthetic data. No claim of production readiness. No live
services, configs, credentials, traces, or provider policies were touched.
No network calls, no deployment, no merge/push/PR, no parallel agents.

TESTS:
- HERMES_PYTHON=/usr/bin/python3 scripts/run_tests.sh tests/agent/test_trace_receipts.py -q
  - Result (final): 1 file, 26 tests passed, 0 failed (0.6s, per-file subprocess runner)
  - Environment: no local .venv with pytest exists in this worktree; runner
    was pointed at the system interpreter (/usr/bin/python3, Python 3.12.3,
    pytest 9.1.1). No packages were installed.
  - Earlier iteration of the same run: 1 test failed
    (test_corrupt_line_fails_closed_on_append) because the journal cached
    state across records; fixed by re-verifying the journal from disk on
    every record. Full re-run: 26/26 green.

What was delivered
- `agent/trace_receipts.py` (new): append-only, hash-chained JSONL journal
  for trace/receipt events.
  - Source identity: every record requires an explicit `source` matching
    `^[A-Za-z][A-Za-z0-9_.-]{0,63}$`; missing/invalid → TraceReceiptError,
    nothing written (fail-closed).
  - Sequence: monotonic per-source sequence, rebuilt by re-verifying the
    on-disk journal before each append (survives restart; detects external
    corruption).
  - Fail-closed provenance/integrity: per-record `payload_sha256`,
    `prev_receipt_id` hash chain, and `receipt_id` recomputed on read;
    `verify_journal()` rejects tampering, truncation, schema drift, and
    sequence gaps. Corrupt journal → append fails closed.
  - Payload safety: payload must be a JSON object ≤ 64 KiB, be
    JSON-serializable, and contain no credential-looking key anywhere in
    the tree (recursive, separator-normalized match on e.g. authorization,
    password, secret, token, api_key, credential, private_key, cookie) —
    rejected wholesale rather than partially recorded.
  - Journal file cap 16 MiB; created/chmod 0600; fsync per append.
- `tests/agent/test_trace_receipts.py` (new): 26 synthetic tests covering
  identity validation, per-source sequencing, restart persistence, tamper /
  truncation / corruption / schema-drift detection, credential-like key
  rejection, size bounds, permissions, and empty-journal verify. No
  network, no real credentials, tmp_path only.

Source evidence
- Existing durable-delivery pattern reused as reference: `gateway/delivery_ledger.py`
  (best-effort, bounded, honest ambiguity) — this work does NOT modify it.
- Observer contract referenced for payload-safety stance: `docs/observability/README.md`
  (sanitized payloads, redaction posture). No observer hook behavior changed.
- No SensorGraft/trace-receipt implementation existed in this worktree
  (`grep -ri sensorgraft|graft` → unrelated matches only), so a new,
  deliberately unwired primitive was the smallest safe change.

Deliberately NOT done (scope boundary)
- No wiring into agent loop, gateway, observer hooks, CLI, or plugins.
  There is no SensorGraft consumer contract in this worktree, and silent
  production-path integration would be speculative and risky. Integration
  needs a reviewed design with rollback.
- No config keys, env vars, migrations, state.db writes, or backfill.
- No changes to FLEET_DELIVERY_BRIEF.md semantics; no production NO-GO
  gates were touched or weakened.

Changed files (commit f5f8373aac, branch research/opencode-hermes-sg-20261104)
- agent/trace_receipts.py
- tests/agent/test_trace_receipts.py
(FLEET_DELIVERY_BRIEF.md and this file are left untracked as fleet artifacts.)

Remaining blockers / open items
- No approved SensorGraft consumer contract (event kinds, field mapping,
  retention, export destination, failure policy) exists in-repo; delivery
  of an end-to-end integration is blocked on that spec or on explicit
  maintainer direction.
- System-python test run is not CI parity; a proper `.venv` with dev
  extras should re-run the file in CI before merge (CI runs everything on
  main anyway).
- Per-record full-file re-verification is O(journal size); fine at the
  16 MiB cap and low event rates, but a tail-check optimization would be
  needed if this is ever hot-path.

Safe PR proposal (not created — no push/PR per brief)
- Title: "trace: add fail-closed trace/receipt journal (unwired primitive)"
- Body: adds agent/trace_receipts.py + synthetic tests; documents that the
  journal is not called by any production path; lists non-goals (no
  network, no backfill, no config surface); links this result file;
  notes CI should re-run on merge.
- Follow-ups (separate PRs, each gated on maintainer sign-off):
  (1) SensorGraft consumer spec (event kinds, retention, export target,
      failure semantics, opt-in config gate);
  (2) wire behind an explicit config gate default OFF, with gateway
      observer hook emitting receipts and fail-open/fail-closed policy
      decided in review;
  (3) retention/prune policy aligned with delivery_ledger bounds.

Verification of non-impact
- `git status` after commit: only FLEET_DELIVERY_BRIEF.md and this file
  untracked; no other worktrees or files modified.
- No secrets, tokens, real traces, or production data appear in the diff.

## Fleet Commander continuation — November 6, 2026

STATUS: PASS (research tests only; production HOLD)
TESTS: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/agent/test_trace_receipts.py` on x1-370: **32 passed, 0 failed** in 0.43 seconds.

- Source HEAD verified as `e04d716bc7` (`fix(trace): serialize receipt appends across processes`). No production Hermes tracing hooks enabled and no real recordings or provider traffic accessed.
- This confirms the focused local test suite, not independent crash-recovery custody, an off-host signed witness or SensorGraft ingestion acceptance.
- Do not deploy, federate, purge or enable journals without an approved consumer schema, independent anti-rollback custody and separate production admission.

## November 6 continuation — independent synthetic CI gate and honest rollback boundary

STATUS: PASS (offline unit tests; production integration HOLD)
TESTS: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/agent/test_trace_receipts.py` -> **33 passed, 0 failed** on x1-370. New `.github/workflows/trace-receipts-research.yml` parsed with PyYAML and passed local structural permission/path checks.

- Added a narrowly scoped pull-request/workflow-dispatch action: read-only repository permissions, non-persistent checkout credentials, pinned checkout/setup-python action SHAs, Python 3.11, pytest 9.0.2, and the focused receipt test file only. No production deployment, external credentials, or network/model traffic in tests.
- Reworded journal primitive documentation: local unkeyed hash chains detect broken interior links and payload tampering but **cannot** detect deletion of a valid trailing suffix, or complete rewrite/re-hashing, without a separately pinned trusted head. Added a synthetic regression proving a retained coherent prefix remains locally verifiable after tail deletion.
- Do not infer external anti-rollback, signed custody, per-host chain completeness or approval to wire the journal from a passing suite. Next gate is actual hosted CI at the exact revised PR head plus an independently reviewed consumer/custody spec.
