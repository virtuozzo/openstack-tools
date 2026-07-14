#!/usr/bin/env python3
"""Delete common OpenStack resources from one project using openstacksdk."""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator


EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

DEFAULT_COMMAND_TIMEOUT = 120
DEFAULT_WAIT_TIMEOUT = 600
DEFAULT_WAIT_INTERVAL = 5.0


@dataclass(frozen=True)
class ResourceKind:
    name: str
    label_fields: tuple[str, ...]
    include_resource: Callable[[dict[str, object]], bool] = lambda _resource: True
    delete_identifier_fields: tuple[str, ...] = ("ID",)


class CleanupError(RuntimeError):
    """Recoverable cleanup failure for one resource group."""


class FailFastError(CleanupError):
    """Abort remaining deletions after the first failure."""


class UsageError(Exception):
    """Invalid CLI usage / non-interactive misuse."""


@dataclass
class RuntimeOptions:
    quiet: bool = False
    fail_fast: bool = False
    wait: bool = True
    wait_timeout: int = DEFAULT_WAIT_TIMEOUT
    wait_interval: float = DEFAULT_WAIT_INTERVAL
    command_timeout: int = DEFAULT_COMMAND_TIMEOUT
    github: bool = False


SPINNER_FRAMES = "⠋⠙⠹⠼⠼⠴⠦⠧⠇⠏"


def is_ci() -> bool:
    for key in ("CI", "GITHUB_ACTIONS"):
        if os.environ.get(key, "").strip().casefold() in {"1", "true", "yes"}:
            return True
    return False


def is_noninteractive() -> bool:
    return is_ci() or not sys.stdin.isatty()


