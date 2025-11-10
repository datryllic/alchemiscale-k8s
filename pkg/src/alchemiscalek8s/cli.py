import click
import yaml

from alchemiscale.compute.settings import ComputeManagerSettings, ComputeServiceSettings
from alchemiscale.security.models import CredentialedComputeIdentity
from alchemiscalek8s.manager import K8SManager, K8SBatchApi


@click.group()
def cli():
    pass


@cli.group()
def manager():
    pass


@cli.command(name="start")
@click.option(
    "-c",
    "--config-file",
    "config_file",
    type=click.File("r"),
    required=True,
)
@click.option(
    "-s",
    "--service-config-file",
    "service_config_file",
    type=click.File("r"),
    required=True,
)
def manager_start(config_file, service_config_file):
    manager_settings = ComputeManagerSettings(**yaml.safe_load(config_file))
    service_settings = ComputeServiceSettings(**yaml.safe_load(service_config_file))
    manager = K8SManager(manager_settings, service_settings)
    raise NotImplementedError


@cli.group()
def k8s():
    pass


@k8s.command(name="clearjobs")
def k8s_clear_jobs():
    batch_api = K8SBatchApi()
    batch_api.clear_failed_jobs()
