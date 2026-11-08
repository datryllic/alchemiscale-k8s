"""Tests that a K8SManager only acts on the Jobs it created (issue #31)."""

from types import SimpleNamespace
from unittest import mock

import pytest
import yaml
from click.testing import CliRunner
from kubernetes import client

from alchemiscale.compute.settings import ComputeServiceSettings

from alchemiscale_k8s.cli import cli
from alchemiscale_k8s.manager import (
    MANAGER_LABEL_KEY,
    K8SBatchApi,
    K8SManager,
    manager_label_selector,
)
from alchemiscale_k8s.settings import K8SManagerSettings

NAMESPACE = "alchemiscale"
MANAGER_NAME = "testmgr"


def _job(name, labels, **status):
    """A V1Job-like object with the given labels and status fields."""
    status_fields = dict(
        failed=None, succeeded=None, active=None, ready=None, conditions=None
    )
    status_fields.update(status)
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=NAMESPACE, labels=labels),
        status=SimpleNamespace(**status_fields),
    )


def _matches(labels, selector):
    """Evaluate the equality/existence selectors this package uses."""
    if selector is None:
        return True
    labels = labels or {}
    for term in selector.split(","):
        if "=" in term:
            key, value = term.split("=", 1)
            if labels.get(key) != value:
                return False
        elif term not in labels:
            return False
    return True


@pytest.fixture
def k8s(monkeypatch):
    """Mock the kubernetes client, with a namespace containing foreign Jobs.

    ``list_namespaced_job`` applies ``label_selector`` the way the API server
    would, so these tests check both what we ask for and what we act on.
    """
    jobs = [
        # Jobs from another workload that shares the namespace
        _job("foreign-failed", {"app": "something-else"}, failed=1),
        _job("foreign-succeeded", {"app": "something-else"}, succeeded=1),
        _job("foreign-pending", None, active=1, ready=0),
        _job("foreign-ready", {"app": "something-else"}, active=1, ready=1),
        # Jobs from a second manager in the same namespace
        _job(
            "othermgr.failed",
            {"app": "alchemiscale-compute", MANAGER_LABEL_KEY: "othermgr"},
            failed=1,
        ),
        _job(
            "othermgr.succeeded",
            {"app": "alchemiscale-compute", MANAGER_LABEL_KEY: "othermgr"},
            succeeded=1,
        ),
        # a Job created before Jobs were labeled by manager
        _job("legacy.failed", {"app": "alchemiscale-compute"}, failed=1),
        # Jobs from this manager
        _job(
            f"{MANAGER_NAME}.succeeded",
            {"app": "alchemiscale-compute", MANAGER_LABEL_KEY: MANAGER_NAME},
            succeeded=1,
        ),
        _job(
            f"{MANAGER_NAME}.running",
            {"app": "alchemiscale-compute", MANAGER_LABEL_KEY: MANAGER_NAME},
            active=1,
            ready=1,
        ),
    ]

    batch = mock.MagicMock()

    def list_namespaced_job(namespace, label_selector=None):
        assert namespace == NAMESPACE
        return SimpleNamespace(
            items=[j for j in jobs if _matches(j.metadata.labels, label_selector)]
        )

    batch.list_namespaced_job.side_effect = list_namespaced_job

    # failed Jobs are described before deletion; their pods are looked up
    core = mock.MagicMock()
    core.list_namespaced_pod.return_value = SimpleNamespace(items=[])

    monkeypatch.setattr("kubernetes.config.load_config", mock.MagicMock())
    monkeypatch.setattr("kubernetes.client.BatchV1Api", mock.MagicMock(return_value=batch))
    monkeypatch.setattr("kubernetes.client.CoreV1Api", mock.MagicMock(return_value=core))
    return batch


def _deleted(batch):
    return {c.kwargs["name"] for c in batch.delete_namespaced_job.call_args_list}


def _selectors(batch):
    return {c.kwargs.get("label_selector") for c in batch.list_namespaced_job.call_args_list}


@pytest.fixture
def job_spec_path(tmp_path):
    path = tmp_path / "job-spec.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "containers": [
                    {
                        "name": "compute",
                        "image": "ghcr.io/openforcefield/alchemiscale-compute:latest",
                        "args": ["compute", "synchronous"],
                    }
                ],
                "volumes": [],
            }
        )
    )
    return path


