"""Regression tests for legacy cron skill identifiers.

Older jobs can store a JSON-encoded array inside a string field, e.g.
'["portfolio-ops"]'. It must resolve to the skill name, not to a literal
skill called '["portfolio-ops"]' (which never exists and warns every run).
"""
from tools.cronjob_tools import _canonical_skills


def test_plain_string_is_unchanged():
    assert _canonical_skills("portfolio-ops") == ["portfolio-ops"]


def test_json_array_string_is_decoded():
    assert _canonical_skills('["portfolio-ops"]') == ["portfolio-ops"]


def test_json_array_in_skills_list_is_decoded():
    assert _canonical_skills(skills=['["portfolio-ops"]', "other"]) == [
        "portfolio-ops", "other"
    ]


def test_legacy_skill_and_skills_both_normalize():
    assert _canonical_skills('["portfolio-ops"]', ['["portfolio-ops"]']) == [
        "portfolio-ops"
    ]


def test_invalid_json_string_is_kept_literal():
    assert _canonical_skills("[not-json]") == ["[not-json]"]
