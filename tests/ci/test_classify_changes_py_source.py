"""Regression: Python source under a skipped prefix must not skip the Python lane.

The classifier's ``python`` lane is a fail-open denylist: it may run when not
needed, but must never skip a change that could break Python. A ``.py`` file
under ``website/``, ``docs/``, ``apps/``, ``ui-tui/``, ``web/`` or ``docker/``
is executable code, not prose — this repo ships real Python in
``website/scripts/``. Before the fix, those files were classified as
Python-irrelevant and the pytest/ruff/footguns jobs were skipped.
"""

from __future__ import annotations

from tests.ci.test_classify_changes import _lanes, classify


# Real files in this repo plus defensive siblings; all are Python source,
# none is prose or a frontend-only package.
PY_SOURCE_UNDER_SKIP_PREFIX = [
    "website/scripts/generate-skill-docs.py",  # real file
    "website/scripts/extract-skills.py",       # real file
    "website/scripts/generate-llms-txt.py",    # real file
    "docs/tools/build_index.py",
    "apps/desktop/scripts/gen.py",
    "ui-tui/scripts/gen.py",
    "web/scripts/gen.py",
    "docker/gen_certs.py",
    "website/scripts/hooks.pyi",
    "website/scripts/setup.pth",
]

PROSE_OR_FRONTEND = [
    "README.md",
    "docs/guide.md",
    "website/docs/intro.md",
    "apps/desktop/src/app.tsx",
    "ui-tui/src/entry.ts",
    "web/src/lib/x.ts",
    "package-lock.json",
    "Dockerfile",
]


def test_source_file_under_skipped_prefix_still_runs_python():
    for path in PY_SOURCE_UNDER_SKIP_PREFIX:
        assert classify([path])["python"] is True, f"{path} is Python source"


def test_prose_and_frontend_only_paths_still_skip_python():
    for path in PROSE_OR_FRONTEND:
        assert classify([path])["python"] is False, f"{path} is not Python source"


def test_other_lanes_unchanged_for_skipped_prefix_source():
    assert classify(["website/scripts/generate-skill-docs.py"]) == _lanes(
        python=True, site=True, scan=True
    )
    assert classify(["apps/desktop/scripts/gen.py"]) == _lanes(
        python=True, frontend=True, scan=True
    )
    assert classify(["docker/gen_certs.py"]) == _lanes(
        python=True, docker_meta=True, scan=True
    )


def test_mixed_prose_and_skipped_prefix_source_runs_python():
    assert classify(["README.md", "website/scripts/extract-skills.py"]) == _lanes(
        python=True, site=True, scan=True
    )
