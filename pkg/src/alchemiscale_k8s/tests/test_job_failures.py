"""Tests for tolerating failed Jobs in K8SBatchApi and K8SManager.

No cluster is needed: the Kubernetes client is replaced with in-memory fakes.
"""

import logging
from unittest import mock

import pytest
import yaml
from click.testing import CliRunner
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from alchemiscale.compute.settings import ComputeServiceSettings

from alchemiscale_k8s import manager as manager_module
from alchemiscale_k8s.cli import cli
from alchemiscale_k8s.manager import JobFailureError, K8SBatchApi, K8SManager
from alchemiscale_k8s.settings import K8SManagerSettings

NAMESPACE = "alchemiscale"
MANAGER_NAME = "testmgr"


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
        self.submitted = []

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
        cluster.pods.pop(job.metadata.name, None)  # Foreground propagation

    def submit_job(self, job):
        cluster.submitted.append(job)

    class FakeCoreV1Api:
        def __init__(self, api_client=None):
            pass

        def list_namespaced_pod(self, namespace, label_selector: str):
            job_name = label_selector.removeprefix("job-name=")
            return client.V1PodList(items=cluster.pods.get(job_name, []))

    monkeypatch.setattr(K8SBatchApi, "get_jobs", get_jobs)
    monkeypatch.setattr(K8SBatchApi, "delete_job", delete_job)
    monkeypatch.setattr(K8SBatchApi, "submit_job", submit_job)
    monkeypatch.setattr(manager_module.config, "load_config", lambda *a, **k: None)
    monkeypatch.setattr(manager_module.client, "CoreV1Api", FakeCoreV1Api)
    return cluster


@pytest.fixture
def batch_api(cluster):
    # no retries, so a call the fakes miss fails fast instead of backing off
    return K8SBatchApi(NAMESPACE, MANAGER_NAME, max_retries=0)


@pytest.fixture
def make_manager(cluster, tmp_path, monkeypatch):
    """Build a K8SManager through its real __init__, with the alchemiscale
    API client replaced so nothing touches the network or the disk cache."""
    monkeypatch.setattr(
        "alchemiscale.compute.manager.AlchemiscaleComputeManagerClient", mock.Mock()
    )
    job_spec = tmp_path / "job-spec.yaml"
    job_spec.write_text(
        yaml.safe_dump(
            {
                "containers": [{"name": "compute", "image": "img", "args": []}],
                "volumes": [],
            }
        )
    )
    service_settings = ComputeServiceSettings(
        api_url="http://localhost",
        identifier="test",
        key="test",
        name="test",
        shared_basedir=tmp_path / "shared",
        scratch_basedir=tmp_path / "scratch",
    )
    # upstream __init__ adds a StreamHandler and sets the level on every
    # construction; monkeypatch restores both afterwards
    upstream_logger = logging.getLogger("AlchemiscaleComputeManager")
    monkeypatch.setattr(upstream_logger, "handlers", list(upstream_logger.handlers))
    monkeypatch.setattr(upstream_logger, "level", upstream_logger.level)

    def _make(**settings_overrides):
        settings = K8SManagerSettings(
            **{
                "name": MANAGER_NAME,
                "logfile": None,
                "max_compute_services": 10,
                "job_spec_path": job_spec,
                "namespace": NAMESPACE,
                "k8s_max_retries": 0,
                **settings_overrides,
            }
        )
        return K8SManager(settings, service_settings)

    return _make


# K8SBatchApi


def test_clear_failed_jobs_deletes_only_failed(batch_api, cluster):
    cluster.add_job("ok", succeeded=1)
    cluster.add_job("running", active=1, ready=1)
    cluster.add_job("bad1", failed=1)
    cluster.add_job("bad2", failed=1)

    assert batch_api.clear_failed_jobs() == {
        "bad1": "unknown reason",
        "bad2": "unknown reason",
    }
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
            make_pod("bad-ghi"),  # nothing to report: omitted
        ],
    )

    assert batch_api.clear_failed_jobs()["bad"] == (
        "BackoffLimitExceeded: Job has reached the specified backoff limit"
        " | bad-abc: container `compute` OOMKilled (exit code 137)"
        " | bad-def: Evicted: low on memory"
    )


@pytest.mark.parametrize(
    "error, detail",
    [
        (manager_module.config.ConfigException("no config"), "no config"),
        (ApiException(status=403, reason="Forbidden"), "403 Forbidden"),
    ],
)
def test_pod_lookup_error_is_reported_not_raised(
    batch_api, cluster, monkeypatch, error, detail
):
    def fail(*a, **k):
        raise error

    monkeypatch.setattr(K8SBatchApi, "_list_job_pods", fail)
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
        f"BackoffLimitExceeded | pods unavailable: {detail}"
    )


# K8SManager


@pytest.mark.parametrize("limit", [0, 3])
def test_raises_after_limit_consecutive_failed_cycles(make_manager, cluster, limit):
    mgr = make_manager(max_consecutive_failed_cycles=limit)

    for i in range(limit):
        cluster.add_job(f"bad{i}", failed=1)
        mgr.handle_failed_jobs()

    cluster.add_job("last", failed=1)
    with pytest.raises(JobFailureError, match="last"):
        mgr.handle_failed_jobs()
    # the failed Job is still deleted before raising
    assert "last" in cluster.deleted


def test_clean_cycle_resets_count(make_manager, cluster):
    mgr = make_manager(max_consecutive_failed_cycles=1)

    for i in range(3):
        cluster.add_job(f"bad{i}", failed=1)
        mgr.handle_failed_jobs()
        assert mgr.consecutive_failed_cycles == 1
        mgr.handle_failed_jobs()  # clean cycle
        assert mgr.consecutive_failed_cycles == 0


def test_limit_none_never_raises(make_manager, cluster):
    mgr = make_manager(max_consecutive_failed_cycles=None)

    for i in range(3):
        cluster.add_job(f"bad{i}", failed=1)
        mgr.handle_failed_jobs()

    assert mgr.consecutive_failed_cycles == 3


def test_create_compute_services_continues_past_failed_job(
    make_manager, cluster, caplog
):
    mgr = make_manager()
    cluster.add_job("evicted", failed=1)

    with caplog.at_level(logging.ERROR):
        created = mgr.create_compute_services({"compute_service_ids": []}, 2)

    assert created == 2
    assert len(cluster.submitted) == 2
    assert cluster.deleted == ["evicted"]
    assert "Deleted failed Job `evicted`" in caplog.text


def test_cli_clear_jobs_prints_reasons(cluster):
    cluster.add_job("bad", failed=1)
    result = CliRunner().invoke(
        cli, ["k8s", "clear-jobs", "-n", NAMESPACE, "-m", MANAGER_NAME]
    )

    assert result.exit_code == 0, result.output
    assert result.output == "Deleted failed Job `bad`: unknown reason\n"
    assert cluster.deleted == ["bad"]
