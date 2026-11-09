"""Loading, validating and rendering the compute Job spec file.

The job spec file (``K8SManagerSettings.job_spec_path``) is a Job template:
an ordinary Kubernetes Job manifest in camelCase YAML, without the Job's
name::

    apiVersion: batch/v1   # optional
    kind: Job              # optional
    metadata:              # optional; Job labels and annotations
      labels: {...}
      annotations: {...}
    spec:                  # any JobSpec field
      activeDeadlineSeconds: 86400
      ttlSecondsAfterFinished: 3600
      template:
        metadata:          # optional; pod labels and annotations
          labels: {...}
        spec:              # any PodSpec field
          containers: [...]
          volumes: [...]
          affinity: {...}

This is the shape of Kubernetes' ``JobTemplateSpec`` (a CronJob's
``jobTemplate``), so a Job written for ``kubectl`` can be used as-is once
its name is removed.

The manager owns a few values and overlays them on every Job (see
:func:`build_job`). A user value for a manager-owned field is accepted only
if it equals the manager's value; any other value raises
:class:`JobSpecError`. Silently overriding these would change behavior the
operator explicitly asked for, and honoring them would break the manager.

The Job body is built as a plain camelCase ``dict`` and submitted as-is, so
fields unknown to the installed ``kubernetes`` client (e.g. newer than it)
still reach the API server. :func:`validate_job_spec` checks the rendered
body against the client's models at startup.
"""

import re
from copy import deepcopy

from kubernetes import client


class JobSpecError(ValueError):
    """The job spec file is malformed or conflicts with manager-owned values."""


_TOP_LEVEL = {"apiVersion": "batch/v1", "kind": "Job"}
_METADATA_KEYS = {"labels", "annotations"}

# manager-owned values; a user value is accepted only if it equals these
POD_SPEC_OWNED = {"restartPolicy": "Never"}
JOB_SPEC_OWNED = {
    # one pod per Job: a retried pod would count toward ``status.failed``,
    # and the manager would delete the Job while it is still retrying
    "backoffLimit": 0,
    # each Job is exactly one compute service with a unique ``--name``
    "parallelism": 1,
    "completions": 1,
    # a suspended Job never runs, and is never seen as pending
    "suspend": False,
}
# JobSpec fields the user may not set at all
JOB_SPEC_FORBIDDEN = {"selector", "manualSelector"}
# args the manager appends to the first container
MANAGER_ARGS = ("--name", "--compute-manager-id")


def load_job_spec(raw) -> dict:
    """Normalize the parsed job spec file.

    Returns a dict with ``metadata``, ``spec``, ``spec.template``,
    ``spec.template.metadata`` and ``spec.template.spec`` all present, and
    without ``apiVersion``/``kind``. Raises :class:`JobSpecError` for
    structural problems.
    """
    if not isinstance(raw, dict):
        raise JobSpecError("job spec must be a YAML mapping")

    raw = deepcopy(raw)
    for key, expected in _TOP_LEVEL.items():
        if raw.pop(key, expected) != expected:
            raise JobSpecError(f"`{key}` must be {expected!r} if set")
    unknown = raw.keys() - {"metadata", "spec"}
    if unknown:
        raise JobSpecError(
            f"unexpected top-level key(s) {sorted(unknown)} in job spec; "
            "the file is a Job manifest: JobSpec fields belong under `spec`, "
            "and PodSpec fields under `spec.template.spec`"
        )

    job_spec = _check_mapping(raw.get("spec"), "spec")
    template = _check_mapping(job_spec.get("template"), "spec.template")
    unknown = template.keys() - {"metadata", "spec"}
    if unknown:
        raise JobSpecError(
            f"unexpected key(s) {sorted(unknown)} under `spec.template`; "
            "PodSpec fields belong under `spec.template.spec`"
        )
    job_spec["template"] = {
        "metadata": _check_metadata(template.get("metadata"), "spec.template.metadata"),
        "spec": _check_mapping(template.get("spec"), "spec.template.spec"),
    }
    spec = {
        "metadata": _check_metadata(raw.get("metadata"), "metadata"),
        "spec": job_spec,
    }

    where = "spec.template.spec.containers"
    containers = job_spec["template"]["spec"].get("containers")
    if not isinstance(containers, list) or not containers:
        raise JobSpecError(f"`{where}` must be a non-empty list")
    if not isinstance(containers[0], dict):
        raise JobSpecError(f"`{where}[0]` must be a mapping")
    if not isinstance(containers[0].get("args"), list):
        raise JobSpecError(
            f"`{where}[0].args` must be a list; the manager appends "
            f"{' and '.join(MANAGER_ARGS)} to it"
        )

    return spec


