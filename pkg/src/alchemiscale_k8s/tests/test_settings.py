"""Tests for K8SManagerSettings.

"""

import re
import warnings
from uuid import uuid4

import pytest
from alchemiscale.storage.models import ComputeManagerID, ComputeServiceID
from pydantic import ValidationError

from alchemiscale_k8s.settings import MAX_MANAGER_NAME_LENGTH, K8SManagerSettings


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


# --- name validation (issue #14) ---

# Kubernetes DNS-1123 subdomain regex, as used to validate Job names.
_DNS1123_SUBDOMAIN = re.compile(
    r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*"
)


def test_max_manager_name_length_is_30():
    assert MAX_MANAGER_NAME_LENGTH == 30


@pytest.mark.parametrize(
    "name",
    [
        "testmgr",
        "nrp.omsf",
        "a",
        "mgr1",
        "a.1.b2",
        "a" * MAX_MANAGER_NAME_LENGTH,
    ],
)
def test_valid_names_accepted(name):
    settings = K8SManagerSettings(**_base_kwargs(name=name))
    assert settings.name == name

    # names that pass must yield Job names Kubernetes and alchemiscale accept
    jobname = f"{name}.{uuid4().hex}"
    assert len(jobname) <= 63
    assert _DNS1123_SUBDOMAIN.fullmatch(jobname)
    ComputeManagerID.new_from_name(name)
    ComputeServiceID.new_from_name(jobname)


@pytest.mark.parametrize(
    "name",
    [
        "test_mgr",  # underscore: rejected by Kubernetes
        "test:mgr",  # colon: rejected by Kubernetes
        "test-mgr",  # hyphen: rejected by alchemiscale compute IDs
        "TestMgr",  # uppercase: rejected by Kubernetes; not silently lowercased
        "1mgr",  # leading digit: rejected by alchemiscale compute IDs
        ".mgr",
        "mgr.",
        "a..b",
        "test mgr",
        "",
    ],
)
def test_invalid_names_rejected(name):
    with pytest.raises(ValidationError, match="lowercase"):
        K8SManagerSettings(**_base_kwargs(name=name))


def test_name_too_long_rejected():
    name = "a" * (MAX_MANAGER_NAME_LENGTH + 1)
    with pytest.raises(ValidationError, match="job-name") as excinfo:
        K8SManagerSettings(**_base_kwargs(name=name))
    assert "at most 30" in str(excinfo.value)
