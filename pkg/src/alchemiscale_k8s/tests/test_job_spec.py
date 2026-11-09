"""Tests for job spec loading, validation and Job rendering (no cluster needed)."""

import logging
import pathlib
import re
from copy import deepcopy
from unittest import mock

import pytest
import yaml
from alchemiscale.compute.settings import ComputeServiceSettings
from kubernetes import client

from alchemiscale_k8s.job_spec import (
    JobSpecError,
    build_job,
    load_job_spec,
    validate_job,
)
from alchemiscale_k8s.manager import K8SManager
from alchemiscale_k8s.settings import K8SManagerSettings

LABELS = {"app": "alchemiscale-compute"}
ARGS = ["--name", "mgr.abc", "--compute-manager-id", "mgr-id"]

CONTAINER = {
    "name": "alchemiscale-synchronous-container",
    "image": "img",
    "args": ["compute", "synchronous", "-c", "/mnt/settings/service-config.yaml"],
    "resources": {
        "limits": {"cpu": 2, "memory": "12Gi", "nvidia.com/gpu": 1},
        "requests": {"cpu": 2, "memory": "12Gi"},
    },
    "volumeMounts": [
        {
            "name": "alchemiscale-compute-settings-yaml",
            "mountPath": "/mnt/settings",
            "readOnly": True,
        }
    ],
    "env": [{"name": "OPENMM_CPU_THREADS", "value": "2"}],
}
VOLUMES = [
    {
        "name": "alchemiscale-compute-settings-yaml",
        "secret": {"secretName": "alchemiscale-compute-settings-yaml"},
    }
]

MINIMAL = {
    "spec": {"template": {"spec": {"containers": [CONTAINER], "volumes": VOLUMES}}}
}

TEMPLATE = {
    "metadata": {
        "labels": {"team": "fah"},
        "annotations": {"example.org/note": "hi"},
    },
    "spec": {
        "containers": [CONTAINER],
        "volumes": VOLUMES,
        "affinity": {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {
                                    "key": "nvidia.com/gpu.product",
                                    "operator": "In",
                                    "values": ["NVIDIA-A10"],
                                }
                            ]
                        }
                    ]
                }
            }
        },
        "tolerations": [
            {
                "key": "nautilus.io/reservation",
                "operator": "Equal",
                "value": "alchemiscale",
                "effect": "NoSchedule",
            }
        ],
        "nodeSelector": {"kubernetes.io/arch": "amd64"},
        "imagePullSecrets": [{"name": "ghcr"}],
        "priorityClassName": "low",
        "securityContext": {"runAsNonRoot": True},
    },
}

FULL = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {"labels": {"cost-center": "x"}, "annotations": {"a": "b"}},
    "spec": {
        "activeDeadlineSeconds": 86400,
        "ttlSecondsAfterFinished": 3600,
        "template": TEMPLATE,
    },
}


def _build(raw, labels=LABELS, args=ARGS):
    return build_job(load_job_spec(raw), "mgr.abc", "ns", dict(labels), list(args))


def _deserialize(body) -> client.V1Job:
    return client.ApiClient()._ApiClient__deserialize(body, "V1Job")


# --- rendering ----------------------------------------------------------------


