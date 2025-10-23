import click
import yaml

from alchemiscale.compute.settings import ComputeManagerSettings, ComputeServiceSettings
from alchemiscale.security.models import CredentialedComputeIdentity
from alchemiscalek8s.manager import K8SManager

@click.group()
def cli():
    pass

@cli.command(name="manager")
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
def manager(config_file, service_config_file):
    manager_settings = ComputeManagerSettings(**yaml.safe_load(config_file))
    service_settings = ComputeServiceSettings(**yaml.safe_load(service_config_file))
    manager = K8SManager(manager_settings, service_settings)
    raise NotImplementedError
