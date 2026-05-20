#!/usr/bin/env python3
"""Delete common OpenStack resources from one project."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable


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
    ],
    delete_command=lambda resource_id: ["openstack", "port", "delete", resource_id],
    label_fields=("Name", "ID", "Fixed IP Addresses", "Device Owner"),
    include_resource=lambda resource: not is_router_owned_port(resource),
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


def cleanup_routers(env: dict[str, str]) -> int:
    try:
        routers = list_resources(ROUTER_KIND, env)
    except CleanupError as e:
        print(e, file=sys.stderr)
        return 1

    print()
    print(f"Routers ({len(routers)}):")
    if not routers:
        print("  none")
        return 0

    for index, router in enumerate(routers, start=1):
        print(f"  {index}. {resource_label(ROUTER_KIND, router)}")

    if not confirm(f"Delete all {len(routers)} routers?"):
        print("Skipping routers.")
        return 0

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


# Generic list, prompt, and delete flow for resource kinds without special handling.
def cleanup_kind(kind: ResourceKind, env: dict[str, str]) -> int:
    try:
        all_resources = list_resources(kind, env)
    except CleanupError as e:
        print(e, file=sys.stderr)
        return 1
    resources = [
        resource for resource in all_resources if kind.include_resource(resource)
    ]
    skipped_count = len(all_resources) - len(resources)

    print()
    print(f"{kind.name[:1].upper() + kind.name[1:]} ({len(resources)}):")
    if skipped_count:
        print(f"  skipped {skipped_count} by cleanup filters")
        if kind.name == "networks":
            for resource in all_resources:
                if not kind.include_resource(resource):
                    print(f"    skipped: {resource_label(kind, resource)}")
    if not resources:
        print("  none")
        return 0

    for index, resource in enumerate(resources, start=1):
        print(f"  {index}. {resource_label(kind, resource)}")

    if not confirm(f"Delete all {len(resources)} {kind.name}?"):
        print(f"Skipping {kind.name}.")
        return 0

    failures = 0
    for resource in resources:
        if not delete_resource(kind, resource, env):
            failures += 1
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
        "Resources will be processed in this order: "
        "server, volume, floating IP, router, port, network, keypair."
    )

    failures = 0
    failures += cleanup_kind(SERVER_KIND, env)
    failures += cleanup_kind(VOLUME_KIND, env)
    failures += cleanup_kind(FLOATING_IP_KIND, env)
    failures += cleanup_routers(env)
    failures += cleanup_kind(PORT_KIND, env)
    failures += cleanup_kind(NETWORK_KIND, env)
    failures += cleanup_kind(KEYPAIR_KIND, env)

    print()
    if failures:
        print(f"Completed with {failures} deletion failure(s).", file=sys.stderr)
        return 1

    print("Completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
