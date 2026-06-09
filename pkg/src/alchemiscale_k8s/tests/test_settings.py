"""Tests for K8SManagerSettings.

"""

import warnings

import pytest

from alchemiscale_k8s.settings import K8SManagerSettings


def _base_kwargs(**overrides):
    defaults = dict(
        name="testmgr",
        logfile=None,
        max_compute_services=2,
        job_spec_path="/tmp/job-spec.yaml",
        namespace="alchemiscale",
    )
    defaults.update(overrides)
    return defaults


def test_max_submit_per_cycle_default_inherited():
    """Without specifying anything, we get the upstream default of 1."""
    settings = K8SManagerSettings(**_base_kwargs())
    assert settings.max_submit_per_cycle == 1


def test_max_submit_per_cycle_explicit():
    settings = K8SManagerSettings(**_base_kwargs(max_submit_per_cycle=4))
    assert settings.max_submit_per_cycle == 4


def test_job_creation_rate_migrated_with_warning():
    """Old configs that set ``job_creation_rate`` continue to work but warn."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        settings = K8SManagerSettings(**_base_kwargs(job_creation_rate=3))

    assert settings.max_submit_per_cycle == 3
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert deprecations, "expected a DeprecationWarning when using job_creation_rate"
    assert "job_creation_rate" in str(deprecations[0].message)


def test_job_creation_rate_ignored_when_new_name_also_set():
    """If a user sets both, the new name wins and the old one is warned about."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        settings = K8SManagerSettings(
            **_base_kwargs(max_submit_per_cycle=5, job_creation_rate=99)
        )

    assert settings.max_submit_per_cycle == 5
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert deprecations
    assert (
        "ignored" in str(deprecations[0].message).lower()
        or "precedence" in str(deprecations[0].message).lower()
    )


def test_job_creation_rate_no_longer_a_field():
    """The deprecated alias is migrated at validation time, not stored as a field."""
    fields = set(K8SManagerSettings.model_fields)
    assert "job_creation_rate" not in fields
    assert "max_submit_per_cycle" in fields  # inherited from ComputeManagerSettings
