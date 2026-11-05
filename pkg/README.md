# alchemiscale-k8s package

## Installation

Dependencies for `alchemiscale-k8s` can be installed using `micromamba` (or other variants with their corresponding command).
The `alchemiscale-k8s` package is currently only installable through its source code.

```shell
micromamba create -n alchemiscale-k8s -f conda-env.yaml
micromamba activate -n alchemiscale-k8s
pip install --no-deps .
```

## Usage

Three components are required using the Kubernetes manager:

1. A `K8SManagerSettings` configuration yaml file
2. Job specification configuration yaml file
3. `ComputeServiceSettings` configuration yaml file

To run the main execution loop, invoke the following:

```shell
alchemiscale-k8s manager start -c manager-config.yaml -s service-config.yaml
```

Currently, a Kubernetes compute manager will not clear failed Jobs, blocking a new compute manager from running from a previous failure.
First, diagnose the cause of the job failures.
Next, run `alchemiscale-k8s k8s clear-jobs` to clear out any failures.
Finally, the Kubernetes compute manager can be rerun.

### `K8SManagerSettings` configuration

```yaml
# manager-config.yaml
name: k8smanager
logfile: null
loglevel: INFO
max_compute_services: 2
sleep_interval: 1800
job_spec_path: ./config/job_spec.yaml
namespace: alchemiscale
k8s_max_retries: 5
k8s_retry_base_seconds: 2.0
k8s_retry_max_seconds: 60.0
```

The `K8SManagerSettings` will be populated from the above configuration.
The `name` field is used to derive the `ComputeManagerID` which is bound to any created `ComputeService` instances.
This value should be unique if creating multiple managers, as only one manager with a given name can exist at a given time.
Note that the compute services created by the Kubernetes manager will have a name with the form `${name}job`.

`logfile` directs log outputs to a path.
If this value is `null` then log outputs are directed to `stderr`.

