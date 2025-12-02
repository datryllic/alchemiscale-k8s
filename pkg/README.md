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

`job_spec_path` defines the path to a yaml file providing container and volume structures used by the Kubernetes API.
The structure of this file is discussed below.

`namespace` is the Kubernetes namespace to be used by the compute manager.

`k8s_max_retries`, `k8s_retry_base_seconds`, and `k8s_retry_max_seconds` adjust the behavior of exponential backoff for requests to the Kubernetes API.

### Job specification configuration

The definition of containers and volumes to be used for a compute service is contained in the job specification.
If compute services were previously managed manually using a `Deployment`, as demonstrated in the `/deployment/` directory, this is the `template` spec of the compute service configuration.
The values here determine the version of the `alchemiscale-compute` image to use, the resources needed by the container, and volumes created created for the pod.

Note here that the `alchemiscale-compute-settings-yaml` volume comes from `alchemiscale-compute-settings-yaml` secret.
This secret must exist prior to running the compute manager (see `/compute/secrets.sh`).

```yaml
# ./config/job-spec.yaml
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
      value: "4"
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
```

Importantly, the manager uses the `api_url`, `identifier`, and `key` fields for communication with the alchemiscale compute API.
The manager will disregard `identifier` here, instead replacing it with an `identifier` generated from its own `name` field.
