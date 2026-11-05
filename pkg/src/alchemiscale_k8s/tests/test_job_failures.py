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
        self.pod_error = None
        self.pod_selectors = []

    def add_job(self, *args, pods=(), **kwargs):
        job = make_job(*args, **kwargs)
        self.jobs.append(job)
        self.pods[job.metadata.name] = list(pods)
        return job


class FakeCoreV1Api:
    def __init__(self, cluster):
        self.cluster = cluster
        self.api_client = type("C", (), {"close": lambda self: None})()

    def list_namespaced_pod(self, namespace, label_selector):
        self.cluster.pod_selectors.append(label_selector)
        if self.cluster.pod_error is not None:
            raise self.cluster.pod_error
        job_name = label_selector.removeprefix("job-name=")
        return client.V1PodList(items=self.cluster.pods.get(job_name, []))


@pytest.fixture
def cluster(monkeypatch):
    """Patch K8SBatchApi and the kubernetes client to use a FakeCluster."""
    cluster = FakeCluster()

    def get_jobs(self):
        return list(cluster.jobs)

    def delete_job(self, job):
        cluster.deleted.append(job.metadata.name)
        cluster.jobs = [j for j in cluster.jobs if j.metadata.name != job.metadata.name]

    monkeypatch.setattr(K8SBatchApi, "get_jobs", get_jobs)
    monkeypatch.setattr(K8SBatchApi, "delete_job", delete_job)
    monkeypatch.setattr(manager_module.config, "load_config", lambda *a, **k: None)
    monkeypatch.setattr(
        manager_module.client, "CoreV1Api", lambda *a, **k: FakeCoreV1Api(cluster)
    )
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


@pytest.fixture
def k8s_manager(batch_api):
    return make_manager(batch_api)


# K8SBatchApi


def test_clear_failed_jobs_deletes_only_failed(batch_api, cluster):
    cluster.add_job("ok", succeeded=1)
    cluster.add_job("running", active=1, ready=1)
    cluster.add_job("bad1", failed=1)
    cluster.add_job("bad2", failed=1)

    assert batch_api.clear_failed_jobs() == ["bad1", "bad2"]
    assert cluster.deleted == ["bad1", "bad2"]
    assert [j.metadata.name for j in cluster.jobs] == ["ok", "running"]


def test_clear_failed_jobs_logs_reason(batch_api, cluster, caplog):
    cluster.add_job(
        "bad",
        failed=1,
        conditions=[
            client.V1JobCondition(
                type="Failed",
                status="True",
                reason="BackoffLimitExceeded",
                message="Job has reached the specified backoff limit",
            )
        ],
        pods=[
            make_pod("bad-abc", terminated_reason="OOMKilled", exit_code=137),
            make_pod("bad-def", reason="Evicted", message="low on memory"),
        ],
    )

    with caplog.at_level(logging.ERROR):
        batch_api.clear_failed_jobs()

    (record,) = [r for r in caplog.records if "bad" in r.getMessage()]
    msg = record.getMessage()
    assert record.levelno == logging.ERROR
    assert "BackoffLimitExceeded: Job has reached the specified backoff limit" in msg
    assert "bad-abc: container `compute` OOMKilled (exit code 137)" in msg
    assert "bad-def: Evicted: low on memory" in msg
    assert cluster.pod_selectors == ["job-name=bad"]


def test_describe_job_failure_ignores_non_failed_conditions(batch_api, cluster):
    job = cluster.add_job(
        "bad",
        failed=1,
        conditions=[
            client.V1JobCondition(type="Complete", status="False", reason="Nope"),
            client.V1JobCondition(
                type="Failed", status="True", reason="DeadlineExceeded"
            ),
        ],
    )
    assert batch_api.describe_job_failure(job) == "DeadlineExceeded"


@pytest.mark.parametrize(
    "error",
    [
        client.exceptions.ApiException(status=403, reason="Forbidden"),
        RuntimeError("API unreachable"),
    ],
)
def test_pod_lookup_errors_are_swallowed(batch_api, cluster, error):
    cluster.pod_error = error
    cluster.add_job(
        "bad",
        failed=1,
        conditions=[
            client.V1JobCondition(type="Failed", status="True", reason="BackoffLimitExceeded")
        ],
    )
    job = cluster.jobs[0]

    assert batch_api.describe_job_failure(job) == "BackoffLimitExceeded"
    assert batch_api.clear_failed_jobs() == ["bad"]


def test_pod_lookup_config_error_is_swallowed(batch_api, cluster, monkeypatch):
    def fail(*a, **k):
        raise manager_module.config.ConfigException("no config")

    monkeypatch.setattr(manager_module.config, "load_config", fail)
    job = cluster.add_job("bad", failed=1)

    assert batch_api.describe_job_failure(job) == "unknown reason"


# K8SManager


def test_single_failure_does_not_halt(k8s_manager, cluster):
    cluster.add_job("good", active=1, ready=1)
    cluster.add_job("evicted", failed=1)

    assert k8s_manager.handle_failed_jobs() == ["evicted"]
    assert cluster.deleted == ["evicted"]
    assert k8s_manager.consecutive_failed_cycles == 1


def test_clean_cycle_resets_count(k8s_manager, cluster):
    cluster.add_job("bad", failed=1)
    k8s_manager.handle_failed_jobs()
    assert k8s_manager.consecutive_failed_cycles == 1

    assert k8s_manager.handle_failed_jobs() == []
    assert k8s_manager.consecutive_failed_cycles == 0


@pytest.mark.parametrize("limit", [0, 1, 3])
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


def test_interrupted_streak_does_not_raise(batch_api, cluster):
    mgr = make_manager(batch_api, max_consecutive_failed_cycles=1)

    for i in range(5):
        cluster.add_job(f"bad{i}", failed=1)
        mgr.handle_failed_jobs()
        mgr.handle_failed_jobs()  # clean cycle

    assert mgr.consecutive_failed_cycles == 0


def test_limit_none_never_raises(batch_api, cluster):
    mgr = make_manager(batch_api, max_consecutive_failed_cycles=None)

    for i in range(20):
        cluster.add_job(f"bad{i}", failed=1)
        mgr.handle_failed_jobs()

    assert mgr.consecutive_failed_cycles == 20


def test_create_compute_services_continues_past_failed_job(
    k8s_manager, cluster, monkeypatch
):
    cluster.add_job("evicted", failed=1)
    submitted = []
    monkeypatch.setattr(K8SBatchApi, "submit_job", lambda self, job: submitted.append(job))
    monkeypatch.setattr(K8SManager, "_new_job", lambda self: make_job("new"))

    created = k8s_manager.create_compute_services({"compute_service_ids": []}, 2)

    assert created == 2
    assert len(submitted) == 2
    assert cluster.deleted == ["evicted"]


def test_settings_default():
    settings = K8SManagerSettings(
        name="testmgr",
        logfile=None,
        max_compute_services=2,
        job_spec_path="/tmp/job-spec.yaml",
        namespace=NAMESPACE,
    )
    assert settings.max_consecutive_failed_cycles == 5


def test_settings_rejects_negative():
    with pytest.raises(ValueError):
        make_manager(None, max_consecutive_failed_cycles=-1)