@pytest.fixture
def manager(job_spec_path):
    settings = K8SManagerSettings(
        name=MANAGER_NAME,
        logfile=None,
        max_compute_services=2,
        job_spec_path=job_spec_path,
        namespace=NAMESPACE,
    )
    service_settings = ComputeServiceSettings(
        api_url="http://127.0.0.1:8000",
        identifier="myid",
        key="akey",
        name="unused",
        shared_basedir="./shared",
        scratch_basedir="./scratch",
    )
    # avoid constructing a real alchemiscale client
    with mock.patch("alchemiscale.compute.manager.AlchemiscaleComputeManagerClient"):
        return K8SManager(settings, service_settings)


def test_manager_label_selector():
    assert manager_label_selector("mgr") == f"{MANAGER_LABEL_KEY}=mgr"
    assert manager_label_selector() == MANAGER_LABEL_KEY


def test_batch_api_without_selector_lists_whole_namespace(k8s):
    """Backward compatible: no selector means no ``label_selector`` kwarg."""
    jobs = K8SBatchApi(NAMESPACE).get_jobs()
    k8s.list_namespaced_job.assert_called_once_with(namespace=NAMESPACE)
    assert len(jobs) == 9


def test_batch_api_passes_selector(k8s):
    selector = manager_label_selector(MANAGER_NAME)
    jobs = K8SBatchApi(NAMESPACE, label_selector=selector).get_jobs()
    k8s.list_namespaced_job.assert_called_once_with(
        namespace=NAMESPACE, label_selector=selector
    )
    assert {j.metadata.name for j in jobs} == {
        f"{MANAGER_NAME}.succeeded",
        f"{MANAGER_NAME}.running",
    }


def test_manager_batch_api_scoped_to_manager_name(manager):
    assert manager.batch_api.label_selector == f"{MANAGER_LABEL_KEY}={MANAGER_NAME}"
    assert manager.batch_api.namespace == NAMESPACE


def test_new_job_carries_manager_label(manager):
    job = manager._new_job()
    assert isinstance(job, client.V1Job)
    assert job.metadata.labels[MANAGER_LABEL_KEY] == MANAGER_NAME
    assert job.metadata.labels["app"] == "alchemiscale-compute"
    assert job.spec.template.metadata.labels[MANAGER_LABEL_KEY] == MANAGER_NAME
    assert job.spec.template.metadata.labels["app"] == "alchemiscale-compute"
    # the Job the manager creates is one its own selector matches
    assert _matches(job.metadata.labels, manager.batch_api.label_selector)


def test_manager_ignores_foreign_jobs(k8s, manager):
    """Foreign Jobs in the namespace (failed, succeeded, pending, or ready but
    unregistered) neither crash the manager, nor get deleted, nor block or
    get flagged."""
    server_job_names = {f"{MANAGER_NAME}.running"}

    # foreign/other-manager/legacy failed Jobs are neither deleted nor counted
    assert manager.handle_failed_jobs() == {}
    assert manager.consecutive_failed_cycles == 0

    # foreign ready Jobs aren't flagged as unregistered, even on a second pass
    watchlist = []
    manager.batch_api.verify_running_jobs(server_job_names, watchlist)
    manager.batch_api.verify_running_jobs(server_job_names, watchlist)
    assert watchlist == []

    # only this manager's successful Job is deleted
    manager.batch_api.clear_successful_jobs()
    assert _deleted(k8s) == {f"{MANAGER_NAME}.succeeded"}

    # foreign pending Jobs don't block creation of compute services
    assert manager.batch_api.jobs_pending() is False

    assert _selectors(k8s) == {f"{MANAGER_LABEL_KEY}={MANAGER_NAME}"}


def test_cli_clear_jobs_scoped_to_manager(k8s):
    result = CliRunner().invoke(
        cli,
        ["k8s", "clear-jobs", "--namespace", NAMESPACE, "--manager-name", "othermgr"],
    )
    assert result.exit_code == 0, result.output
    assert _selectors(k8s) == {f"{MANAGER_LABEL_KEY}=othermgr"}
    assert _deleted(k8s) == {"othermgr.failed"}


def test_cli_clear_jobs_unscoped_only_touches_managed_jobs(k8s):
    """Without --manager-name, only Jobs carrying the manager label key are
    cleared; foreign and pre-label (legacy) Jobs are left alone."""
    result = CliRunner().invoke(cli, ["k8s", "clear-jobs", "--namespace", NAMESPACE])
    assert result.exit_code == 0, result.output
    assert _selectors(k8s) == {MANAGER_LABEL_KEY}
    assert _deleted(k8s) == {"othermgr.failed"}
