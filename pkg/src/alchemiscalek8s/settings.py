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