`loglevel` defines the minimum logging severity level (see [logging levels](https://docs.python.org/3/library/logging.html#levels)).

`max_compute_services` limits the number of created services to a maximum value.

`sleep_interval` defines the number of seconds until the manager requests a new instruction from the compute API.
This is currently the maximum rate the manager can create services.

`job_spec_path` defines the path to a yaml file providing the pod template (and optional Job-level settings) used for each compute service Job.
The structure of this file is discussed below.

`namespace` is the Kubernetes namespace to be used by the compute manager.

`k8s_max_retries`, `k8s_retry_base_seconds`, and `k8s_retry_max_seconds` adjust the behavior of exponential backoff for requests to the Kubernetes API.

### Job specification configuration

The job specification is a pod template for the compute service Jobs, written in ordinary Kubernetes (camelCase) YAML.
If compute services were previously managed manually using a `Deployment`, as demonstrated in the `/deployment/` directory, this is the `template` of that `Deployment`.
It has three top-level sections:

- `metadata` (optional): `labels` and `annotations` for the pods.
- `spec` (required): any [PodSpec](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/#PodSpec) field, such as `containers`, `volumes`, `affinity`, `nodeSelector`, `tolerations`, `imagePullSecrets`, `priorityClassName`, `securityContext` and `initContainers`.
  The first container must be the `alchemiscale-compute` container, and must set `args`.
- `job` (optional): `metadata` with `labels` and `annotations` for the Job itself, and `spec` with any [JobSpec](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/job-v1/#JobSpec) field other than `template`, such as `activeDeadlineSeconds` and `ttlSecondsAfterFinished`.

The values here determine the version of the `alchemiscale-compute` image to use, the resources needed by the container, the volumes created for the pod, and where the pods are scheduled.

Note here that the `alchemiscale-compute-settings-yaml` volume comes from `alchemiscale-compute-settings-yaml` secret.
This secret must exist prior to running the compute manager (see `/compute/secrets.sh`).

```yaml
# ./config/job-spec.yaml
metadata:
  labels:
    team: free-energy
spec:
  containers:
  - name: alchemiscale-synchronous-container
    image: ghcr.io/openforcefield/alchemiscale-compute:v0.7.1
    args: ["compute", "synchronous", "-c", "/mnt/settings/service-config.yaml"]
    resources:
      limits:
        cpu: 2
        memory: 12Gi
        ephemeral-storage: 48Gi
        nvidia.com/gpu: 1
      requests:
        cpu: 2
        memory: 12Gi
        ephemeral-storage: 48Gi
    volumeMounts:
      - name: alchemiscale-compute-settings-yaml
        mountPath: "/mnt/settings"
        readOnly: true
    env:
      - name: OPENMM_CPU_THREADS
        value: "2"
  volumes:
    - name: alchemiscale-compute-settings-yaml
      secret:
        secretName: alchemiscale-compute-settings-yaml
job:
  spec:
    ttlSecondsAfterFinished: 86400
```

The manager owns a few values and sets them on every Job:

- `spec.restartPolicy: Never` and `job.spec.backoffLimit: 0`, plus `job.spec.parallelism: 1`, `job.spec.completions: 1` and `job.spec.suspend: false`: each Job runs exactly one compute service, and a failed pod fails its Job.
- The `app: alchemiscale-compute` label, on both the Job and its pods.
- The Job's `name` and `namespace`.
- `--name <job name>` and `--compute-manager-id <id>`, appended to the first container's `args`.

You may set a manager-owned field only to the manager's own value.
Any other value (e.g. `restartPolicy: OnFailure`, `backoffLimit: 3`, a different `app` label, or `--name` in the container args) is an error.
So are `job.spec.template`, `job.spec.selector` and `job.spec.manualSelector`, and `name` or `namespace` under either `metadata`.
The manager checks the job specification when it starts, and refuses to start if it is invalid.
It logs a warning for any field the installed `kubernetes` client does not recognize, which is usually a typo.
Such fields are still passed through to Kubernetes, in case your cluster is newer than the client.

#### Example: choosing a GPU model on NRP

On the [National Research Platform](https://nrp.ai/) (NRP), the GPU model is chosen with a node affinity on the `nvidia.com/gpu.product` node label.
Tainted nodes, such as nodes reserved for a group, also need a matching toleration.
Adjust the GPU models and the toleration to your cluster.

```yaml
# ./config/job-spec.yaml (NRP)
spec:
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
        - matchExpressions:
          - key: nvidia.com/gpu.product
            operator: In
            values:
            - NVIDIA-A10
            - NVIDIA-GeForce-RTX-3090
  tolerations:
  - key: nautilus.io/reservation
    operator: Equal
    value: alchemiscale
    effect: NoSchedule
  containers:
  - name: alchemiscale-synchronous-container
    image: ghcr.io/openforcefield/alchemiscale-compute:v0.7.1
    args: ["compute", "synchronous", "-c", "/mnt/settings/service-config.yaml"]
    resources:
      limits:
        cpu: 2
        memory: 12Gi
        ephemeral-storage: 48Gi
        nvidia.com/gpu: 1
      requests:
        cpu: 2
        memory: 12Gi
        ephemeral-storage: 48Gi
    volumeMounts:
      - name: alchemiscale-compute-settings-yaml
        mountPath: "/mnt/settings"
        readOnly: true
    env:
      - name: OPENMM_CPU_THREADS
        value: "2"
  volumes:
    - name: alchemiscale-compute-settings-yaml
      secret:
        secretName: alchemiscale-compute-settings-yaml
job:
  spec:
    activeDeadlineSeconds: 604800
    ttlSecondsAfterFinished: 86400
```

#### Legacy form

Earlier versions read only `containers` and `volumes`, placed at the top level of the file.
This form still works: a file with no `metadata`, `spec` or `job` key is treated as the `spec` section.
New files should use the form above.

```yaml
# ./config/job-spec.yaml (legacy form)
containers:
- name: alchemiscale-synchronous-container
  image: ghcr.io/openforcefield/alchemiscale-compute:v0.7.1
  args: ["compute", "synchronous", "-c", "/mnt/settings/service-config.yaml"]
  volumeMounts:
    - name: alchemiscale-compute-settings-yaml
      mountPath: "/mnt/settings"
      readOnly: true
volumes:
  - name: alchemiscale-compute-settings-yaml
    secret:
      secretName: alchemiscale-compute-settings-yaml
```

### `ComputeServiceSettings` configuration

This configuration specifies the settings used by a created compute service.

```yaml
# service-config.yaml
api_url: "http://127.0.0.1:8000"
identifier: myid
key: akey
shared_basedir: "./shared"
scratch_basedir: "./scratch"
claim_limit: 2
max_tasks: null   # max Tasks to execute before exiting; null = no limit
max_time: null    # max seconds to run before exiting; null = no limit
```

Importantly, the manager uses the `api_url`, `identifier`, and `key` fields for communication with the alchemiscale compute API.
The manager will disregard `identifier` here, instead replacing it with an `identifier` generated from its own `name` field.
