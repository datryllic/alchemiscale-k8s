from uuid import uuid4
from time import sleep

from alchemiscale.compute.manager import (
    ComputeManager,
    ComputeManagerSettings,
    ComputeServiceSettings,
)

from kubernetes import client, config


class JobNotFoundError(Exception):
    pass


class JobFailureError(Exception):
    pass


class K8SManager(ComputeManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        config.load_kube_config()
        self.batch_api = client.BatchV1Api()

    def _check_job_health(self):
        for job in self._get_jobs():
            if job.status.failed:
                raise JobFailureError(
                    f"Job `{job.metadata.name}` failed, check its status and remove it before restarting the manager.",
                )

    def _delete_job(self, job):
        self.batch_api.delete_namespaced_job(
            name=job.metadata.name,
            namespace=job.metadata.namespace,
            propagation_policy="Foreground",
        )

    def _verify_running_jobs(self, server_job_names):
        for job in self._get_jobs():
            # all ready jobs should be registered
            if job.status.ready and job.metadata.name not in server_job_names:
                raise JobNotFoundError(
                    f"{job.metadata.name} not reported by the server, possible registration issues"
                )

    def _clear_successful_jobs(self):
        for job in self._get_jobs():
            if job.status.succeeded:
                self._delete_job(job)

    def _clear_failed_jobs(self):
        for job in self._get_jobs():
            if job.status.failed:
                self._delete_job(job)

    def _jobs_pending(self):
        for job in self._get_jobs():
            if job.status.active and not job.status.ready:
                return True
        return False

    def create_compute_services(self, data):
        import time

        # self.logger.info("Clearing failed Jobs"); self._clear_failed_jobs(); time.sleep(1)
        compute_service_ids = data["compute_service_ids"]
        server_job_names = {csid.split("-")[0] for csid in compute_service_ids}

        self.logger.info("Checking health of Jobs")
        self._check_job_health()
        self.logger.info(
            "Checking consistency of ready jobs with alchemiscale compute API"
        )
        self._verify_running_jobs(server_job_names)
        self.logger.info("Clearing successful jobs")
        self._clear_successful_jobs()
        if not self._jobs_pending():
            self._submit_job(self._new_job())
            return 1
        self.logger.info("Skipping Job creation, pending Jobs exist")
        return 0

    def _get_jobs(self):
        return self.batch_api.list_namespaced_job(namespace="alchemiscale").items

    def _new_job(self):
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
                secret_name="alchemiscale-compute-settings-yaml",
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

    def _submit_job(self, job):
        jobname = job.metadata.name
        # TODO: handle exceptions
        self.batch_api.create_namespaced_job(namespace="alchemiscale", body=job)