def use_github_output(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return explicit
    return os.environ.get("GITHUB_ACTIONS", "").strip().casefold() in {
        "1",
        "true",
        "yes",
    }


def github_escape(message: str) -> str:
    return (
        message.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def emit_github_annotation(level: str, message: str, options: RuntimeOptions) -> None:
    if not options.github:
        return
    print(f"::{level}::{github_escape(message)}", file=sys.stderr)


def log_info(message: str, options: RuntimeOptions, *, force: bool = False) -> None:
    if options.quiet and not force:
        return
    print(message)


def log_error(message: str, options: RuntimeOptions) -> None:
    emit_github_annotation("error", message, options)
    print(message, file=sys.stderr)


def log_warning(message: str, options: RuntimeOptions) -> None:
    emit_github_annotation("warning", message, options)
    print(message, file=sys.stderr)


class ProgressIndicator:
    def __init__(self) -> None:
        self._message = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._interactive = sys.stderr.isatty()

    def start(self, message: str) -> None:
        self._message = message
        if not self._interactive:
            print(message, file=sys.stderr)
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def update(self, message: str) -> None:
        self._message = message
        if not self._interactive:
            print(message, file=sys.stderr)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None
        if self._interactive:
            sys.stderr.write("\r" + " " * 80 + "\r")
            sys.stderr.flush()

    def _run(self) -> None:
        index = 0
        while not self._stop.is_set():
            frame = SPINNER_FRAMES[index % len(SPINNER_FRAMES)]
            sys.stderr.write(f"\r{frame} {self._message}")
            sys.stderr.flush()
            index += 1
            self._stop.wait(0.1)


@contextmanager
def progress(message: str) -> Iterator[ProgressIndicator]:
    indicator = ProgressIndicator()
    indicator.start(message)
    try:
        yield indicator
    finally:
        indicator.stop()


@dataclass
class ResourceGroup:
    kind: ResourceKind
    to_delete: list[dict[str, object]] = field(default_factory=list)
    skipped: list[dict[str, object]] = field(default_factory=list)


def normalized_field_name(field: str) -> str:
    return "".join(ch for ch in field.casefold() if ch.isalnum())


def resource_value(resource: dict[str, object], field: str) -> object:
    wanted = normalized_field_name(field)
    for key, value in resource.items():
        if normalized_field_name(str(key)) == wanted:
            return value
    return ""


def normalized_resource_value(resource: dict[str, object], field: str) -> str:
    return str(resource_value(resource, field)).strip().casefold()


def first_normalized_resource_value(
    resource: dict[str, object],
    fields: tuple[str, ...],
) -> str:
    for field in fields:
        value = normalized_resource_value(resource, field)
        if value:
            return value
    return ""


def is_false_value(value: object) -> bool:
    if isinstance(value, bool):
        return not value
    return str(value).strip().casefold() in {"false", "no", "0"}


def is_true_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"true", "yes", "1"}


def is_internal_network(resource: dict[str, object]) -> bool:
    for field in ("Router Type", "router_type"):
        router_type_raw = resource_value(resource, field)
        if router_type_raw == "":
            continue
        if isinstance(router_type_raw, bool):
            return not router_type_raw
        router_type = str(router_type_raw).strip().casefold()
        return router_type == "internal"

    router_external = resource_value(resource, "router:external")
    if router_external != "":
        return is_false_value(router_external)

    return False


def is_not_shared_network(resource: dict[str, object]) -> bool:
    for field in ("Shared", "shared"):
        value = resource_value(resource, field)
        if value != "":
            return is_false_value(value)
    return False


def network_is_cleanup_candidate(resource: dict[str, object]) -> bool:
    name = normalized_resource_value(resource, "Name")
    if name == "public":
        return False

    network_type = first_normalized_resource_value(
        resource,
        ("Network Type", "Provider Network Type", "provider:network_type"),
    )

    if network_type != "vxlan":
        return False

    return is_internal_network(resource) and is_not_shared_network(resource)


def is_router_owned_port(resource: dict[str, object]) -> bool:
    return normalized_resource_value(resource, "Device Owner").startswith(
        "network:router"
    )


def is_compute_owned_port(resource: dict[str, object]) -> bool:
    return normalized_resource_value(resource, "Device Owner").startswith("compute:")


SERVER_KIND = ResourceKind(
    name="servers",
    label_fields=("Name", "ID"),
)

PORT_KIND = ResourceKind(
    name="ports",
    label_fields=("Name", "ID", "Fixed IP Addresses", "Device Owner"),
)

ROUTER_KIND = ResourceKind(
    name="routers",
    label_fields=("Name", "ID"),
)

FLOATING_IP_KIND = ResourceKind(
    name="floating IPs",
    label_fields=("Floating IP Address", "ID"),
)

VOLUME_KIND = ResourceKind(
    name="volumes",
    label_fields=("Name", "ID", "Status"),
)

NETWORK_KIND = ResourceKind(
    name="networks",
    label_fields=("Name", "ID", "Router Type", "Network Type", "Shared"),
    include_resource=lambda resource: network_is_cleanup_candidate(resource),
)

KEYPAIR_KIND = ResourceKind(
    name="keypairs",
    label_fields=("Name",),
    include_resource=lambda resource: (
        normalized_resource_value(resource, "Name") == "ssh_key"
    ),
    delete_identifier_fields=("Name",),
)


def require_openstacksdk() -> bool:
    try:
        import openstack  # noqa: F401
    except ImportError:
        print(
            "Error: openstacksdk is required. Install with: pip install openstacksdk",
            file=sys.stderr,
        )
        return False
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete common OpenStack resources from one project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes:\n"
            f"  {EXIT_OK}  success / nothing to do / dry-run OK\n"
            f"  {EXIT_FAILURE}  OpenStack or operational failure\n"
            f"  {EXIT_USAGE}  usage / non-interactive misuse "
            "(missing project name or missing --yes)\n"
        ),
    )
    parser.add_argument(
        "project_name",
        nargs="?",
        help="OpenStack project name to clean up",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="List resources that would be deleted, then exit without deleting",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt and delete listed resources",
    )
    parser.add_argument(
        "--wait",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "After deleting servers/volumes, wait until they are gone before "
            "continuing (default: on in CI or with --yes)"
        ),
    )
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=DEFAULT_WAIT_TIMEOUT,
        metavar="SECONDS",
        help=f"Max seconds to wait for async deletes (default: {DEFAULT_WAIT_TIMEOUT})",
    )
    parser.add_argument(
        "--wait-interval",
        type=float,
        default=DEFAULT_WAIT_INTERVAL,
        metavar="SECONDS",
        help=f"Seconds between wait polls (default: {DEFAULT_WAIT_INTERVAL})",
    )
    parser.add_argument(
        "--command-timeout",
        type=int,
        default=DEFAULT_COMMAND_TIMEOUT,
        metavar="SECONDS",
        help=(
            "Timeout for OpenStack API requests "
            f"(default: {DEFAULT_COMMAND_TIMEOUT})"
        ),
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Abort on the first deletion failure",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-resource delete chatter; keep plan table and summary",
    )
    parser.add_argument(
        "--github",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Emit GitHub Actions annotations and write GITHUB_STEP_SUMMARY "
            "(default: on when GITHUB_ACTIONS is set)"
        ),
    )
    return parser.parse_args()


def runtime_options_from_args(args: argparse.Namespace) -> RuntimeOptions:
    wait = args.wait
    if wait is None:
        wait = bool(args.yes) or is_ci()

    return RuntimeOptions(
        quiet=bool(args.quiet),
        fail_fast=bool(args.fail_fast),
        wait=bool(wait),
        wait_timeout=max(1, int(args.wait_timeout)),
        wait_interval=max(0.1, float(args.wait_interval)),
        command_timeout=max(1, int(args.command_timeout)),
        github=use_github_output(args.github),
    )


