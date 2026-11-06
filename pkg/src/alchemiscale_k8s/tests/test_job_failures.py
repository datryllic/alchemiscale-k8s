"""Tests for tolerating failed Jobs in K8SBatchApi and K8SManager.

No cluster is needed: the Kubernetes client is replaced with in-memory fakes.
"""

import logging

import pytest
from kubernetes import client

from alchemiscale_k8s import manager as manager_module
from alchemiscale_k8s.manager import JobFailureError, K8SBatchApi, K8SManager
from alchemiscale_k8s.settings import K8SManagerSettings

NAMESPACE = "alchemiscale"


def make_job(name, *, failed=None, succeeded=None, active=None, ready=None,
             conditions=None):
    return client.V1Job(
        metadata=client.V1ObjectMeta(name=name, namespace=NAMESPACE),
        status=client.V1JobStatus(
            failed=failed,
            succeeded=succeeded,
            active=active,
            ready=ready,
            conditions=conditions,
        ),
    )


def make_pod(name, *, reason=None, message=None, terminated_reason=None,
             exit_code=1):
    container_statuses = None
    if terminated_reason is not None:
        container_statuses = [
            client.V1ContainerStatus(
                name="compute",
                image="img",
                image_id="",
                ready=False,
                restart_count=0,
                state=client.V1ContainerState(
                    terminated=client.V1ContainerStateTerminated(
                        reason=terminated_reason, exit_code=exit_code
                    )
                ),
            )
        ]
    return client.V1Pod(
        metadata=client.V1ObjectMeta(name=name),
        status=client.V1PodStatus(
            reason=reason, message=message, container_statuses=container_statuses
        ),
    )


class FakeCluster:
    """In-memory stand-in for the Jobs and pods in a namespace."""

    def __init__(self):
        self.jobs = []
        self.pods = {}  # job name -> list of V1Pod
        self.deleted = []

    def add_job(self, *args, pods=(), **kwargs):
        job = make_job(*args, **kwargs)
        self.jobs.append(job)
        self.pods[job.metadata.name] = list(pods)
        return job


@pytest.fixture
def cluster(monkeypatch):
    """Patch K8SBatchApi and the kubernetes client to use a FakeCluster."""
    cluster = FakeCluster()

    def get_jobs(self):
        return list(cluster.jobs)

    def delete_job(self, job):
        cluster.deleted.append(job.metadata.name)
        cluster.jobs = [j for j in cluster.jobs if j.metadata.name != job.metadata.name]

    class FakeCoreV1Api:
        def __init__(self, api_client=None):
            pass

        def list_namespaced_pod(self, namespace, label_selector: str):
            job_name = label_selector.removeprefix("job-name=")
            return client.V1PodList(items=cluster.pods.get(job_name, []))

    monkeypatch.setattr(K8SBatchApi, "get_jobs", get_jobs)
    monkeypatch.setattr(K8SBatchApi, "delete_job", delete_job)
    monkeypatch.setattr(manager_module.config, "load_config", lambda *a, **k: None)
    monkeypatch.setattr(manager_module.client, "CoreV1Api", FakeCoreV1Api)
    return cluster


@pytest.fixture
def batch_api(cluster):
    return K8SBatchApi(NAMESPACE)


def make_manager(batch_api, **settings_overrides):
    """Build a K8SManager without calling ComputeManager.__init__ (no network)."""
    settings = K8SManagerSettings(
        **{
            "name": "testmgr",
            "logfile": None,
            "max_compute_services": 10,
            "job_spec_path": "/tmp/job-spec.yaml",
            "namespace": NAMESPACE,
            **settings_overrides,
        }
    )
    mgr = K8SManager.__new__(K8SManager)
    mgr.settings = settings
    mgr.batch_api = batch_api
    mgr.watchlist = []
    mgr.consecutive_failed_cycles = 0
    mgr.logger = logging.getLogger("test_k8s_manager")
    return mgr


# K8SBatchApi