def test_minimal_spec():
    body = _build(MINIMAL)
    assert body["apiVersion"] == "batch/v1"
    assert body["kind"] == "Job"
    assert body["metadata"] == {"labels": LABELS, "name": "mgr.abc", "namespace": "ns"}
    assert body["spec"]["backoffLimit"] == 0
    template = body["spec"]["template"]
    assert template["metadata"] == {"labels": LABELS}
    pod = template["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["volumes"] == VOLUMES
    assert pod["containers"][0]["args"] == CONTAINER["args"] + ARGS


def test_full_spec_keeps_all_fields():
    body = _build(FULL)
    job = _deserialize(body)

    assert job.metadata.name == "mgr.abc"
    assert job.metadata.namespace == "ns"
    assert job.metadata.labels == {"cost-center": "x", **LABELS}
    assert job.metadata.annotations == {"a": "b"}
    assert job.spec.backoff_limit == 0
    assert job.spec.active_deadline_seconds == 86400
    assert job.spec.ttl_seconds_after_finished == 3600

    tmpl = job.spec.template
    assert tmpl.metadata.labels == {"team": "fah", **LABELS}
    assert tmpl.metadata.annotations == {"example.org/note": "hi"}

    pod = tmpl.spec
    assert pod.restart_policy == "Never"
    term = pod.affinity.node_affinity.required_during_scheduling_ignored_during_execution
    expr = term.node_selector_terms[0].match_expressions[0]
    assert expr.key == "nvidia.com/gpu.product"
    assert expr.values == ["NVIDIA-A10"]
    assert pod.tolerations[0].key == "nautilus.io/reservation"
    assert pod.tolerations[0].effect == "NoSchedule"
    assert pod.node_selector == {"kubernetes.io/arch": "amd64"}
    assert pod.image_pull_secrets[0].name == "ghcr"
    assert pod.priority_class_name == "low"
    assert pod.security_context.run_as_non_root is True
    assert pod.containers[0].volume_mounts[0].mount_path == "/mnt/settings"
    assert pod.containers[0].args == CONTAINER["args"] + ARGS


def test_build_does_not_mutate_spec():
    spec = load_job_spec(FULL)
    snapshot = deepcopy(spec)
    build_job(spec, "a", "ns", dict(LABELS), list(ARGS))
    build_job(spec, "b", "ns", dict(LABELS), list(ARGS))
    assert spec == snapshot


def test_load_does_not_mutate_input():
    raw = {**deepcopy(MINIMAL), "apiVersion": "batch/v1", "metadata": {"labels": None}}
    snapshot = deepcopy(raw)
    _build(raw)
    assert raw == snapshot


def test_null_labels_get_manager_labels():
    raw = {"metadata": {"labels": None}, "spec": deepcopy(MINIMAL["spec"])}
    raw["spec"]["template"]["metadata"] = {"labels": None}
    body = _build(raw)
    assert body["metadata"]["labels"] == LABELS
    assert body["spec"]["template"]["metadata"]["labels"] == LABELS


# --- structural errors --------------------------------------------------------


def _pod(**pod_spec):
    return {"spec": {"template": {"spec": pod_spec}}}


@pytest.mark.parametrize(
    "raw, match",
    [
        ([], "mapping"),
        ({**MINIMAL, "apiVersion": "batch/v2"}, "apiVersion"),
        ({**MINIMAL, "kind": "CronJob"}, "kind"),
        ({**MINIMAL, "containers": []}, "top-level"),
        ({"containers": [CONTAINER], "volumes": VOLUMES}, "spec.template.spec"),
        ({"spec": {"containers": [CONTAINER]}}, "non-empty list"),
        ({"spec": {"template": {"containers": [CONTAINER]}}}, "spec.template.spec"),
        ({"spec": []}, "`spec` must be a mapping"),
        ({"spec": {"template": []}}, "`spec.template` must be a mapping"),
        (_pod(), "non-empty list"),
        (_pod(containers=[]), "non-empty list"),
        (_pod(containers=["a"]), r"containers\[0\]` must be a mapping"),
        (_pod(containers=[{"name": "a"}]), "args"),
        ({**MINIMAL, "metadata": {"name": "x"}}, "`metadata`"),
        (
            {
                "spec": {
                    "template": {
                        "metadata": {"namespace": "x"},
                        "spec": {"containers": [CONTAINER]},
                    }
                }
            },
            "spec.template.metadata",
        ),
        ({**MINIMAL, "metadata": {"labels": ["a"]}}, "labels"),
    ],
)
def test_structural_errors(raw, match):
    with pytest.raises(JobSpecError, match=match):
        load_job_spec(raw)


# --- manager-owned fields -----------------------------------------------------


def _with(pod_spec=None, job_spec=None, pod_labels=None, job_labels=None, args=None):
    raw = deepcopy(FULL)
    template = raw["spec"]["template"]
    template["spec"].update(pod_spec or {})
    raw["spec"].update(job_spec or {})
    template["metadata"]["labels"].update(pod_labels or {})
    raw["metadata"]["labels"].update(job_labels or {})
    if args is not None:
        template["spec"]["containers"][0]["args"] = args
    return raw


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(pod_spec={"restartPolicy": "Never"}),
        dict(job_spec={"backoffLimit": 0}),
        dict(job_spec={"parallelism": 1, "completions": 1, "suspend": False}),
        dict(pod_labels={"app": "alchemiscale-compute"}),
        dict(job_labels={"app": "alchemiscale-compute"}),
    ],
)
def test_owned_fields_matching_value_accepted(kwargs):
    _build(_with(**kwargs))  # does not raise


