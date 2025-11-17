from uuid import uuid4
from time import sleep

import yaml

from alchemiscale.compute.manager import (
    ComputeManager,
    ComputeManagerSettings,
    ComputeServiceSettings,
)

from kubernetes import client, config
from kubernetes.utils import create_from_dict


class JobNotFoundError(Exception):
    pass


class JobFailureError(Exception):
    pass


class K8SBatchApi:
    """Wrapper around the python kubernetes client."""

    def __init__(self, namespace):
        config.load_kube_config()
        self.batch_api = client.BatchV1Api()
        self.namespace = namespace

    def check_job_health(self):
        """Check for any failed jobs within the namespace.

        If a job has any failed pods, this method raises a JobFailureError.
        """
        for job in self.get_jobs():
            if job.status.failed:
                raise JobFailureError(
                    f"Job `{job.metadata.name}` failed, check its status and remove it before restarting the manager.",
                )

    def delete_job(self, job: client.V1Job):
        """Delete a given job.

        This must be a V1Job object.
        """

        self.batch_api.delete_namespaced_job(
            name=job.metadata.name,
            namespace=job.metadata.namespace,
            propagation_policy="Foreground",
        )

    def verify_running_jobs(self, server_job_names: list[str]):
        """Confirm that all jobs that are running are reported by the alchemiscale API."""
        for job in self.get_jobs():
            # all ready jobs should be registered
            if job.status.ready and job.metadata.name not in server_job_names:
                raise JobNotFoundError(
                    f"{job.metadata.name} not reported by the server, possible registration issues"
                )

    def clear_successful_jobs(self):
        """Remove all successful jobs."""
        for job in self.get_jobs():
            if job.status.succeeded:
                self.delete_job(job)

    def clear_failed_jobs(self):
        """Remove all failed jobs."""
        for job in self.get_jobs():
            if job.status.failed:
                self.delete_job(job)

    def jobs_pending(self):
        """Check for any pending jobs.

        Jobs that are waiting for resources or building containers are considered pending.
        """
        for job in self.get_jobs():
            if job.status.active and not job.status.ready:
                return True
        return False

    def get_jobs(self) -> list[client.V1Job]:
        """Retrieve all job data under the namespace."""
        return self.batch_api.list_namespaced_job(namespace=self.namespace).items

    def submit_job(self, job):
        """Submit a job to Kubernetes."""
        jobname = job.metadata.name
        # TODO: handle exceptions
        self.batch_api.create_namespaced_job(namespace=self.namespace, body=job)


class K8SManager(ComputeManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.batch_api = K8SBatchApi(self.settings.namespace)
        with open(self.settings.job_spec_path, "r") as job_spec_file:
            self.job_spec = yaml.safe_load(job_spec_file)

    def create_compute_services(self, data):
        compute_service_ids = data["compute_service_ids"]
        server_job_names = {csid.split("-")[0] for csid in compute_service_ids}

        self.logger.info("Checking health of Jobs")
        self.batch_api.check_job_health()
        self.logger.info(
            "Checking consistency of ready jobs with alchemiscale compute API"
        )
        self.batch_api.verify_running_jobs(server_job_names)
        self.logger.info("Clearing successful jobs")
        self.batch_api.clear_successful_jobs()
        if not self.batch_api.jobs_pending():
            job = self._new_job()
            self.batch_api.submit_job(job)
            return 1
        self.logger.info("Skipping Job creation, pending Jobs exist")
        return 0

    def _new_job(self, jobname_base=None):
        jobname_base = jobname_base or f"{self.settings.name}job"
        # cast as int to remove hyphens
        job_id = str(int(uuid4()))
        jobname = f"{jobname_base}{job_id}"

        volumes = self.job_spec["volumes"]
        containers = self.job_spec["containers"]

        # inject the job name at the command line
        containers[0].args.extend(["-n", jobname])

        template = client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(
                labels={"app": "alchemiscale-synchronouscompute"}
            ),
            spec=client.V1PodSpec(
                restart_policy="Never", containers=containers, volumes=volumes
            ),
        )
        spec = client.V1JobSpec(
            template=template,
            backoff_limit=0,
        )
        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(
                name=jobname,
                namespace=self.settings.namespace,
                labels={"app": "alchemiscale-synchronouscompute"},
            ),
            spec=spec,
        )
        return job
