#!/usr/bin/env python3
"""Delete common OpenStack resources from one project."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator


@dataclass(frozen=True)
class ResourceKind:
    name: str
    list_command: list[str]
    delete_command: Callable[[str], list[str]]
    label_fields: tuple[str, ...]
    include_resource: Callable[[dict[str, object]], bool] = lambda _resource: True
    delete_identifier_fields: tuple[str, ...] = ("ID",)


class CleanupError(RuntimeError):
    """Recoverable cleanup failure for one resource group."""


SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


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
    return normalized_resource_value(resource, "Device Owner").startswith("network:router")


def is_compute_owned_port(resource: dict[str, object]) -> bool:
    return normalized_resource_value(resource, "Device Owner").startswith("compute:")


SERVER_KIND = ResourceKind(
    name="servers",
    list_command=[
        "openstack",
        "server",
        "list",
        "-f",
        "json",
        "-c",
        "ID",
        "-c",
        "Name",
    ],
    delete_command=lambda resource_id: ["openstack", "server", "delete", resource_id],
    label_fields=("Name", "ID"),
)

PORT_KIND = ResourceKind(
    name="ports",
    list_command=[
        "openstack",
        "port",
        "list",
        "--long",
        "-f",
        "json",
        "-c",
        "ID",
        "-c",
        "Name",
        "-c",
        "Fixed IP Addresses",
        "-c",
        "Device Owner",
        "-c",
        "Device ID",
    ],
    delete_command=lambda resource_id: ["openstack", "port", "delete", resource_id],
    label_fields=("Name", "ID", "Fixed IP Addresses", "Device Owner"),
)

ROUTER_KIND = ResourceKind(
    name="routers",
    list_command=[
        "openstack",
        "router",
        "list",
        "-f",
        "json",
        "-c",
        "ID",
        "-c",
        "Name",
    ],
    delete_command=lambda resource_id: ["openstack", "router", "delete", resource_id],
    label_fields=("Name", "ID"),
)

FLOATING_IP_KIND = ResourceKind(
    name="floating IPs",
    list_command=[
        "openstack",
        "floating",
        "ip",
        "list",
        "-f",
        "json",
        "-c",
        "ID",
        "-c",
        "Floating IP Address",
    ],
    delete_command=lambda resource_id: [
        "openstack",
        "floating",
        "ip",
        "delete",
        resource_id,
    ],
    label_fields=("Floating IP Address", "ID"),
)

VOLUME_KIND = ResourceKind(
    name="volumes",
    list_command=[
        "openstack",
        "volume",
        "list",
        "-f",
        "json",
        "-c",
        "ID",
        "-c",
        "Name",
        "-c",
        "Status",
    ],
    delete_command=lambda resource_id: ["openstack", "volume", "delete", resource_id],
    label_fields=("Name", "ID", "Status"),
)

NETWORK_KIND = ResourceKind(
    name="networks",
    list_command=[
        "openstack",
        "network",
        "list",
        "--long",
        "-f",
        "json",
    ],
    delete_command=lambda resource_id: ["openstack", "network", "delete", resource_id],
    label_fields=("Name", "ID", "Router Type", "Network Type", "Shared"),
    include_resource=lambda resource: network_is_cleanup_candidate(resource),
)

KEYPAIR_KIND = ResourceKind(
    name="keypairs",
    list_command=[
        "openstack",
        "keypair",
        "list",
        "-f",
        "json",
        "-c",
        "Name",
    ],
    delete_command=lambda resource_name: ["openstack", "keypair", "delete", resource_name],
    label_fields=("Name",),
    include_resource=lambda resource: normalized_resource_value(resource, "Name") == "ssh_key",
    delete_identifier_fields=("Name",),
)


def require_openstack_cli() -> bool:
    if shutil.which("openstack"):
        return True

    print(
        "Error: OpenStack CLI is required. Install python-openstackclient.",
        file=sys.stderr,
    )
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete common OpenStack resources from one project.",
    )
    parser.add_argument(
        "project_name",
        nargs="?",
        help="OpenStack project name to clean up; prompts interactively if omitted.",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="list resources that would be deleted without making changes.",
    )
    return parser.parse_args()


def project_env(project_name: str) -> dict[str, str]:
    env = os.environ.copy()
    env["OS_PROJECT_NAME"] = project_name
    env.pop("OS_PROJECT_ID", None)
    return env


def run_command(
    command: list[str],
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, env=env, text=True)


def command_output_or_raise(
    command: list[str],
    env: dict[str, str],
    failure_message: str,
) -> str:
    result = run_command(command, env)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        message = f"{failure_message}\n{output}" if output else failure_message
        raise CleanupError(message)
    return result.stdout


def list_resources(kind: ResourceKind, env: dict[str, str]) -> list[dict[str, object]]:
    output = command_output_or_raise(
        kind.list_command,
        env,
        f"Failed to list {kind.name}.",
    )
    try:
        resources = json.loads(output)
    except json.JSONDecodeError as e:
        raise CleanupError(f"Failed to parse {kind.name} list output: {e}") from e

    if not isinstance(resources, list):
        raise CleanupError(f"Unexpected {kind.name} list output.")

    return [resource for resource in resources if isinstance(resource, dict)]


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
        value = str(resource_value(resource, field) or "").strip()
        if value:
            values.append(value)
    return " / ".join(values) if values else "<unknown>"


def confirm(prompt: str) -> bool:
    choice = input(f"{prompt} y/n: ")
    return choice.lower().startswith("y")


def already_gone_error(kind: ResourceKind, output: str) -> bool:
    if kind.name != "ports":
        return False
    normalized = output.casefold()
    return "no port found" in normalized or "could not be found" in normalized


def delete_resource(
    kind: ResourceKind,
    resource: dict[str, object],
    env: dict[str, str],
) -> bool:
    identifier = resource_delete_identifier(kind, resource)
    if not identifier:
        print(
            f"Skipping {kind.name} item without a delete identifier: {resource}",
            file=sys.stderr,
        )
        return False

    label = resource_label(kind, resource)
    print(f"Deleting {kind.name}: {label}...")
    result = run_command(kind.delete_command(identifier), env)
    output = (result.stdout + result.stderr).strip()

    if result.returncode == 0:
        if output:
            print(output)
        print(f"Deleting {kind.name}: {label}... done.")
        return True

    if already_gone_error(kind, output):
        print(f"Deleting {kind.name}: {label}... already gone.")
        return True

    print(f"Deleting {kind.name}: {label}... failed.", file=sys.stderr)
    if output:
        print(output, file=sys.stderr)
    return False


def run_openstack_action(
    description: str,
    command: list[str],
    env: dict[str, str],
    ignore_failure_patterns: tuple[str, ...] = (),
) -> bool:
    print(f"{description}...")
    result = run_command(command, env)
    output = (result.stdout + result.stderr).strip()

    if result.returncode == 0:
        if output:
            print(output)
        print(f"{description}... done.")
        return True

    output_normalized = output.casefold()
    if any(pattern.casefold() in output_normalized for pattern in ignore_failure_patterns):
        if output:
            print(output)
        print(f"{description}... skipped.")
        return True

    print(f"{description}... failed.", file=sys.stderr)
    if output:
        print(output, file=sys.stderr)
    return False


# Router cleanup needs Neutron router operations instead of generic port deletion.
def router_ports(router_id: str, env: dict[str, str]) -> list[dict[str, object]]:
    output = command_output_or_raise(
        [
            "openstack",
            "port",
            "list",
            "--router",
            router_id,
            "-f",
            "json",
            "-c",
            "ID",
            "-c",
            "Name",
            "-c",
            "Fixed IP Addresses",
            "-c",
            "Device Owner",
        ],
        env,
        f"Failed to list ports for router {router_id}.",
    )
    try:
        ports = json.loads(output)
    except json.JSONDecodeError as e:
        raise CleanupError(f"Failed to parse router port list output: {e}") from e

    if not isinstance(ports, list):
        raise CleanupError("Unexpected router port list output.")

    return [port for port in ports if isinstance(port, dict)]


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


def remove_router_port(router_id: str, port: dict[str, object], env: dict[str, str]) -> bool:
    port_id = resource_id(port)
    if not port_id:
        print(f"Skipping router port without an ID: {port}", file=sys.stderr)
        return False

    label = resource_label(PORT_KIND, port)
    subnet_ids = fixed_ip_subnet_ids(port)
    for subnet_id in subnet_ids:
        if run_openstack_action(
            f"Removing subnet {subnet_id} from router {router_id}",
            ["openstack", "router", "remove", "subnet", router_id, subnet_id],
            env,
        ):
            return True

    return run_openstack_action(
        f"Removing port {label} from router {router_id}",
        ["openstack", "router", "remove", "port", router_id, port_id],
        env,
    )


def cleanup_router(router: dict[str, object], env: dict[str, str]) -> bool:
    router_id = resource_id(router)
    if not router_id:
        print(f"Skipping router without an ID: {router}", file=sys.stderr)
        return False

    label = resource_label(ROUTER_KIND, router)
    ok = run_openstack_action(
        f"Unsetting external gateway for router {label}",
        ["openstack", "router", "unset", "--external-gateway", router_id],
        env,
        ignore_failure_patterns=(
            "no external gateway",
            "not currently set",
            "gateway is not set",
        ),
    )

    for port in router_ports(router_id, env):
        ok = remove_router_port(router_id, port, env) and ok

    return run_openstack_action(
        f"Deleting routers: {label}",
        ["openstack", "router", "delete", router_id],
        env,
    ) and ok


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


def filter_resources(kind: ResourceKind, env: dict[str, str]) -> ResourceGroup:
    all_resources = list_resources(kind, env)
    to_delete = [
        resource for resource in all_resources if kind.include_resource(resource)
    ]
    skipped = [
        resource for resource in all_resources if not kind.include_resource(resource)
    ]
    return ResourceGroup(kind=kind, to_delete=to_delete, skipped=skipped)


def server_volumes_attached(
    server: dict[str, object],
    env: dict[str, str],
) -> list[dict[str, object]]:
    server_id = resource_id(server)
    server_name = str(resource_value(server, "Name") or server_id).strip()
    output = command_output_or_raise(
        [
            "openstack",
            "server",
            "show",
            server_id,
            "-f",
            "json",
            "-c",
            "volumes_attached",
        ],
        env,
        f"Failed to get volumes for server {server_name}.",
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as e:
        raise CleanupError(
            f"Failed to parse volumes for server {server_name}: {e}"
        ) from e

    if isinstance(payload, list):
        if not payload:
            return []
        payload = payload[0]

    if not isinstance(payload, dict):
        return []

    attached = resource_value(payload, "volumes_attached")
    if not isinstance(attached, list):
        return []

    return [item for item in attached if isinstance(item, dict)]


def attachment_volume_id(attachment: dict[str, object]) -> str:
    for field in ("id", "volume_id"):
        value = attachment.get(field)
        if value:
            return str(value).strip()
    return ""


def attachment_delete_on_termination(attachment: dict[str, object]) -> bool:
    for key, value in attachment.items():
        if normalized_field_name(str(key)) == "deleteontermination":
            return is_true_value(value)
    return False


def volumes_deleted_with_servers(
    servers: list[dict[str, object]],
    env: dict[str, str],
    indicator: ProgressIndicator | None = None,
) -> dict[str, str]:
    """Map volume ID to server name for volumes with delete_on_termination=True."""
    auto_delete: dict[str, str] = {}
    total = len(servers)
    for index, server in enumerate(servers, start=1):
        server_name = str(resource_value(server, "Name") or resource_id(server)).strip()
        if indicator and total:
            indicator.update(
                f"Checking VM volumes ({index}/{total}): {server_name}..."
            )
        for attachment in server_volumes_attached(server, env):
            volume_id = attachment_volume_id(attachment)
            if volume_id and attachment_delete_on_termination(attachment):
                auto_delete[volume_id] = server_name
    return auto_delete


def filter_volume_resources(
    servers: list[dict[str, object]],
    env: dict[str, str],
    indicator: ProgressIndicator | None = None,
) -> ResourceGroup:
    if indicator:
        indicator.update(gathering_message(VOLUME_KIND))
    all_volumes = list_resources(VOLUME_KIND, env)
    auto_delete = volumes_deleted_with_servers(servers, env, indicator)

    to_delete: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for volume in all_volumes:
        volume_id = resource_id(volume)
        server_name = auto_delete.get(volume_id)
        if server_name:
            skipped.append({**volume, "_skip_reason": f"deleted with server {server_name}"})
        else:
            to_delete.append(volume)

    return ResourceGroup(kind=VOLUME_KIND, to_delete=to_delete, skipped=skipped)


def filter_port_resources(
    servers: list[dict[str, object]],
    env: dict[str, str],
) -> ResourceGroup:
    all_ports = list_resources(PORT_KIND, env)
    server_ids = {resource_id(server) for server in servers if resource_id(server)}
    server_names = {
        resource_id(server): str(resource_value(server, "Name") or resource_id(server)).strip()
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


def collect_cleanup_plan(env: dict[str, str]) -> list[ResourceGroup]:
    with progress(gathering_message(SERVER_KIND)) as indicator:
        servers = filter_resources(SERVER_KIND, env)

    with progress(gathering_message(VOLUME_KIND)) as indicator:
        volumes = filter_volume_resources(servers.to_delete, env, indicator)

    plan: list[ResourceGroup] = [servers, volumes]
    for kind in (
        FLOATING_IP_KIND,
        ROUTER_KIND,
    ):
        with progress(gathering_message(kind)):
            plan.append(filter_resources(kind, env))

    with progress(gathering_message(PORT_KIND)):
        plan.append(filter_port_resources(servers.to_delete, env))

    for kind in (
        NETWORK_KIND,
        KEYPAIR_KIND,
    ):
        with progress(gathering_message(kind)):
            plan.append(filter_resources(kind, env))
    return plan


def print_cleanup_plan(plan: list[ResourceGroup]) -> int:
    total = 0
    print()
    print("Resources to clean up:")
    for group in plan:
        total += len(group.to_delete)
        print()
        print(f"{kind_display_name(group.kind)} ({len(group.to_delete)}):")
        if group.skipped:
            if group.kind.name == "volumes":
                print(f"  skipped {len(group.skipped)} (deleted with server):")
                for resource in group.skipped:
                    print(f"    skipped: {resource_label(group.kind, resource)}")
            elif group.kind.name == "ports":
                with_server = [
                    resource
                    for resource in group.skipped
                    if str(resource.get("_skip_reason", "")).startswith("deleted with server")
                ]
                other = [
                    resource
                    for resource in group.skipped
                    if resource not in with_server
                ]
                if with_server:
                    print(f"  skipped {len(with_server)} (deleted with server):")
                    for resource in with_server:
                        print(f"    skipped: {resource_label(group.kind, resource)}")
                if other:
                    print(f"  skipped {len(other)} by cleanup filters")
            else:
                print(f"  skipped {len(group.skipped)} by cleanup filters")
                if group.kind.name == "networks":
                    for resource in group.skipped:
                        print(f"    skipped: {resource_label(group.kind, resource)}")
        if not group.to_delete:
            print("  none")
            continue
        for index, resource in enumerate(group.to_delete, start=1):
            print(f"  {index}. {resource_label(group.kind, resource)}")
    print()
    print(f"Total: {total} resource(s) to delete.")
    return total


def delete_routers(routers: list[dict[str, object]], env: dict[str, str]) -> int:
    failures = 0
    for router in routers:
        try:
            ok = cleanup_router(router, env)
        except CleanupError as e:
            print(e, file=sys.stderr)
            ok = False
        if not ok:
            failures += 1
    return failures


def delete_kind_resources(group: ResourceGroup, env: dict[str, str]) -> int:
    failures = 0
    for resource in group.to_delete:
        if not delete_resource(group.kind, resource, env):
            failures += 1
    return failures


def execute_cleanup_plan(plan: list[ResourceGroup], env: dict[str, str]) -> int:
    failures = 0
    for group in plan:
        if not group.to_delete:
            continue
        print()
        print(f"Deleting {kind_display_name(group.kind)}...")
        if group.kind.name == "routers":
            failures += delete_routers(group.to_delete, env)
        else:
            failures += delete_kind_resources(group, env)
    return failures


def get_project_name(project_name: str | None) -> str:
    if project_name:
        return project_name

    while True:
        project_name = input("Enter project name to clean up: ").strip()
        if project_name:
            return project_name
        print("Project name cannot be empty.")


def main() -> int:
    args = parse_args()

    if not require_openstack_cli():
        return 1

    project_name = get_project_name(args.project_name)
    env = project_env(project_name)

    print(f"Using OS_PROJECT_NAME={project_name}")
    print(
        "Deletion order: "
        "server, volume, floating IP, router, port, network, keypair."
    )

    try:
        plan = collect_cleanup_plan(env)
    except CleanupError as e:
        print(e, file=sys.stderr)
        return 1

    total = print_cleanup_plan(plan)
    if total == 0:
        print("Nothing to clean up.")
        return 0

    if args.dry_run:
        print("Dry run: no resources were deleted.")
        return 0

    if not confirm(f"Delete all {total} resources listed above?"):
        print("Aborted.")
        return 0

    failures = execute_cleanup_plan(plan, env)

    print()
    if failures:
        print(f"Completed with {failures} deletion failure(s).", file=sys.stderr)
        return 1

    print("Completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