def _check_mapping(value, where: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise JobSpecError(f"`{where}` must be a mapping")
    return value


def _check_metadata(value, where: str) -> dict:
    value = _check_mapping(value, where)
    unknown = value.keys() - _METADATA_KEYS
    if unknown:
        raise JobSpecError(
            f"`{where}` may only set {sorted(_METADATA_KEYS)}, "
            f"got {sorted(unknown)}; names and namespaces are set by the manager"
        )
    for key in _METADATA_KEYS:
        _check_mapping(value.get(key), f"{where}.{key}")
    return value


def _overlay_owned(user: dict, owned: dict, where: str) -> None:
    """Set ``owned`` values on ``user`` in place, raising on conflict."""
    for key, value in owned.items():
        if key in user and user[key] != value:
            raise JobSpecError(
                f"`{where}.{key}` is managed by alchemiscale-k8s and must be "
                f"{value!r} (or unset), got {user[key]!r}"
            )
        user[key] = value


def build_job(
    job_spec: dict,
    name: str,
    namespace: str,
    labels: dict[str, str],
    extra_args: list[str],
) -> dict:
    """Render a Job body from a normalized job spec.

    Parameters
    ----------
    job_spec
        Output of :func:`load_job_spec`.
    name, namespace
        The Job's name and namespace.
    labels
        Manager-owned labels, applied to both the Job and its pod template.
    extra_args
        Args appended to the first container's args.

    Returns
    -------
    dict
        A camelCase Job body suitable for ``BatchV1Api.create_namespaced_job``.
    """
    job_spec = deepcopy(job_spec)
    job_meta = job_spec["metadata"]
    spec = job_spec["spec"]
    pod_meta = spec["template"]["metadata"]
    pod_spec = spec["template"]["spec"]

    forbidden = JOB_SPEC_FORBIDDEN & spec.keys()
    if forbidden:
        raise JobSpecError(
            f"`spec` may not set {sorted(forbidden)}; the manager relies on "
            "the selector Kubernetes generates"
        )
    _overlay_owned(spec, JOB_SPEC_OWNED, "spec")
    _overlay_owned(pod_spec, POD_SPEC_OWNED, "spec.template.spec")

    args = pod_spec["containers"][0]["args"]
    for arg in args:
        if isinstance(arg, str) and any(
            arg == flag or arg.startswith(flag + "=") for flag in MANAGER_ARGS
        ):
            raise JobSpecError(
                f"`spec.template.spec.containers[0].args` may not contain "
                f"{arg!r}; the manager sets it"
            )
    args.extend(extra_args)

    job_meta["labels"] = job_meta.get("labels") or {}
    pod_meta["labels"] = pod_meta.get("labels") or {}
    _overlay_owned(job_meta["labels"], labels, "metadata.labels")
    _overlay_owned(pod_meta["labels"], labels, "spec.template.metadata.labels")

    job_meta["name"] = name
    job_meta["namespace"] = namespace

    return {**_TOP_LEVEL, "metadata": job_meta, "spec": spec}


def validate_job_spec(
    job_spec: dict,
    namespace: str,
    labels: dict[str, str],
    extra_args: list[str],
) -> list[str]:
    """Render a sample Job and check it against the client's models.

    Raises :class:`JobSpecError` for conflicts with manager-owned values,
    structurally wrong values (e.g. a mapping where a list is expected) and
    anything the ``kubernetes`` client's models reject (e.g. a container
    without a ``name``).

    Returns a list of warnings for fields the installed client does not
    know. These are submitted as-is, since they may be valid on a newer
    cluster, but are most often typos that Kubernetes will drop.
    """
    body = build_job(job_spec, "validation", namespace, labels, extra_args)

    problems = []
    _walk(body, "V1Job", "", problems)
    errors = [msg for kind, msg in problems if kind == "error"]
    if errors:
        raise JobSpecError("invalid job spec: " + "; ".join(errors))

    try:
        client.ApiClient()._ApiClient__deserialize(body, "V1Job")
    except (ValueError, TypeError) as e:
        raise JobSpecError(f"invalid job spec: {e}") from e

    return [msg for kind, msg in problems if kind == "warning"]


_LIST_RE = re.compile(r"^list\[(.+)\]$")
_DICT_RE = re.compile(r"^dict[\[(]([^,]+),\s*(.+)[\])]$")


def _walk(data, type_name: str, path: str, problems: list) -> None:
    """Check ``data`` against kubernetes client model ``type_name``."""
    if data is None:
        return

    m = _LIST_RE.match(type_name)
    if m:
        if not isinstance(data, list):
            problems.append(("error", f"`{path}` must be a list"))
            return
        for i, item in enumerate(data):
            _walk(item, m.group(1), f"{path}[{i}]", problems)
        return

    m = _DICT_RE.match(type_name)
    if m:
        if not isinstance(data, dict):
            problems.append(("error", f"`{path}` must be a mapping"))
            return
        for key, value in data.items():
            _walk(value, m.group(2), f"{path}.{key}", problems)
        return

    klass = getattr(client.models, type_name, None)
    if klass is None or not getattr(klass, "attribute_map", None):
        # primitive, ``object`` or a model without fields
        return

    if not isinstance(data, dict):
        problems.append(("error", f"`{path}` must be a mapping"))
        return

    attrs = {json_key: attr for attr, json_key in klass.attribute_map.items()}
    for key, value in data.items():
        sub_path = f"{path}.{key}" if path else key
        if key not in attrs:
            problems.append(
                (
                    "warning",
                    f"unknown field `{sub_path}` (not in {type_name}); it is "
                    "sent to Kubernetes as-is but may be ignored",
                )
            )
            continue
        _walk(value, klass.openapi_types[attrs[key]], sub_path, problems)