def connect_project(project_name: str, options: RuntimeOptions) -> Any:
    """Return an openstacksdk Connection scoped to project_name."""
    import openstack

    saved_project_id = os.environ.get("OS_PROJECT_ID")
    saved_project_name = os.environ.get("OS_PROJECT_NAME")
    try:
        os.environ["OS_PROJECT_NAME"] = project_name
        os.environ.pop("OS_PROJECT_ID", None)
        return openstack.connect(
            cloud="envvars",
            project_name=project_name,
            timeout=options.command_timeout,
        )
    except Exception as e:
        raise CleanupError(f"Failed to connect to OpenStack: {e}") from e
    finally:
        if saved_project_id is not None:
            os.environ["OS_PROJECT_ID"] = saved_project_id
        else:
            os.environ.pop("OS_PROJECT_ID", None)
        if saved_project_name is not None:
            os.environ["OS_PROJECT_NAME"] = saved_project_name
        else:
            os.environ.pop("OS_PROJECT_NAME", None)


def is_not_found(exc: BaseException) -> bool:
    from openstack import exceptions as sdk_exc

    if isinstance(exc, (sdk_exc.NotFoundException, sdk_exc.ResourceNotFound)):
        return True
    message = str(exc).casefold()
    return "could not be found" in message or "not found" in message


def exception_message(exc: BaseException) -> str:
    return str(exc).strip() or exc.__class__.__name__


def confirm(prompt: str) -> bool:
    choice = input(f"{prompt} y/n: ")
    return choice.lower().startswith("y")


def resource_id(resource: dict[str, object]) -> str:
    value = resource_value(resource, "ID")
    return str(value or "").strip()


def resource_delete_identifier(kind: ResourceKind, resource: dict[str, object]) -> str:
    for field in kind.delete_identifier_fields:
        value = str(resource_value(resource, field) or "").strip()
        if value:
            return value
    return ""


def resource_label(kind: ResourceKind, resource: dict[str, object]) -> str:
    values = []
    for field in kind.label_fields:
        value = format_field_value(field, resource_value(resource, field))
        if value:
            values.append(value)
    return " / ".join(values) if values else "<unknown>"


def format_field_value(field: str, value: object) -> str:
    if value is None or value == "":
        return ""

    field_key = normalized_field_name(field)

    if field_key in {"routertype", "router_type"}:
        if isinstance(value, bool):
            return "External" if value else "Internal"
        text = str(value).strip()
        if text.casefold() in {"true", "1", "yes"}:
            return "External"
        if text.casefold() in {"false", "0", "no"}:
            return "Internal"
        return text

    if field_key in {"shared"}:
        if isinstance(value, bool):
            return "shared" if value else "not shared"
        text = str(value).strip().casefold()
        if text in {"true", "1", "yes"}:
            return "shared"
        if text in {"false", "0", "no"}:
            return "not shared"
        return str(value).strip()

    if isinstance(value, bool):
        return "True" if value else "False"

    if field_key in {"fixedipaddresses", "fixedips"}:
        if isinstance(value, list):
            ips = []
            for item in value:
                if isinstance(item, dict):
                    ip = str(item.get("ip_address") or "").strip()
                    if ip:
                        ips.append(ip)
                else:
                    text = str(item).strip()
                    if text:
                        ips.append(text)
            return ", ".join(ips) if ips else ""
        text = str(value).strip()
        matches = re.findall(
            r"'ip_address': '([^']+)'|\"ip_address\": \"([^\"]+)\"",
            text,
        )
        ips = [part for match in matches for part in match if part]
        return ", ".join(ips) if ips else text

    if isinstance(value, list):
        return ", ".join(str(item).strip() for item in value if str(item).strip())

    return str(value).strip()


def print_openstack_table(headers: list[str], rows: list[list[str]]) -> None:
    if not headers:
        return

    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def border() -> str:
        return "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def line(cells: list[str]) -> str:
        parts = [f" {cell:<{widths[index]}} " for index, cell in enumerate(cells)]
        return "|" + "|".join(parts) + "|"

    print(border())
    print(line(headers))
    print(border())
    for row in rows:
        print(line(row))
    print(border())


def sdk_to_server(server: Any) -> dict[str, object]:
    return {"ID": server.id, "Name": getattr(server, "name", None) or ""}


def sdk_to_volume(volume: Any) -> dict[str, object]:
    return {
        "ID": volume.id,
        "Name": getattr(volume, "name", None) or "",
        "Status": getattr(volume, "status", None) or "",
    }


def sdk_to_floating_ip(floating_ip: Any) -> dict[str, object]:
    return {
        "ID": floating_ip.id,
        "Floating IP Address": (
            getattr(floating_ip, "floating_ip_address", None) or ""
        ),
    }


def sdk_to_router(router: Any) -> dict[str, object]:
    return {"ID": router.id, "Name": getattr(router, "name", None) or ""}


