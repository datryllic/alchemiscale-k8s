import re
import warnings
from pathlib import Path

from alchemiscale.compute.settings import ComputeManagerSettings
from pydantic import Field, field_validator, model_validator

# Length of the suffix appended to the manager name to form a k8s Job name:
# ``"." + uuid4().hex`` (see ``K8SManager._new_job``).
_JOB_NAME_SUFFIX_LENGTH = 1 + 32

# Kubernetes copies the Job name into the ``job-name`` pod label, and label
# values are limited to 63 characters, so the full Job name must fit in 63.
_K8S_LABEL_VALUE_MAX_LENGTH = 63

MAX_MANAGER_NAME_LENGTH = _K8S_LABEL_VALUE_MAX_LENGTH - _JOB_NAME_SUFFIX_LENGTH

# Intersection of:
# - Kubernetes DNS-1123 subdomain rules for Job names (lowercase
#   alphanumerics, '-' and '.', each '.'-separated part starting and ending
#   with an alphanumeric);
# - alchemiscale's ``ComputeIDBase`` name rules (``^[a-zA-Z][a-zA-Z0-9_.:]*$``),
#   which apply both to the ``ComputeManagerID`` derived from the name and to
#   the ``ComputeServiceID`` derived from the Job name; this excludes '-'.
_MANAGER_NAME_PATTERN = re.compile(r"[a-z][a-z0-9]*(\.[a-z0-9]+)*")


class K8SManagerSettings(ComputeManagerSettings):
    job_spec_path: Path = Field(
        ..., description="File containing job container and volume definitions."
    )
    namespace: str = Field(
        ..., description="Namespace on the target k8s cluster to submit jobs to."
    )
    k8s_max_retries: int = Field(
        5,
        description=(
            "Maximum number of times to retry a request to k8s. "
            "In the case k8s is unresponsive an expoenential backoff "
            "is applied with retries until this number is reached. "
            "If set to -1, retries will continue indefinitely until success."
        ),
    )
    k8s_retry_base_seconds: float = Field(
        2.0,
        description=(
            "The base number of seconds to use for exponential backoff to k8s. "
            "Must be greater than 1.0.",
        ),
    )
    k8s_retry_max_seconds: float = Field(
        60.0,
        description=(
            "Maximum number of seconds to sleep between retries to k8s; "
            "avoids runaway exponential backoff while allowing for many retries."
        ),
    )
    max_consecutive_failed_cycles: int | None = Field(
        5,
        ge=0,
        description=(
            "Failed Jobs are logged and deleted whenever the manager creates "
            "compute services; stop with JobFailureError if they are found in "
            "more than this many consecutive such cycles. 0 stops on the first "
            "failed Job; null never stops."
        ),
    )

    @field_validator("name")
    @classmethod
    def _validate_name_for_k8s(cls, value: str) -> str:
        """Ensure ``name`` can be used to build k8s Job names and label values.

        The manager names each Job ``{name}.{uuid4().hex}``. That string must
        be a valid DNS-1123 subdomain, and because Kubernetes copies it into
        the ``job-name`` pod label it must also be at most 63 characters long.
        The same string is used as the name part of each ``ComputeServiceID``,
        so it must also satisfy alchemiscale's ID rules, which forbid '-'.
        """
        if len(value) > MAX_MANAGER_NAME_LENGTH:
            raise ValueError(
                f"name {value!r} is {len(value)} characters long; it must be at "
                f"most {MAX_MANAGER_NAME_LENGTH}. Kubernetes Job names are built "
                f"as '{{name}}.{{32-character hex uuid}}', and Kubernetes copies "
                f"the Job name into the 'job-name' pod label, whose value is "
                f"limited to {_K8S_LABEL_VALUE_MAX_LENGTH} characters."
            )
        if not _MANAGER_NAME_PATTERN.fullmatch(value):
            raise ValueError(
                f"name {value!r} is not valid for a Kubernetes compute manager. "
                "It must start with a lowercase letter and contain only "
                "lowercase letters, digits, and periods ('.'), with no leading, "
                "trailing, or consecutive periods (e.g. 'nrp.omsf'). "
                "Kubernetes Job names allow only lowercase alphanumerics, '-' "
                "and '.', and alchemiscale compute IDs additionally forbid '-' "
                "and require a leading letter."
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def _migrate_job_creation_rate(cls, data):
        """Backward-compat: ``job_creation_rate`` was the old name for what is
        now ``max_submit_per_cycle`` on the upstream
        :class:`alchemiscale.compute.settings.ComputeManagerSettings`.

        Existing configs that still set ``job_creation_rate`` continue to
        work; the value is migrated and a :class:`DeprecationWarning` is
        emitted. Configs that set both ``job_creation_rate`` and
        ``max_submit_per_cycle`` use the new name (the old name is ignored
        with a warning).
        """
        if not isinstance(data, dict):
            return data
        if "job_creation_rate" not in data:
            return data

        old_value = data.pop("job_creation_rate")
        if "max_submit_per_cycle" in data:
            warnings.warn(
                "K8SManagerSettings.job_creation_rate is deprecated and "
                "ignored when max_submit_per_cycle is also set; "
                "max_submit_per_cycle takes precedence.",
                DeprecationWarning,
                stacklevel=2,
            )
        elif old_value is not None:
            warnings.warn(
                "K8SManagerSettings.job_creation_rate is deprecated; use "
                "max_submit_per_cycle instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            data["max_submit_per_cycle"] = old_value
        return data
