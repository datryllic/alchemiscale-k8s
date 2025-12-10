from alchemiscale.compute.settings import ComputeManagerSettings
from pathlib import Path
from pydantic import Field


class K8SManagerSettings(ComputeManagerSettings):
    job_spec_path: Path = Field(
        ..., description="File containing job container and volume definitions."
    )
    namespace: str = Field(
        ..., description="Namespace on the target k8s cluster to submit jobs to."
    )
    job_creation_rate: int = Field(
        ..., description="Max number of jobs to create per cycle."
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
        description=("The base number of seconds to use for exponential backoff to k8s. "
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