def sdk_to_port(port: Any) -> dict[str, object]:
    return {
        "ID": port.id,
        "Name": getattr(port, "name", None) or "",
        "Fixed IP Addresses": getattr(port, "fixed_ips", None) or [],
        "Device Owner": getattr(port, "device_owner", None) or "",
        "Device ID": getattr(port, "device_id", None) or "",
    }


def sdk_to_network(network: Any) -> dict[str, object]:
    provider_type = getattr(network, "provider_network_type", None) or ""
    is_external = bool(getattr(network, "is_router_external", False))
    is_shared = bool(getattr(network, "is_shared", False))
    return {
        "ID": network.id,
        "Name": getattr(network, "name", None) or "",
        "Network Type": provider_type,
        "Provider Network Type": provider_type,
        "provider:network_type": provider_type,
        "Router Type": is_external,
        "router:external": is_external,
        "Shared": is_shared,
        "shared": is_shared,
    }


def sdk_to_keypair(keypair: Any) -> dict[str, object]:
    return {"Name": getattr(keypair, "name", None) or ""}


def list_servers(conn: Any) -> list[dict[str, object]]:
    try:
        return [sdk_to_server(server) for server in conn.compute.servers()]
    except Exception as e:
        raise CleanupError(f"Failed to list servers.\n{exception_message(e)}") from e


def list_volumes(conn: Any) -> list[dict[str, object]]:
    try:
        return [sdk_to_volume(volume) for volume in conn.block_storage.volumes()]
    except Exception as e:
        raise CleanupError(f"Failed to list volumes.\n{exception_message(e)}") from e


def list_floating_ips(conn: Any) -> list[dict[str, object]]:
    try:
        return [sdk_to_floating_ip(ip) for ip in conn.network.ips()]
    except Exception as e:
        raise CleanupError(
            f"Failed to list floating IPs.\n{exception_message(e)}"
        ) from e


def list_routers(conn: Any) -> list[dict[str, object]]:
    try:
        return [sdk_to_router(router) for router in conn.network.routers()]
    except Exception as e:
        raise CleanupError(f"Failed to list routers.\n{exception_message(e)}") from e


def list_ports(conn: Any) -> list[dict[str, object]]:
    try:
        return [sdk_to_port(port) for port in conn.network.ports()]
    except Exception as e:
        raise CleanupError(f"Failed to list ports.\n{exception_message(e)}") from e


def list_networks(conn: Any) -> list[dict[str, object]]:
    try:
        return [sdk_to_network(network) for network in conn.network.networks()]
    except Exception as e:
        raise CleanupError(f"Failed to list networks.\n{exception_message(e)}") from e


def list_keypairs(conn: Any) -> list[dict[str, object]]:
    try:
        return [sdk_to_keypair(keypair) for keypair in conn.compute.keypairs()]
    except Exception as e:
        raise CleanupError(f"Failed to list keypairs.\n{exception_message(e)}") from e


LISTERS: dict[str, Callable[[Any], list[dict[str, object]]]] = {
    "servers": list_servers,
    "volumes": list_volumes,
    "floating IPs": list_floating_ips,
    "routers": list_routers,
    "ports": list_ports,
    "networks": list_networks,
    "keypairs": list_keypairs,
}


def list_resources(kind: ResourceKind, conn: Any) -> list[dict[str, object]]:
    lister = LISTERS[kind.name]
    return lister(conn)


def filter_resources(kind: ResourceKind, conn: Any) -> ResourceGroup:
    all_resources = list_resources(kind, conn)
    to_delete = [
        resource for resource in all_resources if kind.include_resource(resource)
    ]
    skipped = [
        resource for resource in all_resources if not kind.include_resource(resource)
    ]
    return ResourceGroup(kind=kind, to_delete=to_delete, skipped=skipped)


def attachment_volume_id(attachment: Any) -> str:
    for attr in ("volume_id", "id"):
        value = getattr(attachment, attr, None)
        if value:
            return str(value).strip()
    if isinstance(attachment, dict):
        for key in ("volume_id", "id"):
            value = attachment.get(key)
            if value:
                return str(value).strip()
    return ""


def attachment_delete_on_termination(attachment: Any) -> bool:
    if hasattr(attachment, "delete_on_termination"):
        return is_true_value(getattr(attachment, "delete_on_termination"))
    if isinstance(attachment, dict):
        for key, value in attachment.items():
            if normalized_field_name(str(key)) == "deleteontermination":
                return is_true_value(value)
    return False


def volumes_deleted_with_servers(
    servers: list[dict[str, object]],
    conn: Any,
    indicator: ProgressIndicator | None = None,
) -> dict[str, str]:
    """Map volume ID to server name for volumes with delete_on_termination=True."""
    auto_delete: dict[str, str] = {}
    total = len(servers)
    for index, server in enumerate(servers, start=1):
        server_id = resource_id(server)
        server_name = str(
            resource_value(server, "Name") or server_id
        ).strip()
        if indicator and total:
            indicator.update(
                f"Checking VM volumes ({index}/{total}): {server_name}..."
            )
        try:
            attachments = list(conn.compute.volume_attachments(server_id))
        except Exception as e:
            raise CleanupError(
                f"Failed to get volumes for server {server_name}.\n"
                f"{exception_message(e)}"
            ) from e
        for attachment in attachments:
            volume_id = attachment_volume_id(attachment)
            if volume_id and attachment_delete_on_termination(attachment):
                auto_delete[volume_id] = server_name
    return auto_delete


