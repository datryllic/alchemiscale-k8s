from uuid import uuid4
from time import sleep

from alchemiscale.compute.manager import (
    ComputeManager,
    ComputeManagerSettings,
    ComputeServiceSettings,
)

from kubernetes import client, config


class K8SManager(ComputeManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.job_registry: list[str] = []
        config.load_kube_config()
        self.batch_api = client.BatchV1Api()

    def create_compute_services(self, data):
        self.create_compute_service()

    def create_compute_service(self):
        self.submit_job(self.new_job())

    def new_job(self):
        compute_version = "0.1.3-3"
        container = client.V1Container(
            name="alchemiscale-synchronous-container",
            image=f"ghcr.io/openforcefield/alchemiscale-compute:{compute_version}",
            args=[
                "compute",
                "synchronous",
                "-c",
                "/mnt/settings/synchronous-compute-settings.yaml",
            ],
            resources=client.V1ResourceRequirements(
                limits={
                    "cpu": "2",
                    "memory": "12Gi",
                    "ephemeral-storage": "48Gi",
                    "nvidia.com/gpu": "1",
                },
                requests={
                    "cpu": "2",
                    "memory": "12Gi",
                    "ephemeral-storage": "48Gi",
                },
            ),
            restart_policy="Never",
            volume_mounts=[
                client.V1VolumeMount(
                    name="alchemiscale-compute-settings-yaml",
                    mount_path="/mnt/settings",
                    read_only=True,
                ),
            ],
            env=[client.V1EnvVar(name="OPENMM_CPU_THREADS", value="4")],
        )

        volume = client.V1Volume(
            name="alchemiscale-compute-settings-yaml",
            secret=client.V1SecretVolumeSource(
                secretName="alchemiscale-compute-settings-yaml",
            ),
        )
        template = client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(
                labels={"app": "alchemiscale-synchronouscompute"}
            ),
            spec=client.V1PodSpec(
                restart_policy="Never", containers=[container], volumes=[volume]
            ),
        )
        spec = client.V1JobSpec(
            template=template,
            backoff_limit=0,
            ttl_seconds_after_finished=5,
            selector=client.V1LabelSelector(
                match_labels={"app": "alchemiscale-synchronouscompute"}
            ),
        )
        job_id = str(int(uuid4()))
        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(
                name=f"testjob{job_id}",
                namespace="alchemiscale",
                labels={"app": "alchemiscale-synchronouscompute"},
            ),
            spec=spec,
        )
        return job

    def submit_job(self, job):
        jobname = job.metadata.name
        # TODO: handle exceptions
        self.batch_api.create_namespaced_job(namespace="default", body=job)
        self.job_registry.append(jobname)
