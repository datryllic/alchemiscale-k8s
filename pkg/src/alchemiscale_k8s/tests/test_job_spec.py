"""Tests for job spec loading, validation and Job rendering (no cluster needed)."""

import logging
from copy import deepcopy

import pytest
import yaml
from alchemiscale.compute.settings import ComputeServiceSettings
from kubernetes import client

from alchemiscale_k8s.job_spec import (
    JobSpecError,
    build_job,
    load_job_spec,
    validate_job_spec,
)
from alchemiscale_k8s.manager import K8SManager
from alchemiscale_k8s.settings import K8SManagerSettings

LABELS = {"app": "alchemiscale-compute"}
ARGS = ["--name", "mgr.abc", "--compute-manager-id", "mgr-id"]

CONTAINER = {
    "name": "alchemiscale-synchronous-container",
    "image": "ghcr.io/openforcefield/alchemiscale-compute:v0.7.1",
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

LEGACY = {"containers": [CONTAINER], "volumes": VOLUMES}

NEW = {
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
    "job": {
        "metadata": {"labels": {"cost-center": "x"}, "annotations": {"a": "b"}},
        "spec": {"activeDeadlineSeconds": 86400, "ttlSecondsAfterFinished": 3600},
    },
}


def _build(raw, labels=LABELS, args=ARGS):
    return build_job(load_job_spec(raw), "mgr.abc", "ns", dict(labels), list(args))


def _deserialize(body) -> client.V1Job:
    return client.ApiClient()._ApiClient__deserialize(body, "V1Job")


# --- forms --------------------------------------------------------------------


def test_legacy_form():
    body = _build(LEGACY)
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


def test_legacy_form_extra_podspec_fields():
    raw = dict(LEGACY, nodeSelector={"gpu": "yes"})
    body = _build(raw)
    assert body["spec"]["template"]["spec"]["nodeSelector"] == {"gpu": "yes"}


def test_new_form_keeps_all_fields():
    body = _build(NEW)
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


def test_legacy_and_new_forms_equivalent():
    new = {"spec": deepcopy(LEGACY)}
    assert _build(LEGACY) == _build(new)


def test_body_is_serializable_like_typed_model():
    """The dict body survives a round trip through the client models."""
    body = _build(NEW)
    roundtrip = client.ApiClient().sanitize_for_serialization(_deserialize(body))
    # resource quantities come back as strings
    expected = deepcopy(body)
    for section in ("limits", "requests"):
        res = expected["spec"]["template"]["spec"]["containers"][0]["resources"]
        res[section] = {k: str(v) for k, v in res[section].items()}
    assert roundtrip == expected


def test_build_does_not_mutate_spec():
    spec = load_job_spec(NEW)
    snapshot = deepcopy(spec)
    build_job(spec, "a", "ns", dict(LABELS), list(ARGS))
    build_job(spec, "b", "ns", dict(LABELS), list(ARGS))
    assert spec == snapshot


def test_load_does_not_mutate_input():
    raw = deepcopy(LEGACY)
    _build(raw)
    assert raw == LEGACY


# --- structural errors --------------------------------------------------------


@pytest.mark.parametrize(
    "raw, match",
    [
        ([], "mapping"),
        ({"spec": {"containers": [CONTAINER]}, "containers": []}, "top-level"),
        ({"spec": {}}, "non-empty list"),
        ({"spec": {"containers": []}}, "non-empty list"),
        ({"spec": {"containers": [{"name": "a"}]}}, "args"),
        ({"spec": {"containers": [CONTAINER]}, "job": {"activeDeadlineSeconds": 1}}, "job.spec"),
        ({"metadata": {"name": "x"}, "spec": {"containers": [CONTAINER]}}, "metadata"),
        (
            {"spec": {"containers": [CONTAINER]}, "job": {"metadata": {"namespace": "x"}}},
            "job.metadata",
        ),
        ({"metadata": {"labels": ["a"]}, "spec": {"containers": [CONTAINER]}}, "labels"),
    ],
)
def test_structural_errors(raw, match):
    with pytest.raises(JobSpecError, match=match):
        load_job_spec(raw)


# --- manager-owned fields -----------------------------------------------------


def _with(pod_spec=None, job_spec=None, pod_labels=None, job_labels=None, args=None):
    raw = deepcopy(NEW)
    raw["spec"].update(pod_spec or {})
    raw["job"]["spec"].update(job_spec or {})
    raw["metadata"]["labels"].update(pod_labels or {})
    raw["job"]["metadata"]["labels"].update(job_labels or {})
    if args is not None:
        raw["spec"]["containers"][0]["args"] = args
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
    body = _build(_with(**kwargs))
    assert body["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    assert body["spec"]["backoffLimit"] == 0


@pytest.mark.parametrize(
    "kwargs, match",
    [
        (dict(pod_spec={"restartPolicy": "OnFailure"}), "spec.restartPolicy"),
        (dict(job_spec={"backoffLimit": 3}), "job.spec.backoffLimit"),
        (dict(job_spec={"parallelism": 2}), "job.spec.parallelism"),
        (dict(job_spec={"completions": 5}), "job.spec.completions"),
        (dict(job_spec={"suspend": True}), "job.spec.suspend"),
        (dict(job_spec={"template": {}}), "template"),
        (dict(job_spec={"selector": {}}), "selector"),
        (dict(job_spec={"manualSelector": True}), "manualSelector"),
        (dict(pod_labels={"app": "other"}), "metadata.labels.app"),
        (dict(job_labels={"app": "other"}), "job.metadata.labels.app"),
        (dict(args=["compute", "--name", "x"]), "--name"),
        (dict(args=["compute", "--compute-manager-id=x"]), "--compute-manager-id"),
    ],
)
def test_owned_fields_conflict_raises(kwargs, match):
    with pytest.raises(JobSpecError, match=match):
        _build(_with(**kwargs))


def test_extra_manager_labels_applied_everywhere():
    labels = {**LABELS, "alchemiscale.org/compute-manager": "mgr"}
    body = _build(NEW, labels=labels)
    for meta in (body["metadata"], body["spec"]["template"]["metadata"]):
        assert meta["labels"]["alchemiscale.org/compute-manager"] == "mgr"


# --- validation ---------------------------------------------------------------


def test_validate_clean_spec_has_no_warnings():
    assert validate_job_spec(load_job_spec(NEW), "ns", LABELS, ARGS) == []
    assert validate_job_spec(load_job_spec(LEGACY), "ns", LABELS, ARGS) == []


def test_validate_warns_on_unknown_fields():
    raw = _with(pod_spec={"tolerationz": []})
    raw["spec"]["containers"][0]["imagePullPolice"] = "Always"
    warnings = validate_job_spec(load_job_spec(raw), "ns", LABELS, ARGS)
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
        validate_job_spec(load_job_spec(_with(pod_spec=pod_spec)), "ns", LABELS, ARGS)


def test_validate_rejects_model_errors():
    raw = deepcopy(LEGACY)
    raw["containers"] = [{"image": "x", "args": []}]  # missing required name
    with pytest.raises(JobSpecError, match="name"):
        validate_job_spec(load_job_spec(raw), "ns", LABELS, ARGS)


# --- K8SManager integration ---------------------------------------------------


@pytest.fixture
def make_manager(tmp_path):
    def _make(raw):
        path = tmp_path / "job-spec.yaml"
        path.write_text(yaml.safe_dump(raw))
        settings = K8SManagerSettings(
            name="testmgr",
            logfile=None,
            max_compute_services=2,
            job_spec_path=path,
            namespace="alchemiscale",
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


@pytest.mark.parametrize("raw", [LEGACY, NEW])
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


def test_shipped_readme_examples_validate():
    """The job spec examples in the package README load and validate."""
    import pathlib
    import re

    readme = pathlib.Path(__file__).parents[3] / "README.md"
    blocks = re.findall(r"```yaml\n(# \./config/job-spec[^\n]*\n.*?)```", readme.read_text(), re.S)
    assert len(blocks) >= 2
    for block in blocks:
        spec = load_job_spec(yaml.safe_load(block))
        assert validate_job_spec(spec, "ns", LABELS, ARGS) == []