def filter_volume_resources(
    servers: list[dict[str, object]],
    conn: Any,
    indicator: ProgressIndicator | None = None,
) -> ResourceGroup:
    if indicator:
        indicator.update(gathering_message(VOLUME_KIND))
    all_volumes = list_volumes(conn)
    auto_delete = volumes_deleted_with_servers(servers, conn, indicator)

    to_delete: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for volume in all_volumes:
        volume_id = resource_id(volume)
        server_name = auto_delete.get(volume_id)
        if server_name:
            skipped.append(
                {**volume, "_skip_reason": f"deleted with server {server_name}"}
            )
        else:
            to_delete.append(volume)

    return ResourceGroup(kind=VOLUME_KIND, to_delete=to_delete, skipped=skipped)


def filter_port_resources(
    servers: list[dict[str, object]],
    conn: Any,
) -> ResourceGroup:
    all_ports = list_ports(conn)
    server_ids = {resource_id(server) for server in servers if resource_id(server)}
    server_names = {
        resource_id(server): str(
            resource_value(server, "Name") or resource_id(server)
        ).strip()
        for server in servers
        if resource_id(server)
    }

    to_delete: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for port in all_ports:
        if is_router_owned_port(port):
            skipped.append({**port, "_skip_reason": "router-owned"})
            continue

        device_id = str(resource_value(port, "Device ID") or "").strip()
        if is_compute_owned_port(port) and (
            (device_id in server_ids) if device_id else bool(server_ids)
        ):
            server_name = server_names.get(device_id, "server")
            skipped.append(
                {**port, "_skip_reason": f"deleted with server {server_name}"}
            )
            continue

        to_delete.append(port)

    return ResourceGroup(kind=PORT_KIND, to_delete=to_delete, skipped=skipped)


def kind_display_name(kind: ResourceKind) -> str:
    return kind.name[:1].upper() + kind.name[1:]


def gathering_message(kind: ResourceKind) -> str:
    labels = {
        "servers": "Gathering VM list...",
        "volumes": "Gathering volume list...",
        "floating IPs": "Gathering floating IP list...",
        "routers": "Gathering router list...",
        "ports": "Gathering port list...",
        "networks": "Gathering network list...",
        "keypairs": "Gathering keypair list...",
    }
    return labels.get(kind.name, f"Gathering {kind.name}...")


def collect_cleanup_plan(conn: Any, _options: RuntimeOptions) -> list[ResourceGroup]:
    with progress(gathering_message(SERVER_KIND)):
        servers = filter_resources(SERVER_KIND, conn)

    with progress(gathering_message(VOLUME_KIND)) as indicator:
        volumes = filter_volume_resources(servers.to_delete, conn, indicator)

    plan: list[ResourceGroup] = [servers, volumes]
    for kind in (FLOATING_IP_KIND, ROUTER_KIND):
        with progress(gathering_message(kind)):
            plan.append(filter_resources(kind, conn))

    with progress(gathering_message(PORT_KIND)):
        plan.append(filter_port_resources(servers.to_delete, conn))

    for kind in (NETWORK_KIND, KEYPAIR_KIND):
        with progress(gathering_message(kind)):
            plan.append(filter_resources(kind, conn))
    return plan


def delete_server(conn: Any, resource: dict[str, object]) -> None:
    conn.compute.delete_server(resource_id(resource), ignore_missing=True)


def delete_volume(conn: Any, resource: dict[str, object]) -> None:
    conn.block_storage.delete_volume(resource_id(resource), ignore_missing=True)


def delete_floating_ip(conn: Any, resource: dict[str, object]) -> None:
    conn.network.delete_ip(resource_id(resource), ignore_missing=True)


def delete_port(conn: Any, resource: dict[str, object]) -> None:
    conn.network.delete_port(resource_id(resource), ignore_missing=True)


def delete_network(conn: Any, resource: dict[str, object]) -> None:
    conn.network.delete_network(resource_id(resource), ignore_missing=True)


def delete_keypair(conn: Any, resource: dict[str, object]) -> None:
    name = str(resource_value(resource, "Name") or "").strip()
    conn.compute.delete_keypair(name, ignore_missing=True)


DELETERS: dict[str, Callable[[Any, dict[str, object]], None]] = {
    "servers": delete_server,
    "volumes": delete_volume,
    "floating IPs": delete_floating_ip,
    "ports": delete_port,
    "networks": delete_network,
    "keypairs": delete_keypair,
}


