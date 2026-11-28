"""Regression coverage for cron skill-identifier normalization (commit 557f900fd).

Legacy cron jobs could store a JSON-encoded array inside the string ``skill``
field (``'["portfolio-ops"]'``).  ``_canonical_skills`` must decode those into
real skill names while leaving plain names untouched, producing the same
canonical list regardless of which legacy shape supplied them.  Malformed
input must not be decoded into junk, and wrong-typed input must fail closed
at the tool boundary instead of creating a broken job.

All tests are hermetic: cron storage is redirected to ``tmp_path`` and no
network, model, or gateway calls are made.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tools.cronjob_tools import _canonical_skills, _format_job, cronjob  # noqa: E402


@pytest.fixture
def isolated_cron(tmp_path, monkeypatch):
    """Redirect cron storage to a temp HERMES_HOME for tool-level checks."""
    hermes_home = tmp_path / ".hermes"
    cron_dir = hermes_home / "cron"
    (cron_dir / "output").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLES_DIR", str(hermes_home / "skill-bundles"))
    import cron.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", cron_dir / "output")
    # No scheduler notifications from a test create/update.
    import tools.cronjob_tools as cron_tools
    monkeypatch.setattr(cron_tools, "_notify_provider_jobs_changed_safe", lambda: None)
    return cron_dir


class TestValidEncodingsAllowed:
    """Every encoding seen in the wild must resolve to real skill names."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("portfolio-ops", ["portfolio-ops"]),
            ("  portfolio-ops  ", ["portfolio-ops"]),
            ('["portfolio-ops"]', ["portfolio-ops"]),
            ('  [ "portfolio-ops" ]  ', ["portfolio-ops"]),
            ('["alpha", "beta"]', ["alpha", "beta"]),
            ("[]", []),
        ],
    )
    def test_skill_string_encodings(self, raw, expected):
        assert _canonical_skills(skill=raw) == expected

    def test_json_array_string_inside_skills_list(self):
        assert _canonical_skills(skills=['["alpha", "beta"]', "gamma"]) == [
            "alpha",
            "beta",
            "gamma",
        ]

    def test_tuple_of_legacy_values(self):
        assert _canonical_skills(skills=('["alpha"]', "beta")) == ["alpha", "beta"]

    def test_legacy_skill_and_skills_deduplicate_in_order(self):
        # An explicit ``skills`` value wins over the legacy singular field;
        # both are still decoded and deduplicated within themselves.
        assert _canonical_skills('["alpha", "beta"]', ['["beta"]', "gamma"]) == [
            "beta",
            "gamma",
        ]

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"skill": ""},
            {"skill": "   "},
            {"skills": []},
            {"skills": ["", None, "   "]},
        ],
    )
    def test_empty_inputs_yield_no_skills(self, kwargs):
        assert _canonical_skills(**kwargs) == []


class TestNormalizationEquivalence:
    """Legacy shapes must collapse to one canonical representation."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"skill": "alpha"},
            {"skill": '["alpha"]'},
            {"skills": ["alpha"]},
            {"skills": '["alpha"]'},
            {"skills": ('["alpha"]',)},
            {"skill": "alpha", "skills": ['["alpha"]']},
        ],
    )
    def test_equivalent_shapes_produce_same_list(self, kwargs):
        assert _canonical_skills(**kwargs) == ["alpha"]

    def test_normalization_is_idempotent(self):
        once = _canonical_skills('["alpha", "beta"]')
        assert _canonical_skills(skills=once) == once == ["alpha", "beta"]

    def test_format_job_normalizes_stored_legacy_job(self):
        formatted = _format_job(
            {"id": "job-1", "skill": '["portfolio-ops"]', "skills": ['["portfolio-ops"]']}
        )
        assert formatted["skill"] == "portfolio-ops"
        assert formatted["skills"] == ["portfolio-ops"]
        assert formatted["name"] == "portfolio-ops"

    def test_cronjob_create_persists_canonical_skills(self, isolated_cron):
        result = json.loads(
            cronjob(
                action="create",
                schedule="every 1h",
                name="legacy skill job",
                skill='["portfolio-ops"]',
            )
        )
        assert result["success"] is True
        assert result["skills"] == ["portfolio-ops"]
        assert result["job"]["skill"] == "portfolio-ops"

        stored = json.loads(isolated_cron.joinpath("jobs.json").read_text(encoding="utf-8"))
        assert [job["skills"] for job in stored["jobs"]] == [["portfolio-ops"]]
        listed = json.loads(cronjob(action="list"))
        assert listed["jobs"][0]["skills"] == ["portfolio-ops"]

    def test_cronjob_update_normalizes_legacy_encodings(self, isolated_cron):
        created = json.loads(
            cronjob(action="create", schedule="every 1h", skill="original")
        )
        job_id = created["job_id"]
        updated = json.loads(
            cronjob(
                action="update",
                job_id=job_id,
                skill='["replacement"]',
                skills=['["replacement"]', "extra"],
            )
        )
        assert updated["success"] is True
        assert updated["job"]["skills"] == ["replacement", "extra"]
        assert updated["job"]["skill"] == "replacement"

        stored = json.loads(isolated_cron.joinpath("jobs.json").read_text(encoding="utf-8"))
        assert stored["jobs"][0]["skills"] == ["replacement", "extra"]


class TestMalformedAndWrongTypedFailClosed:
    """Malformed JSON stays literal; unusable containers never make a job."""

    @pytest.mark.parametrize(
        "raw",
        [
            "[not-json]",
            '["alpha", ]',  # trailing comma: json.loads rejects it
            '["alpha"',  # unterminated array
            "[alpha]",  # bare words are not JSON strings
        ],
    )
    def test_malformed_json_string_is_kept_literal(self, raw):
        assert _canonical_skills(skill=raw) == [raw]

    @pytest.mark.parametrize(
        "raw",
        [
            '{"skill": "alpha"}',  # JSON object is not an encoded skill list
            '"alpha"',  # JSON scalar string is not an encoded skill list
            "123",
        ],
    )
    def test_non_array_json_is_not_decoded(self, raw):
        assert _canonical_skills(skill=raw) == [raw]

    @pytest.mark.parametrize("bad", [5, 3.5, object()])
    def test_wrong_typed_skills_container_raises(self, bad):
        with pytest.raises(TypeError):
            _canonical_skills(skills=bad)

    def test_cronjob_create_rejects_wrong_typed_skills_without_writing(self, isolated_cron):
        result = json.loads(
            cronjob(action="create", schedule="every 1h", prompt="hello", skills=5)
        )
        assert result["success"] is False
        assert result["error"]
        assert not isolated_cron.joinpath("jobs.json").exists()

    def test_cronjob_update_rejects_wrong_typed_skills(self, isolated_cron):
        created = json.loads(
            cronjob(action="create", schedule="every 1h", skill="original")
        )
        result = json.loads(
            cronjob(action="update", job_id=created["job_id"], skills=object())
        )
        assert result["success"] is False
        stored = json.loads(isolated_cron.joinpath("jobs.json").read_text(encoding="utf-8"))
        assert stored["jobs"][0]["skills"] == ["original"]
