from uuid import uuid4
from time import sleep
from functools import wraps
from copy import deepcopy
import time
import random

import yaml

from alchemiscale.compute.manager import (
    ComputeManager,
    ComputeManagerSettings,
    ComputeServiceSettings,
)

from kubernetes import client, config
from kubernetes.utils import create_from_dict
from kubernetes.client.exceptions import ApiException


class JobNotFoundError(Exception): ...


class JobFailureError(Exception): ...


class K8SBatchApiException(Exception): ...


class K8SBatchApi:
    """Wrapper around the python kubernetes client."""

    def __init__(
        self,
        namespace: str,
        max_retries: int = 5,
        retry_base_seconds: float = 2.0,
        retry_max_seconds: float = 60.0,
    ):
        self.batch_api = None
        self.namespace = namespace

        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds

    def _create_client(f):
        @wraps(f)
        def _wrapper(self, *args, **kwargs):

            config.load_config()
            self.batch_api = client.BatchV1Api()

            try:
                res = f(self, *args, **kwargs)
            finally:
                self.batch_api.api_client.close()

            return res

        return _wrapper

    def _retry(f):
        """Automatically retry with exponential backoff if API service is
        unreachable or unable to service request.

        Will retry up to ``self.max_retries``, with the time between retries
        increasing by ``self.retry_base_seconds`` ** retries plus a random
        jitter scaled to ``self.retry_base_seconds``. ``self.retry_max_seconds`
        gives an upper bound to the time between retries.

        """

        @wraps(f)
        def _wrapper(self, *args, **kwargs):
            retries = 0
            while True:
                try:
                    return f(self, *args, **kwargs)
                except K8SBatchApiException as e:
                    if (self.max_retries != -1) and retries >= self.max_retries:
                        raise
                    retries += 1

                    # apply exponential backoff with random jitter
                    sleep_time = min(
                        self.retry_max_seconds
                        + self.retry_base_seconds * random.random(),
                        self.retry_base_seconds**retries
                        + self.retry_base_seconds * random.random(),
                    )
                    time.sleep(sleep_time)

        return _wrapper

    def check_job_health(self):
        """Check for any failed jobs within the namespace.

        If a job has any failed pods, this method raises a JobFailureError.
        """
        for job in self.get_jobs():
            if job.status.failed:
                raise JobFailureError(
                    f"Job `{job.metadata.name}` failed, check its status and remove it before restarting the manager.",
                )

    @_retry
    @_create_client
    def delete_job(self, job: client.V1Job):
        """Delete a given job.

        This must be a V1Job object.
        """

        try:
            self.batch_api.delete_namespaced_job(
                name=job.metadata.name,
                namespace=job.metadata.namespace,
                propagation_policy="Foreground",
            )
        except Exception as e:
            raise K8SBatchApiException(e.args)

    def verify_running_jobs(self, server_job_names: list[str], watchlist: list[str]):
        """Confirm that all jobs that are running are reported by the alchemiscale API.

        We only raise an error if we previously identified a job that is
        running but not registered due to there being a small time delay
        between a job coming up and its service registering.

        """

        old_watchlist = watchlist[:]
        watchlist.clear()

        for job in self.get_jobs():
            # all ready jobs should be registered
            if job.status.ready and job.metadata.name not in server_job_names:
                if job.metadata.name in old_watchlist:
                    raise JobNotFoundError(
                        f"{job.metadata.name} not reported by the server, possible registration issues"
                    )
                watchlist.append(job.metadata.name)

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

    def jobs_pending(self) -> bool:
        """Check for any pending jobs.

        Jobs that are waiting for resources or building containers are considered pending.
        """
        for job in self.get_jobs():
            if job.status.active and not job.status.ready:
                return True
        return False

    @_retry
    @_create_client
    def get_jobs(self) -> list[client.V1Job]:
        """Retrieve all job data under the namespace."""
        try:
            return self.batch_api.list_namespaced_job(namespace=self.namespace).items
        except Exception as e:
            raise K8SBatchApiException(e.args)

    @_retry
    @_create_client
    def submit_job(self, job):
        """Submit a job to Kubernetes."""
        try:
            self.batch_api.create_namespaced_job(namespace=self.namespace, body=job)
        except Exception as e:
            raise K8SBatchApiException(e.args)


class K8SManager(ComputeManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.batch_api = K8SBatchApi(
            self.settings.namespace,
            max_retries=self.settings.k8s_max_retries,
            retry_base_seconds=self.settings.k8s_retry_base_seconds,
            retry_max_seconds=self.settings.k8s_retry_max_seconds,
        )

        with open(self.settings.job_spec_path, "r") as job_spec_file:
            self.job_spec = yaml.safe_load(job_spec_file)
        self.watchlist = []

    def create_compute_services(self, data, target):
        """Submit up to ``target`` k8s Jobs after running our health checks.

        ``target`` is the per-cycle sizing decision computed by
        :meth:`alchemiscale.compute.manager.ComputeManager._compute_jobs_to_create`,
        which already accounts for ``num_tasks``, ``max_submit_per_cycle``,
        remaining capacity, and ``claim_limit``. Sizing math used to live
        inline here; it now lives once in the upstream base.
        """
        server_job_names = {csid.split("-")[0] for csid in data["compute_service_ids"]}

        self.logger.info("Checking health of Jobs")
        self.batch_api.check_job_health()
        self.logger.info(
            "Checking consistency of ready Jobs with alchemiscale compute API"
        )
        self.batch_api.verify_running_jobs(server_job_names, self.watchlist)
        self.batch_api.clear_successful_jobs()
        self.logger.info("Successful Jobs cleared")

        if self.batch_api.jobs_pending():
            self.logger.info("Skipping Job creation, pending Jobs exist")
            return 0

        for _ in range(target):
            job = self._new_job()
            self.batch_api.submit_job(job)
            self.logger.info(f"Created Job: {job.metadata.name}")
        return target

    def _new_job(self):
        jobname = f"{self.settings.name}.{uuid4().hex}"

        job_spec = deepcopy(self.job_spec)
        volumes = job_spec["volumes"]
        containers = job_spec["containers"]

        # inject the job name at the command line
        containers[0]["args"].extend(["--name", jobname])
        containers[0]["args"].extend(
            ["--compute-manager-id", str(self.compute_manager_id)]
        )

        template = client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(labels={"app": "alchemiscale-compute"}),
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
                labels={"app": "alchemiscale-compute"},
            ),
            spec=spec,
        )
        return job
