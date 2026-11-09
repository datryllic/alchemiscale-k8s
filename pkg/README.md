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

Failed Jobs (e.g. from evicted, preempted or OOM-killed pods) are logged with their failure reason and deleted, and the manager keeps running.
This check runs only in cycles where the manager creates compute services, i.e. when there are tasks to run and capacity to spare.
If failed Jobs are found in more than `max_consecutive_failed_cycles` consecutive such cycles, the manager stops with a `JobFailureError`, since this usually indicates a bad image, job spec or compute service settings.
In that case, diagnose the cause of the job failures, run `alchemiscale-k8s k8s clear-jobs --namespace <namespace> --manager-name <name>` to clear out that manager's remaining failed Jobs, and rerun the Kubernetes compute manager.

### Job ownership

Each Job (and its pods) created by a manager is labeled `k8s.alchemiscale.org/compute-manager: <name>`, where `<name>` is the manager's `name` setting.
A manager only inspects, waits on, and deletes Jobs carrying its own label, so it can share a namespace with other workloads and with other managers.
`alchemiscale-k8s k8s clear-jobs` likewise only touches the named manager's Jobs.

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
max_consecutive_failed_cycles: 5
```

The `K8SManagerSettings` will be populated from the above configuration.
The `name` field is used to derive the `ComputeManagerID` which is bound to any created `ComputeService` instances.
This value should be unique if creating multiple managers, as only one manager with a given name can exist at a given time.
Note that the compute services created by the Kubernetes manager will have a name with the form `${name}job`.

`name` must start with a lowercase letter, contain only lowercase letters, digits, and periods (`.`), with no leading, trailing, or consecutive periods (e.g. `nrp.omsf`), and be at most 30 characters long.
The manager names each Kubernetes Job `{name}.{32-character hex uuid}`, so these rules keep Job names valid for Kubernetes (lowercase DNS-1123 subdomains, at most 63 characters because the Job name is copied into the `job-name` pod label) and for alchemiscale compute IDs (which disallow `-`).
Invalid names are rejected when the settings are loaded.

`logfile` directs log outputs to a path.
If this value is `null` then log outputs are directed to `stderr`.

`loglevel` defines the minimum logging severity level (see [logging levels](https://docs.python.org/3/library/logging.html#levels)).

`max_compute_services` limits the number of created services to a maximum value.

`sleep_interval` defines the number of seconds until the manager requests a new instruction from the compute API.
This is currently the maximum rate the manager can create services.

`job_spec_path` defines the path to a yaml file with the Job template (a Job manifest without its name) used for each compute service Job.
The structure of this file is discussed below.

`namespace` is the Kubernetes namespace to be used by the compute manager.

`k8s_max_retries`, `k8s_retry_base_seconds`, and `k8s_retry_max_seconds` adjust the behavior of exponential backoff for requests to the Kubernetes API.

`max_consecutive_failed_cycles` sets how many consecutive cycles may find failed Jobs before the manager stops with a `JobFailureError`; only cycles in which the manager creates compute services count, and any such cycle without failed Jobs resets the count.
Set it to `0` to stop on the first failed Job, or to `null` to never stop on failed Jobs.
Reading pod termination reasons for the logs requires `list` on `pods` in the namespace; without it, the logged reason notes that pods were unavailable.

### Job specification configuration

The job specification is a template for the compute service Jobs: an ordinary Kubernetes [Job](https://kubernetes.io/docs/concepts/workloads/controllers/job/) manifest, without the Job's name.
It has the same shape as a CronJob's `jobTemplate`:

- `apiVersion: batch/v1` and `kind: Job` (optional): accepted so that a Job written for `kubectl` can be reused, once everything under `metadata` other than `labels` and `annotations` is removed.
- `metadata` (optional): `labels` and `annotations` for the Job.
- `spec`: any [JobSpec](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/job-v1/#JobSpec) field, such as `activeDeadlineSeconds` and `ttlSecondsAfterFinished`, plus the pod template:
  - `spec.template.metadata` (optional): `labels` and `annotations` for the pods.
  - `spec.template.spec`: any [PodSpec](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/#PodSpec) field, such as `containers`, `volumes`, `affinity`, `nodeSelector`, `tolerations`, `imagePullSecrets`, `priorityClassName`, `securityContext` and `initContainers`.
    The first container must be the `alchemiscale-compute` container, and must set `args`.

If you set `ttlSecondsAfterFinished`, keep it longer than the manager's `sleep_interval`.
Otherwise Kubernetes may delete failed Jobs before the manager logs them; the manager warns at startup if it is shorter.

If compute services were previously managed manually using a `Deployment`, as demonstrated in the `/deployment/` directory, that `Deployment`'s `template` goes under `spec.template` here.
The values here determine the version of the `alchemiscale-compute` image to use, the resources needed by the container, the volumes created for the pod, and where the pods are scheduled.

The image tags in these examples are placeholders (`vX.Y.Z`); use the `alchemiscale-compute` image matching the version of the `alchemiscale` server you connect to.

Note here that the `alchemiscale-compute-settings-yaml` volume comes from `alchemiscale-compute-settings-yaml` secret.
This secret must exist prior to running the compute manager (see `/compute/secrets.sh`).

```yaml
# ./config/job-spec.yaml
apiVersion: batch/v1
kind: Job
spec:
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels:
        team: free-energy
    spec:
      containers:
        - name: alchemiscale-synchronous-container
          image: ghcr.io/openfreeenergy/alchemiscale-compute:vX.Y.Z
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
```

The manager owns a few values and sets them on every Job:

- `spec.backoffLimit: 0`, `spec.parallelism: 1`, `spec.completions: 1`, `spec.suspend: false` and `spec.template.spec.restartPolicy: Never`: each Job runs exactly one pod, which runs one compute service, and a failed pod fails its Job.
- The `app: alchemiscale-compute` and `k8s.alchemiscale.org/compute-manager: <name>` labels, on both the Job and its pods.
- The Job's `name` and `namespace`.
- `--name <job name>` and `--compute-manager-id <id>`, appended to the first container's `args`.

You may set a manager-owned field only to the manager's own value.
Any other value (e.g. `restartPolicy: OnFailure`, `backoffLimit: 3`, a different `app` label, or `--name` in the container args) is an error.
So are `spec.selector`, `spec.manualSelector`, `spec.backoffLimitPerIndex` and `spec.maxFailedIndexes`, and any key other than `labels` and `annotations` under either `metadata`.
The manager checks the job specification when it starts, and refuses to start if it is invalid, including values of the wrong type (e.g. an env var `value: 2`, which must be quoted as `"2"`).
It logs a warning for any field the installed `kubernetes` client does not recognize, which is usually a typo.
Such fields are still passed through to Kubernetes, in case your cluster is newer than the client.

#### Example: choosing a GPU model on NRP

On the [National Research Platform](https://nrp.ai/) (NRP), the GPU model is chosen with a node affinity on the `nvidia.com/gpu.product` node label.
Tainted nodes, such as nodes reserved for a group, also need a matching toleration.
A toleration only allows the pods onto those nodes; use an affinity or `nodeSelector` to require them.
Adjust the GPU models and the toleration to your cluster.

```yaml
# ./config/job-spec.yaml (NRP)
apiVersion: batch/v1
kind: Job
spec:
  activeDeadlineSeconds: 604800
  ttlSecondsAfterFinished: 86400
  template:
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
      # allow pods onto nodes tainted `nautilus.io/reservation=alchemiscale`
      # (any effect); placeholder: replace with your reservation's taint, as
      # listed by `kubectl describe node <node>`
      tolerations:
      - key: nautilus.io/reservation
        operator: Equal
        value: alchemiscale
      containers:
        - name: alchemiscale-synchronous-container
          image: ghcr.io/openfreeenergy/alchemiscale-compute:vX.Y.Z
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