def delete_resource(
    kind: ResourceKind,
    resource: dict[str, object],
    conn: Any,
    options: RuntimeOptions,
) -> bool:
    identifier = resource_delete_identifier(kind, resource)
    if not identifier:
        log_error(
            f"Skipping {kind.name} item without a delete identifier: {resource}",
            options,
        )
        if options.fail_fast:
            raise FailFastError(f"Missing delete identifier for {kind.name}")
        return False

    label = resource_label(kind, resource)
    log_info(f"Deleting {kind.name}: {label}...", options)
    deleter = DELETERS.get(kind.name)
    if deleter is None:
        message = f"No SDK delete handler for {kind.name}"
        log_error(message, options)
        if options.fail_fast:
            raise FailFastError(message)
        return False

    try:
        deleter(conn, resource)
    except Exception as e:
        if is_not_found(e):
            log_info(f"Deleting {kind.name}: {label}... already gone.", options)
            return True
        message = f"Deleting {kind.name}: {label}... failed.\n{exception_message(e)}"
        log_error(message, options)
        if options.fail_fast:
            raise FailFastError(message) from e
        return False

    log_info(f"Deleting {kind.name}: {label}... done.", options)
    return True


def run_sdk_action(
    description: str,
    action: Callable[[], None],
    options: RuntimeOptions,
    ignore_patterns: tuple[str, ...] = (),
) -> bool:
    log_info(f"{description}...", options)
    try:
        action()
    except Exception as e:
        message = exception_message(e)
        if is_not_found(e):
            log_info(f"{description}... already gone.", options)
            return True
        if any(pattern.casefold() in message.casefold() for pattern in ignore_patterns):
            log_info(f"{description}... skipped.", options)
            return True
        full = f"{description}... failed.\n{message}"
        log_error(full, options)
        if options.fail_fast:
            raise FailFastError(full) from e
        return False

    log_info(f"{description}... done.", options)
    return True


def router_ports(conn: Any, router_id: str) -> list[dict[str, object]]:
    try:
        return [
            sdk_to_port(port)
            for port in conn.network.ports(device_id=router_id)
        ]
    except Exception as e:
        raise CleanupError(
            f"Failed to list ports for router {router_id}.\n{exception_message(e)}"
        ) from e


def fixed_ip_subnet_ids(port: dict[str, object]) -> list[str]:
    fixed_ips = resource_value(port, "Fixed IP Addresses")
    if isinstance(fixed_ips, list):
        return [
            str(item.get("subnet_id") or "").strip()
            for item in fixed_ips
            if isinstance(item, dict) and str(item.get("subnet_id") or "").strip()
        ]

    if isinstance(fixed_ips, str):
        matches = re.findall(
            r"'subnet_id': '([^']+)'|\"subnet_id\": \"([^\"]+)\"",
            fixed_ips,
        )
        return [value for match in matches for value in match if value]

    return []


def remove_router_port(
    conn: Any,
    router_id: str,
    port: dict[str, object],
    options: RuntimeOptions,
) -> bool:
    port_id = resource_id(port)
    if not port_id:
        log_error(f"Skipping router port without an ID: {port}", options)
        if options.fail_fast:
            raise FailFastError(f"Router port missing ID on router {router_id}")
        return False

    label = resource_label(PORT_KIND, port)
    subnet_ids = fixed_ip_subnet_ids(port)
    for subnet_id in subnet_ids:
        if run_sdk_action(
            f"Removing subnet {subnet_id} from router {router_id}",
            lambda sid=subnet_id: conn.network.remove_interface_from_router(
                router_id, subnet=sid
            ),
            options,
        ):
            return True

    return run_sdk_action(
        f"Removing port {label} from router {router_id}",
        lambda: conn.network.remove_interface_from_router(router_id, port=port_id),
        options,
    )


def cleanup_router(
    conn: Any,
    router: dict[str, object],
    options: RuntimeOptions,
) -> bool:
    router_id = resource_id(router)
    if not router_id:
        log_error(f"Skipping router without an ID: {router}", options)
        if options.fail_fast:
            raise FailFastError("Router missing ID")
        return False

    label = resource_label(ROUTER_KIND, router)
    ok = run_sdk_action(
        f"Unsetting external gateway for router {label}",
        lambda: conn.network.remove_gateway_from_router(router_id),
        options,
        ignore_patterns=(
            "no external gateway",
            "not currently set",
            "gateway is not set",
            "external_gateway_info",
        ),
    )

    for port in router_ports(conn, router_id):
        ok = remove_router_port(conn, router_id, port, options) and ok

    return (
        run_sdk_action(
            f"Deleting routers: {label}",
            lambda: conn.network.delete_router(router_id, ignore_missing=True),
            options,
        )
        and ok
    )


