import click
import yaml

from alchemiscale.compute.settings import ComputeServiceSettings

from alchemiscale_k8s.manager import K8SManager, K8SBatchApi
from alchemiscale_k8s.settings import K8SManagerSettings


@click.group()
def cli():
    pass


@cli.group()
def manager():
    pass


@manager.command(name="start")
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
@click.option(
    "--steal",
    "steal",
    is_flag=True,
)
def manager_start(config_file, service_config_file, steal):
    manager_settings = K8SManagerSettings(**yaml.safe_load(config_file))

    service_settings_ = yaml.safe_load(service_config_file)
    service_settings = ComputeServiceSettings(**service_settings_["init"])

    manager = K8SManager(manager_settings, service_settings)
    manager.start(steal=steal)


@manager.command(name="clear-error")
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
def manager_clear_error(config_file, service_config_file):
    manager_settings = K8SManagerSettings(**yaml.safe_load(config_file))

    service_settings_ = yaml.safe_load(service_config_file)
    service_settings = ComputeServiceSettings(**service_settings_["init"])

    manager = K8SManager(manager_settings, service_settings)
    manager.clear_error()


@cli.group()
def k8s():
    pass


@k8s.command(name="clearjobs")
@click.option(
    "-n",
    "--namespace",
    "namespace",
    type=str,
    required=True,
    default=None,
)
def k8s_clear_jobs(namespace):
    batch_api = K8SBatchApi(namespace)
    batch_api.clear_failed_jobs()
