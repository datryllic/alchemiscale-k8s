"""Loading, validating and rendering the compute Job spec file.

The job spec file (``K8SManagerSettings.job_spec_path``) is a pod template,
written in ordinary Kubernetes camelCase YAML::

    metadata:          # optional; pod template labels and annotations
      labels: {...}
      annotations: {...}
    spec:              # required; any PodSpec field
      containers: [...]
      volumes: [...]
      affinity: {...}
      tolerations: [...]
    job:               # optional; Job-level settings
      metadata:        # optional; Job labels and annotations
        labels: {...}
        annotations: {...}
      spec:            # optional; any JobSpec field except ``template``
        activeDeadlineSeconds: 86400
        ttlSecondsAfterFinished: 3600

The legacy form, with PodSpec fields such as ``containers`` and ``volumes``
at the top level, is still accepted and treated as ``spec``.

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


_TOP_LEVEL_KEYS = {"metadata", "spec", "job"}
_METADATA_KEYS = {"labels", "annotations"}

# manager-owned values; a user value is accepted only if it equals these
POD_SPEC_OWNED = {"restartPolicy": "Never"}
JOB_SPEC_OWNED = {
    # retried pods would count toward ``status.failed`` and trip the
    # manager's Job health check while the Job is still running
    "backoffLimit": 0,
    # each Job is exactly one compute service with a unique ``--name``
    "parallelism": 1,
    "completions": 1,
    # a suspended Job never runs, and is never seen as pending
    "suspend": False,
}
# JobSpec fields the user may not set at all
JOB_SPEC_FORBIDDEN = {"template", "selector", "manualSelector"}
# args the manager appends to the first container
MANAGER_ARGS = ("--name", "--compute-manager-id")


def load_job_spec(raw) -> dict:
    """Normalize the parsed job spec file into the pod template form.

    Returns a dict with ``metadata``, ``spec`` and ``job`` (itself with
    ``metadata`` and ``spec``), all present. Raises :class:`JobSpecError` for
    structural problems.
    """
    if not isinstance(raw, dict):
        raise JobSpecError("job spec must be a YAML mapping")

    raw = deepcopy(raw)
    if not (_TOP_LEVEL_KEYS & raw.keys()):
        # legacy form: top-level PodSpec fields (containers, volumes, ...)
        raw = {"spec": raw}

    unknown = raw.keys() - _TOP_LEVEL_KEYS
    if unknown:
        raise JobSpecError(
            f"unexpected top-level key(s) {sorted(unknown)} in job spec; "
            "PodSpec fields such as `containers` belong under `spec` "
            "(top-level `containers` is only accepted when none of "
            f"{sorted(_TOP_LEVEL_KEYS)} are present)"
        )

    job = raw.get("job") or {}
    if not isinstance(job, dict):
        raise JobSpecError("`job` must be a mapping")
    unknown = job.keys() - {"metadata", "spec"}
    if unknown:
        raise JobSpecError(
            f"unexpected key(s) {sorted(unknown)} under `job`; "
            "JobSpec fields belong under `job.spec`"
        )

    spec = {
        "metadata": _check_metadata(raw.get("metadata"), "metadata"),
        "spec": _check_mapping(raw.get("spec"), "spec"),
        "job": {
            "metadata": _check_metadata(job.get("metadata"), "job.metadata"),
            "spec": _check_mapping(job.get("spec"), "job.spec"),
        },
    }

    containers = spec["spec"].get("containers")
    if not isinstance(containers, list) or not containers:
        raise JobSpecError("`spec.containers` must be a non-empty list")
    if not isinstance(containers[0], dict):
        raise JobSpecError("`spec.containers[0]` must be a mapping")
    args = containers[0].get("args")
    if not isinstance(args, list):
        raise JobSpecError(
            "`spec.containers[0].args` must be a list; the manager appends "
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
    pod_meta = job_spec["metadata"]
    pod_spec = job_spec["spec"]
    job_meta = job_spec["job"]["metadata"]
    job_body_spec = job_spec["job"]["spec"]

    _overlay_owned(pod_spec, POD_SPEC_OWNED, "spec")

    forbidden = JOB_SPEC_FORBIDDEN & job_body_spec.keys()
    if forbidden:
        raise JobSpecError(
            f"`job.spec` may not set {sorted(forbidden)}; the manager builds "
            "the pod template from `metadata`/`spec`"
        )
    _overlay_owned(job_body_spec, JOB_SPEC_OWNED, "job.spec")

    args = pod_spec["containers"][0]["args"]
    for arg in args:
        if isinstance(arg, str) and any(
            arg == flag or arg.startswith(flag + "=") for flag in MANAGER_ARGS
        ):
            raise JobSpecError(
                f"`spec.containers[0].args` may not contain {arg!r}; "
                "the manager sets it"
            )
    args.extend(extra_args)

    pod_meta["labels"] = pod_meta.get("labels") or {}
    job_meta["labels"] = job_meta.get("labels") or {}
    _overlay_owned(pod_meta["labels"], labels, "metadata.labels")
    _overlay_owned(job_meta["labels"], labels, "job.metadata.labels")

    job_meta["name"] = name
    job_meta["namespace"] = namespace
    job_body_spec["template"] = {"metadata": pod_meta, "spec": pod_spec}

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": job_meta,
        "spec": job_body_spec,
    }


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