def wait_for_deleted_resources(
    kind: ResourceKind,
    resources: list[dict[str, object]],
    conn: Any,
    options: RuntimeOptions,
) -> None:
    if not options.wait or not resources:
        return

    for resource in resources:
        identifier = resource_delete_identifier(kind, resource)
        if not identifier:
            continue
        label = resource_label(kind, resource)
        singular = kind.name.rstrip("s")
        log_info(
            f"Waiting for {singular} {label} to disappear "
            f"(timeout {options.wait_timeout}s)...",
            options,
        )
        try:
            if kind.name == "servers":
                server = conn.compute.get_server(identifier)
                if server is None:
                    log_info(f"Waiting for {singular} {label}... gone.", options)
                    continue
                conn.compute.wait_for_delete(
                    server,
                    wait=options.wait_timeout,
                    interval=options.wait_interval,
                )
            elif kind.name == "volumes":
                volume = conn.block_storage.get_volume(identifier)
                if volume is None:
                    log_info(f"Waiting for {singular} {label}... gone.", options)
                    continue
                conn.block_storage.wait_for_delete(
                    volume,
                    wait=options.wait_timeout,
                    interval=options.wait_interval,
                )
            else:
                continue
        except Exception as e:
            if is_not_found(e):
                log_info(f"Waiting for {singular} {label}... gone.", options)
                continue
            from openstack import exceptions as sdk_exc

            if isinstance(e, sdk_exc.ResourceTimeout):
                raise CleanupError(
                    f"Timed out after {options.wait_timeout}s waiting for "
                    f"{singular} {label} to disappear"
                ) from e
            raise CleanupError(
                f"Failed while waiting for {singular} {label} to disappear.\n"
                f"{exception_message(e)}"
            ) from e

        log_info(f"Waiting for {singular} {label}... gone.", options)


def delete_routers(
    routers: list[dict[str, object]],
    conn: Any,
    options: RuntimeOptions,
) -> int:
    failures = 0
    for router in routers:
        try:
            ok = cleanup_router(conn, router, options)
        except FailFastError:
            raise
        except CleanupError as e:
            log_error(str(e), options)
            ok = False
        if not ok:
            failures += 1
            if options.fail_fast:
                raise FailFastError("Router deletion failed")
    return failures


def delete_kind_resources(
    group: ResourceGroup,
    conn: Any,
    options: RuntimeOptions,
) -> tuple[int, list[dict[str, object]]]:
    failures = 0
    deleted: list[dict[str, object]] = []
    for resource in group.to_delete:
        if delete_resource(group.kind, resource, conn, options):
            deleted.append(resource)
        else:
            failures += 1
    return failures, deleted


def execute_cleanup_plan(
    plan: list[ResourceGroup],
    conn: Any,
    options: RuntimeOptions,
) -> int:
    failures = 0
    for group in plan:
        if not group.to_delete:
            continue
        print()
        print(f"Deleting {kind_display_name(group.kind)}...")
        if group.kind.name == "routers":
            failures += delete_routers(group.to_delete, conn, options)
            deleted: list[dict[str, object]] = []
        else:
            kind_failures, deleted = delete_kind_resources(group, conn, options)
            failures += kind_failures

        if group.kind.name in {"servers", "volumes"}:
            wait_for_deleted_resources(group.kind, deleted, conn, options)
    return failures


def resource_name_and_id(
    kind: ResourceKind, resource: dict[str, object]
) -> tuple[str, str]:
    if kind.name == "floating IPs":
        name = format_field_value(
            "Floating IP Address",
            resource_value(resource, "Floating IP Address"),
        )
        rid = format_field_value("ID", resource_value(resource, "ID"))
        return name, rid

    if kind.name == "keypairs":
        name = format_field_value("Name", resource_value(resource, "Name"))
        return name, ""

    name = format_field_value("Name", resource_value(resource, "Name"))
    rid = format_field_value("ID", resource_value(resource, "ID"))
    return name, rid


def resource_details(kind: ResourceKind, resource: dict[str, object]) -> str:
    skip_fields = {"Name", "ID", "Floating IP Address"}
    parts = []
    for field in kind.label_fields:
        if field in skip_fields:
            continue
        value = format_field_value(field, resource_value(resource, field))
        if value:
            parts.append(value)
    return ", ".join(parts)


def skip_action(resource: dict[str, object]) -> str:
    reason = str(resource.get("_skip_reason") or "").strip()
    if reason.startswith("deleted with server"):
        return f"skip ({reason})"
    if reason == "router-owned":
        return "skip (router-owned)"
    if reason:
        return f"skip ({reason})"
    return "skip"