## Deploying as a container in Kubernetes

The manager can run as a pod inside the cluster it manages.
It then authenticates to the Kubernetes API with the pod's ServiceAccount.
Example manifests are in [`/deployment/compute/`](../deployment/compute/).
They assume the `alchemiscale` namespace; run the commands below from that directory.

### Create a service account

The namespace's `default` ServiceAccount cannot manage Jobs, so the manager needs its own.
[`manager-rbac.yaml`](../deployment/compute/manager-rbac.yaml) defines:

- a `ServiceAccount` named `alchemiscale-k8s-manager`;
- a `Role` granting `get`, `list`, `watch`, `create` and `delete` on `batch` `jobs`, plus `get` and `list` on `pods` (read-only, for inspecting the pods of failed Jobs);
- a `RoleBinding` granting that `Role` to the `ServiceAccount`.

```shell
kubectl apply -f manager-rbac.yaml
```

If the manager creates Jobs in a namespace other than `alchemiscale`, change the `namespace` of all three resources to match the `namespace` field of the manager settings.

### Create Kubernetes Deployment

The manager pod needs the three configuration files described above:

1. `K8SManagerSettings`: [`manager-settings.yaml`](../deployment/compute/manager-settings.yaml).
   Its `job_spec_path` points at the Job spec mounted in the pod, `/mnt/manager/manager-job-spec.yaml`.
2. Job specification: [`manager-job-spec.yaml`](../deployment/compute/manager-job-spec.yaml).
3. `ComputeServiceSettings`: [`synchronous-compute-settings.yaml`](../deployment/compute/synchronous-compute-settings.yaml).
   This file contains the compute identity's key, so it is stored in the `alchemiscale-compute-settings-yaml` Secret.
   The manager and the Jobs it creates both mount this Secret.

Create the Secret, then a ConfigMap holding the manager settings and Job spec:

```shell
bash secrets.sh
bash manager-configmap.sh
```

Re-run these scripts after editing any of the files, then restart the manager with `kubectl -n alchemiscale rollout restart deployment/alchemiscale-k8s-manager`.

Then deploy the manager:

```shell
kubectl apply -f manager-deployment.yaml
```

[`manager-deployment.yaml`](../deployment/compute/manager-deployment.yaml) runs `alchemiscale-k8s manager start -c /mnt/manager/manager-settings.yaml -s /mnt/settings/synchronous-compute-settings.yaml` as the `alchemiscale-k8s-manager` ServiceAccount.
Its `image` is a placeholder: set it to your own image with `alchemiscale-k8s` installed (see [Installation](#installation)).
No image is published for the manager, since what else it needs installed depends on your deployment.
Other details:

- It runs a single replica with `strategy: Recreate`, so the old manager pod stops before a new one starts and two managers with the same `name` never overlap.
- The manager handles `SIGTERM` by stopping its loop and deregistering from the compute API.
  `terminationGracePeriodSeconds: 120` gives that request time to retry.
- If a manager pod is killed without deregistering (for example, it is OOMKilled), its replacement cannot register under the same `name`.
  Add `--steal` to the container's `args` to take over the stale registration.
