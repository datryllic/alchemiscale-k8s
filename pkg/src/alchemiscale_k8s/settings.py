import warnings
from pathlib import Path

from alchemiscale.compute.settings import ComputeManagerSettings
from pydantic import Field, model_validator


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
