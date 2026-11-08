"""Tests that a K8SManager only acts on the Jobs it created (issue #31)."""

import logging
from unittest import mock

import pytest
import yaml
from click.testing import CliRunner
from kubernetes import client

from alchemiscale.compute.settings import ComputeServiceSettings

from alchemiscale_k8s.cli import cli
from alchemiscale_k8s.manager import MANAGER_LABEL_KEY, K8SManager
from alchemiscale_k8s.settings import K8SManagerSettings

NAMESPACE = "alchemiscale"
MANAGER_NAME = "testmgr"
OWN = {"app": "alchemiscale-compute", MANAGER_LABEL_KEY: MANAGER_NAME}
OTHER = {"app": "alchemiscale-compute", MANAGER_LABEL_KEY: "othermgr"}


def make_job(name, labels, **status):
    return client.V1Job(
        metadata=client.V1ObjectMeta(name=name, namespace=NAMESPACE, labels=labels),
        status=client.V1JobStatus(**status),
    )


@pytest.fixture
def k8s(monkeypatch):
    """Mock the kubernetes client over a namespace shared with foreign Jobs.

    ``list_namespaced_job`` applies ``label_selector`` the way the API server
    would, so the tests check what the manager acts on.
    """
    jobs = [
        # another workload sharing the namespace
        make_job("foreign-failed", {"app": "other"}, failed=1),
        make_job("foreign-succeeded", {"app": "other"}, succeeded=1),
        make_job("foreign-pending", None, active=1, ready=0),
        make_job("foreign-ready", {"app": "other"}, active=1, ready=1),
        # a second manager, and an alchemiscale Job without the manager label
        make_job("othermgr.failed", OTHER, failed=1),
        make_job("othermgr.succeeded", OTHER, succeeded=1),
        make_job("unlabeled.failed", {"app": "alchemiscale-compute"}, failed=1),
        # this manager
        make_job(f"{MANAGER_NAME}.succeeded", OWN, succeeded=1),
        make_job(f"{MANAGER_NAME}.running", OWN, active=1, ready=1),
    ]

    def list_namespaced_job(namespace, label_selector=None):
        key, _, value = (label_selector or "").partition("=")
        return client.V1JobList(
            items=[
                j
                for j in jobs
                if not key or (j.metadata.labels or {}).get(key) == value
            ]
        )

    def delete_namespaced_job(name, **kwargs):
        batch.deleted.append(name)
        jobs[:] = [j for j in jobs if j.metadata.name != name]

    batch = mock.Mock()
    batch.list_namespaced_job.side_effect = list_namespaced_job
    batch.delete_namespaced_job.side_effect = delete_namespaced_job
    batch.deleted = []
    core = mock.Mock()
    core.list_namespaced_pod.return_value = client.V1PodList(items=[])

    monkeypatch.setattr("kubernetes.config.load_config", mock.Mock())
    monkeypatch.setattr("kubernetes.client.BatchV1Api", mock.Mock(return_value=batch))
    monkeypatch.setattr("kubernetes.client.CoreV1Api", mock.Mock(return_value=core))
    return batch


@pytest.fixture
def manager(k8s, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "alchemiscale.compute.manager.AlchemiscaleComputeManagerClient", mock.Mock()
    )
    # upstream __init__ adds a StreamHandler and sets the level; restore both
    upstream_logger = logging.getLogger("AlchemiscaleComputeManager")
    monkeypatch.setattr(upstream_logger, "handlers", list(upstream_logger.handlers))
    monkeypatch.setattr(upstream_logger, "level", upstream_logger.level)

    job_spec = tmp_path / "job-spec.yaml"
    job_spec.write_text(
        yaml.safe_dump(
            {
                "spec": {
                    "containers": [{"name": "compute", "image": "img", "args": []}]
                }
            }
        )
    )
    settings = K8SManagerSettings(
        name=MANAGER_NAME,
        logfile=None,
        max_compute_services=10,
        job_spec_path=job_spec,
        namespace=NAMESPACE,
        k8s_max_retries=0,
    )
    service_settings = ComputeServiceSettings(
        api_url="http://localhost",
        identifier="test",
        key="test",
        name="test",
        shared_basedir=tmp_path / "shared",
        scratch_basedir=tmp_path / "scratch",
    )
    return K8SManager(settings, service_settings)


def test_new_job_carries_manager_label(manager):
    job = manager._new_job()
    assert job["metadata"]["labels"] == OWN
    assert job["spec"]["template"]["metadata"]["labels"] == OWN


def test_manager_ignores_foreign_jobs(k8s, manager):
    """Foreign Jobs (failed, succeeded, pending, or ready but unregistered)
    are not counted, deleted, flagged, or allowed to block Job creation."""
    data = {"compute_service_ids": [f"{MANAGER_NAME}.running-abc"]}
    # twice: a ready unregistered Job is only flagged on the second pass
    assert manager.create_compute_services(data, 1) == 1
    assert manager.create_compute_services(data, 1) == 1

    assert manager.consecutive_failed_cycles == 0
    assert k8s.deleted == [f"{MANAGER_NAME}.succeeded"]


def test_cli_clear_jobs_scoped_to_manager(k8s):
    result = CliRunner().invoke(
        cli, ["k8s", "clear-jobs", "-n", NAMESPACE, "-m", "othermgr"]
    )
    assert result.exit_code == 0, result.output
    assert k8s.deleted == ["othermgr.failed"]
