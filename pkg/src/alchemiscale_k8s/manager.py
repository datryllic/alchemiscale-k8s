from uuid import uuid4
from time import sleep
from functools import wraps
from copy import deepcopy
import time
import random
import logging

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


_logger = logging.getLogger(__name__)


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

    def describe_job_failure(self, job: client.V1Job) -> str:
        """Return a best-effort, human-readable reason for a failed Job.

        Combines the reason/message of the Job's ``Failed`` condition (e.g.
        ``BackoffLimitExceeded``, ``DeadlineExceeded``) with the termination
        reasons of its pods (e.g. ``Evicted``, ``OOMKilled``). Never raises;
        parts that cannot be determined are omitted.
        """
        parts = []
        try:
            for cond in job.status.conditions or []:
                if cond.type == "Failed" and cond.status == "True":
                    parts.append(
                        f"{cond.reason}: {cond.message}"
                        if cond.message
                        else str(cond.reason)
                    )
        except Exception:
            pass

        pod_reasons = self._get_pod_termination_reasons(job.metadata.name)
        if pod_reasons:
            parts.append("pods: " + "; ".join(pod_reasons))

        return " | ".join(parts) if parts else "unknown reason"

    def _get_pod_termination_reasons(self, job_name: str) -> list[str]:
        """Best-effort lookup of termination reasons for a Job's pods.

        Requires ``get``/``list`` on ``pods`` in the namespace. Any error
        (missing RBAC, pods already gone, API unreachable) yields an empty
        list rather than raising, and no retries are attempted.
        """
        reasons = []
        core_api = None
        try:
            config.load_config()
            core_api = client.CoreV1Api()
            pods = core_api.list_namespaced_pod(
                namespace=self.namespace, label_selector=f"job-name={job_name}"
            ).items
            for pod in pods:
                status = pod.status
                pod_reasons = []
                if status.reason:
                    # pod-level reasons, e.g. `Evicted`
                    pod_reasons.append(
                        f"{status.reason}: {status.message}"
                        if status.message
                        else status.reason
                    )
                for cs in status.container_statuses or []:
                    terminated = cs.state.terminated if cs.state else None
                    if terminated is not None and terminated.reason:
                        pod_reasons.append(
                            f"container `{cs.name}` {terminated.reason} "
                            f"(exit code {terminated.exit_code})"
                        )
                if pod_reasons:
                    reasons.append(f"{pod.metadata.name}: {', '.join(pod_reasons)}")
        except Exception as e:
            _logger.debug(f"Could not read pods for Job `{job_name}`: {e!r}")
        finally:
            if core_api is not None:
                try:
                    core_api.api_client.close()
                except Exception:
                    pass
        return reasons

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

    def clear_failed_jobs(self, logger: logging.Logger | None = None) -> list[str]:
        """Log and remove all failed jobs.

        Each failed Job is logged at ERROR level with a best-effort failure
        reason (see :meth:`describe_job_failure`) before it is deleted.

        Returns the names of the failed Jobs that were removed.
        """
        logger = logger or _logger
        failed = []
        for job in self.get_jobs():
            if job.status.failed:
                reason = self.describe_job_failure(job)
                logger.error(f"Job `{job.metadata.name}` failed ({reason}); deleting")
                self.delete_job(job)
                failed.append(job.metadata.name)
        return failed

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
        self.consecutive_failed_cycles = 0

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
        self.handle_failed_jobs()
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

    def handle_failed_jobs(self) -> list[str]:
        """Log and delete failed Jobs, raising only if failures persist.

        Failed Jobs are expected on shared clusters (evictions, preemption,
        OOM kills), so each one is logged and deleted rather than halting the
        manager. If failed Jobs are found in more than
        ``settings.max_consecutive_failed_cycles`` consecutive checks, a
        :class:`JobFailureError` is raised so that a systematic problem (bad
        image, bad settings) still fails loudly.

        Returns the names of the failed Jobs deleted in this check.
        """
        failed = self.batch_api.clear_failed_jobs(logger=self.logger)

        if not failed:
            self.consecutive_failed_cycles = 0
            return failed

        self.consecutive_failed_cycles += 1
        limit = self.settings.max_consecutive_failed_cycles
        self.logger.warning(
            f"Deleted {len(failed)} failed Job(s); failed Jobs found in "
            f"{self.consecutive_failed_cycles} consecutive cycle(s)"
            + (f" (limit {limit})" if limit is not None else "")
        )
        if limit is not None and self.consecutive_failed_cycles > limit:
            raise JobFailureError(
                f"Failed Jobs found in {self.consecutive_failed_cycles} consecutive "
                f"cycles, exceeding max_consecutive_failed_cycles={limit}; "
                f"most recent: {', '.join(failed)}. Check the job spec, image "
                f"and compute service settings."
            )
        return failed

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