@pytest.mark.parametrize(
    "kwargs, match",
    [
        (
            dict(pod_spec={"restartPolicy": "OnFailure"}),
            "`spec.template.spec.restartPolicy",
        ),
        (dict(job_spec={"backoffLimit": 3}), "`spec.backoffLimit"),
        (dict(job_spec={"parallelism": 2}), "`spec.parallelism"),
        (dict(job_spec={"completions": 5}), "`spec.completions"),
        (dict(job_spec={"suspend": True}), "`spec.suspend"),
        (dict(job_spec={"selector": {}}), "selector"),
        (dict(job_spec={"manualSelector": True}), "manualSelector"),
        (dict(job_spec={"backoffLimitPerIndex": 1}), "backoffLimitPerIndex"),
        (dict(job_spec={"maxFailedIndexes": 1}), "maxFailedIndexes"),
        (dict(pod_labels={"app": "other"}), "`spec.template.metadata.labels.app"),
        (dict(job_labels={"app": "other"}), "`metadata.labels.app"),
        (dict(args=["compute", "--name", "x"]), "--name"),
        (dict(args=["compute", "--compute-manager-id=x"]), "--compute-manager-id"),
    ],
)
def test_owned_fields_conflict_raises(kwargs, match):
    with pytest.raises(JobSpecError, match=match):
        _build(_with(**kwargs))


def test_extra_manager_labels_applied_everywhere():
    labels = {**LABELS, "k8s.alchemiscale.org/compute-manager": "mgr"}
    body = _build(FULL, labels=labels)
    for meta in (body["metadata"], body["spec"]["template"]["metadata"]):
        assert meta["labels"]["k8s.alchemiscale.org/compute-manager"] == "mgr"


# --- validation ---------------------------------------------------------------


def test_validate_clean_spec_has_no_warnings():
    assert validate_job(_build(FULL)) == []
    assert validate_job(_build(MINIMAL)) == []


def test_validate_warns_on_unknown_fields():
    raw = _with(pod_spec={"tolerationz": []})
    raw["spec"]["template"]["spec"]["containers"][0]["imagePullPolice"] = "Always"
    warnings = validate_job(_build(raw))
    assert len(warnings) == 2
    assert any("spec.template.spec.tolerationz" in w for w in warnings)
    assert any("containers[0].imagePullPolice" in w for w in warnings)


@pytest.mark.parametrize(
    "pod_spec, match",
    [
        ({"tolerations": {"key": "a"}}, "tolerations` must be a list"),
        ({"affinity": ["x"]}, "affinity` must be a mapping"),
        ({"nodeSelector": ["x"]}, "nodeSelector` must be a mapping"),
    ],
)
def test_validate_rejects_wrong_shapes(pod_spec, match):
    with pytest.raises(JobSpecError, match=match):
        validate_job(_build(_with(pod_spec=pod_spec)))


def test_validate_rejects_model_errors():
    raw = deepcopy(MINIMAL)
    # missing the required container name
    raw["spec"]["template"]["spec"]["containers"] = [{"image": "x", "args": []}]
    with pytest.raises(JobSpecError, match="name"):
        validate_job(_build(raw))


def _container(**fields):
    return _with(pod_spec={"containers": [{**CONTAINER, **fields}]})


@pytest.mark.parametrize(
    "raw, match",
    [
        # Kubernetes doesn't coerce types, so these fail on every submission
        (
            _container(env=[{"name": "X", "value": 2}]),
            r"env\[0\]\.value` must be str",
        ),
        (_container(args=["compute", 2]), r"args\[1\]` must be str"),
        (
            _container(volumeMounts=[{"name": "v", "mountPath": "/m", "readOnly": "no"}]),
            "readOnly` must be bool",
        ),
        (_with(job_labels={"gpu": True}), "labels.gpu` must be str"),
        (_with(pod_labels={"v": 1}), "labels.v` must be str"),
        (
            _with(job_spec={"activeDeadlineSeconds": "100"}),
            "activeDeadlineSeconds` must be int",
        ),
        (
            _with(job_spec={"ttlSecondsAfterFinished": True}),
            "ttlSecondsAfterFinished` must be int",
        ),
    ],
)
def test_validate_rejects_wrong_types(raw, match):
    with pytest.raises(JobSpecError, match=match):
        validate_job(_build(raw))


