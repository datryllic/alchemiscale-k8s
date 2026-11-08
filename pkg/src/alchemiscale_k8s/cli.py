import click
import yaml

from alchemiscale.compute.settings import ComputeServiceSettings

from alchemiscale_k8s.manager import K8SManager, K8SBatchApi, manager_label_selector
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
    from alchemiscale.compute.signals import install_stop_handlers

    manager_settings = K8SManagerSettings(**yaml.safe_load(config_file))

    service_settings_ = yaml.safe_load(service_config_file)
    service_settings = ComputeServiceSettings(**service_settings_)

    manager = K8SManager(manager_settings, service_settings)

    # install handlers so SIGHUP/SIGINT/SIGTERM (e.g. K8s pod termination)
    # stop the manager cleanly
    install_stop_handlers(manager)

    try:
        manager.start(steal=steal)
    except KeyboardInterrupt:
        pass


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
    service_settings = ComputeServiceSettings(**service_settings_)

    manager = K8SManager(manager_settings, service_settings)
    manager.clear_error()


@cli.group()
def k8s():
    pass


@k8s.command(name="clear-jobs")
@click.option(
    "-n",
    "--namespace",
    "namespace",
    type=str,
    required=True,
    default=None,
)
@click.option(
    "-m",
    "--manager-name",
    "manager_name",
    type=str,
    default=None,
    help=(
        "Only clear failed Jobs created by the manager with this name. "
        "If omitted, clear failed Jobs created by any alchemiscale-k8s manager "
        "(Jobs carrying the alchemiscale.org/compute-manager label); "
        "other Jobs in the namespace are never touched."
    ),
)
def k8s_clear_jobs(namespace, manager_name):
    """Delete failed Jobs created by alchemiscale-k8s managers."""
    batch_api = K8SBatchApi(
        namespace, label_selector=manager_label_selector(manager_name)
    )
    for name, reason in batch_api.clear_failed_jobs().items():
        click.echo(f"Deleted failed Job `{name}`: {reason}")