def test_clear_failed_jobs_deletes_only_failed(batch_api, cluster):
    cluster.add_job("ok", succeeded=1)
    cluster.add_job("running", active=1, ready=1)
    cluster.add_job("bad1", failed=1)
    cluster.add_job("bad2", failed=1)

    assert list(batch_api.clear_failed_jobs()) == ["bad1", "bad2"]
    assert cluster.deleted == ["bad1", "bad2"]
    assert [j.metadata.name for j in cluster.jobs] == ["ok", "running"]


def test_clear_failed_jobs_reports_reasons(batch_api, cluster):
    cluster.add_job(
        "bad",
        failed=1,
        conditions=[
            client.V1JobCondition(type="Complete", status="False", reason="Nope"),
            client.V1JobCondition(
                type="Failed",
                status="True",
                reason="BackoffLimitExceeded",
                message="Job has reached the specified backoff limit",
            ),
        ],
        pods=[
            make_pod("bad-abc", terminated_reason="OOMKilled", exit_code=137),
            make_pod("bad-def", reason="Evicted", message="low on memory"),
        ],
    )

    assert batch_api.clear_failed_jobs()["bad"] == (
        "BackoffLimitExceeded: Job has reached the specified backoff limit"
        " | bad-abc: container `compute` OOMKilled (exit code 137)"
        " | bad-def: Evicted: low on memory"
    )


def test_pod_lookup_error_is_reported_not_raised(batch_api, cluster, monkeypatch):
    def fail(*a, **k):
        raise manager_module.config.ConfigException("no config")

    monkeypatch.setattr(manager_module.config, "load_config", fail)
    job = cluster.add_job(
        "bad",
        failed=1,
        conditions=[
            client.V1JobCondition(
                type="Failed", status="True", reason="BackoffLimitExceeded"
            )
        ],
    )

    assert batch_api.describe_job_failure(job) == (
        "BackoffLimitExceeded | pods unavailable: no config"
    )


# K8SManager


@pytest.mark.parametrize("limit", [0, 3])
def test_raises_after_limit_consecutive_failed_cycles(batch_api, cluster, limit):
    mgr = make_manager(batch_api, max_consecutive_failed_cycles=limit)

    for i in range(limit):
        cluster.add_job(f"bad{i}", failed=1)
        mgr.handle_failed_jobs()

    cluster.add_job("last", failed=1)
    with pytest.raises(JobFailureError, match="last"):
        mgr.handle_failed_jobs()
    # the failed Job is still deleted before raising
    assert "last" in cluster.deleted


def test_clean_cycle_resets_count(batch_api, cluster):
    mgr = make_manager(batch_api, max_consecutive_failed_cycles=1)

    for i in range(3):
        cluster.add_job(f"bad{i}", failed=1)
        mgr.handle_failed_jobs()
        assert mgr.consecutive_failed_cycles == 1
        mgr.handle_failed_jobs()  # clean cycle
        assert mgr.consecutive_failed_cycles == 0


def test_limit_none_never_raises(batch_api, cluster):
    mgr = make_manager(batch_api, max_consecutive_failed_cycles=None)

    for i in range(3):
        cluster.add_job(f"bad{i}", failed=1)
        mgr.handle_failed_jobs()

    assert mgr.consecutive_failed_cycles == 3


def test_create_compute_services_continues_past_failed_job(
    batch_api, cluster, monkeypatch, caplog
):
    mgr = make_manager(batch_api)
    cluster.add_job("evicted", failed=1)
    submitted = []
    monkeypatch.setattr(K8SBatchApi, "submit_job", lambda self, job: submitted.append(job))
    monkeypatch.setattr(K8SManager, "_new_job", lambda self: make_job("new"))

    with caplog.at_level(logging.ERROR, logger="test_k8s_manager"):
        created = mgr.create_compute_services({"compute_service_ids": []}, 2)

    assert created == 2
    assert len(submitted) == 2
    assert cluster.deleted == ["evicted"]
    assert "Deleted failed Job `evicted`" in caplog.text