def test_validate_accepts_numeric_quantities():
    raw = _container(
        resources={"limits": {"cpu": 2, "memory": 1.5e9}, "requests": {"cpu": 0.5}}
    )
    pod_spec = raw["spec"]["template"]["spec"]
    pod_spec["volumes"] = [{"name": "s", "emptyDir": {"sizeLimit": 1024}}]
    pod_spec["overhead"] = {"cpu": 1}
    assert validate_job(_build(raw)) == []


# --- K8SManager integration ---------------------------------------------------


@pytest.fixture
def make_manager(tmp_path, monkeypatch):
    """Build a K8SManager through its real __init__, with the alchemiscale
    API client replaced so nothing touches the network or the disk cache."""
    monkeypatch.setattr(
        "alchemiscale.compute.manager.AlchemiscaleComputeManagerClient", mock.Mock()
    )
    # upstream __init__ adds a StreamHandler and sets the level on every
    # construction; monkeypatch restores both afterwards
    upstream_logger = logging.getLogger("AlchemiscaleComputeManager")
    monkeypatch.setattr(upstream_logger, "handlers", list(upstream_logger.handlers))
    monkeypatch.setattr(upstream_logger, "level", upstream_logger.level)

    def _make(raw, **settings_overrides):
        path = tmp_path / "job-spec.yaml"
        path.write_text(yaml.safe_dump(raw))
        settings = K8SManagerSettings(
            **{
                "name": "testmgr",
                "logfile": None,
                "max_compute_services": 2,
                "job_spec_path": path,
                "namespace": "alchemiscale",
                **settings_overrides,
            }
        )
        service_settings = ComputeServiceSettings(
            api_url="http://127.0.0.1:1",
            identifier="id",
            key="key",
            name="unused",
            shared_basedir=str(tmp_path / "shared"),
            scratch_basedir=str(tmp_path / "scratch"),
        )
        return K8SManager(settings, service_settings)

    return _make


@pytest.mark.parametrize("raw", [MINIMAL, FULL])
def test_manager_new_job(make_manager, raw):
    manager = make_manager(raw)
    job = manager._new_job()
    name = job["metadata"]["name"]
    assert name.startswith("testmgr.")
    assert job["metadata"]["namespace"] == "alchemiscale"
    assert job["metadata"]["labels"]["app"] == "alchemiscale-compute"
    args = job["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args[-4:] == [
        "--name",
        name,
        "--compute-manager-id",
        str(manager.compute_manager_id),
    ]
    # a second job gets a fresh name and doesn't accumulate args
    job2 = manager._new_job()
    assert job2["metadata"]["name"] != name
    assert len(job2["spec"]["template"]["spec"]["containers"][0]["args"]) == len(args)


def test_manager_rejects_bad_spec_at_startup(make_manager):
    with pytest.raises(JobSpecError, match="restartPolicy"):
        make_manager(_with(pod_spec={"restartPolicy": "Always"}))


def test_manager_logs_unknown_field_warnings(make_manager, caplog):
    with caplog.at_level(logging.WARNING, logger="AlchemiscaleComputeManager"):
        make_manager(_with(pod_spec={"nodeSelecter": {}}))
    assert "nodeSelecter" in caplog.text


@pytest.mark.parametrize("ttl, warns", [(30, True), (60, False), (None, False)])
def test_manager_warns_on_short_ttl(make_manager, caplog, ttl, warns):
    raw = deepcopy(MINIMAL)
    if ttl is not None:
        raw["spec"]["ttlSecondsAfterFinished"] = ttl
    with caplog.at_level(logging.WARNING, logger="AlchemiscaleComputeManager"):
        make_manager(raw, sleep_interval=60)
    assert ("ttlSecondsAfterFinished" in caplog.text) is warns


REPO = pathlib.Path(__file__).parents[4]
README = REPO / "pkg" / "README.md"
DEPLOYMENT_SPEC = REPO / "deployment" / "compute" / "manager-job-spec.yaml"


def _readme_job_specs():
    return re.findall(
        r"```yaml\n(# \./config/job-spec[^\n]*\n.*?)```", README.read_text(), re.S
    )


def test_readme_has_job_spec_examples():
    assert len(_readme_job_specs()) >= 2


@pytest.mark.parametrize("source", ["readme", "deployment"])
def test_shipped_job_specs_validate(source):
    """The job specs in the package README and deployment/ load and validate."""
    if source == "readme":
        texts = _readme_job_specs()
    else:
        texts = [DEPLOYMENT_SPEC.read_text()]
    for text in texts:
        assert validate_job(_build(yaml.safe_load(text))) == []
