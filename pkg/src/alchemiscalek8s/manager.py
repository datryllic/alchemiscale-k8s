from uuid import uuid4
from time import sleep

from alchemiscale.compute.manager import ComputeManager, ComputeManagerSettings, ComputeServiceSettings

from kubernetes import client, config

class K8SManager(ComputeManager):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.job_registry : list[str] = []
        config.load_kube_config()
        self.batch_api = client.BatchV1Api()

    def create_compute_services(self, data):
        self.create_compute_service()

    def create_compute_service(self):
        self.submit_job(self.new_job())

    def new_job(self):
        # TODO: actually use a compute service
        container = client.V1Container(
            name="service",
            image="bash",
            command=["bash", "-c", "sleep 10"]
        )
        template = client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(labels={"app": "pi"}),
            spec=client.V1PodSpec(restart_policy="Never", containers=[container])
        )
        spec = client.V1JobSpec(
            template=template,
            backoff_limit=0,
            ttl_seconds_after_finished=5,
        )
        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(name=f"testjob-{uuid4()}"),
            spec=spec
        )
        return job

    def submit_job(self, job):
        jobname = job.metadata.name
        # TODO: handle exceptions
        self.batch_api.create_namespaced_job(namespace="default", body=job)
        self.job_registry.append(jobname)