def build_plan_rows(plan: list[ResourceGroup]) -> tuple[list[list[str]], int]:
    total = 0
    rows: list[list[str]] = []

    for group in plan:
        type_name = kind_display_name(group.kind).rstrip("s")
        if group.kind.name == "floating IPs":
            type_name = "Floating IP"

        for resource in group.to_delete:
            total += 1
            name, rid = resource_name_and_id(group.kind, resource)
            rows.append(
                [
                    type_name,
                    name,
                    rid,
                    resource_details(group.kind, resource),
                    "delete",
                ]
            )

        for resource in group.skipped:
            if group.kind.name == "ports" and str(
                resource.get("_skip_reason", "")
            ) == "router-owned":
                continue
            name, rid = resource_name_and_id(group.kind, resource)
            rows.append(
                [
                    type_name,
                    name,
                    rid,
                    resource_details(group.kind, resource),
                    skip_action(resource),
                ]
            )

    return rows, total


def print_cleanup_plan(plan: list[ResourceGroup]) -> int:
    rows, total = build_plan_rows(plan)

    print()
    print("Resources to clean up:")
    if not rows:
        print("None")
    else:
        print_openstack_table(
            ["Type", "Name", "ID", "Details", "Action"],
            rows,
        )
    print()
    print(f"Total: {total} resource(s) to delete.")
    return total


def write_github_step_summary(
    project_name: str,
    plan: list[ResourceGroup],
    total: int,
    *,
    dry_run: bool,
    failures: int | None,
    options: RuntimeOptions,
) -> None:
    if not options.github:
        return

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    rows, _ = build_plan_rows(plan)
    lines = [
        f"## OpenStack project cleanup: `{project_name}`",
        "",
        f"- Resources to delete: **{total}**",
        f"- Dry run: **{'yes' if dry_run else 'no'}**",
    ]
    if failures is not None:
        lines.append(f"- Deletion failures: **{failures}**")
    lines.extend(
        [
            "",
            "| Type | Name | ID | Details | Action |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    if not rows:
        lines.append("| — | — | — | — | none |")
    else:
        for row in rows:
            cells = [cell.replace("|", "\\|") for cell in row]
            lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def get_project_name(project_name: str | None) -> str:
    if project_name:
        return project_name

    if is_noninteractive():
        raise UsageError(
            "Project name is required in non-interactive mode "
            "(CI or non-TTY). Pass PROJECT_NAME as an argument."
        )

    while True:
        project_name = input("Enter project name to clean up: ").strip()
        if project_name:
            return project_name
        print("Project name cannot be empty.")


def main() -> int:
    args = parse_args()
    options = runtime_options_from_args(args)

    if not require_openstacksdk():
        return EXIT_FAILURE

    try:
        project_name = get_project_name(args.project_name)
    except UsageError as e:
        log_error(str(e), options)
        return EXIT_USAGE

    if not args.dry_run and not args.yes and is_noninteractive():
        log_error(
            "Non-interactive mode requires --yes (or --dry-run) to proceed.",
            options,
        )
        return EXIT_USAGE

    print(f"Using OS_PROJECT_NAME={project_name}")
    print(
        "Deletion order: "
        "server, volume, floating IP, router, port, network, keypair."
    )

    try:
        conn = connect_project(project_name, options)
        plan = collect_cleanup_plan(conn, options)
    except CleanupError as e:
        log_error(str(e), options)
        return EXIT_FAILURE

    total = print_cleanup_plan(plan)

    if total == 0:
        print("Nothing to clean up.")
        write_github_step_summary(
            project_name,
            plan,
            total,
            dry_run=bool(args.dry_run),
            failures=0,
            options=options,
        )
        return EXIT_OK

    if args.dry_run:
        print("Dry run: no resources were deleted.")
        write_github_step_summary(
            project_name,
            plan,
            total,
            dry_run=True,
            failures=0,
            options=options,
        )
        return EXIT_OK

    if not args.yes:
        if is_noninteractive():
            log_error(
                "Non-interactive mode requires --yes to delete resources.",
                options,
            )
            return EXIT_USAGE
        if not confirm(f"Delete all {total} resources listed above?"):
            print("Aborted.")
            return EXIT_OK

    if args.yes:
        print(f"Deleting {total} resources (--yes)...")

    try:
        failures = execute_cleanup_plan(plan, conn, options)
    except FailFastError as e:
        log_error(str(e), options)
        write_github_step_summary(
            project_name,
            plan,
            total,
            dry_run=False,
            failures=1,
            options=options,
        )
        return EXIT_FAILURE
    except CleanupError as e:
        log_error(str(e), options)
        write_github_step_summary(
            project_name,
            plan,
            total,
            dry_run=False,
            failures=1,
            options=options,
        )
        return EXIT_FAILURE

    print()
    if failures:
        log_error(
            f"Completed with {failures} deletion failure(s).",
            options,
        )
        write_github_step_summary(
            project_name,
            plan,
            total,
            dry_run=False,
            failures=failures,
            options=options,
        )
        return EXIT_FAILURE

    print("Completed successfully.")
    write_github_step_summary(
        project_name,
        plan,
        total,
        dry_run=False,
        failures=0,
        options=options,
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
