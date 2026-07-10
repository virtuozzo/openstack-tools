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
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator


EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

DEFAULT_COMMAND_TIMEOUT = 120
DEFAULT_WAIT_TIMEOUT = 600
DEFAULT_WAIT_INTERVAL = 5.0


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
            "Timeout for each openstack CLI call "
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


def project_env(project_name: str) -> dict[str, str]:
    env = os.environ.copy()
    env["OS_PROJECT_NAME"] = project_name
    env.pop("OS_PROJECT_ID", None)
    return env


def run_command(
    command: list[str],
    env: dict[str, str],
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            env=env,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        cmd_name = " ".join(command[:3]) if command else "command"
        raise CleanupError(
            f"Timed out after {timeout}s running: {cmd_name}"
        ) from e


def command_output_or_raise(
    command: list[str],
    env: dict[str, str],
    failure_message: str,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> str:
    result = run_command(command, env, timeout=timeout)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        message = f"{failure_message}\n{output}" if output else failure_message
        raise CleanupError(message)
    return result.stdout


def list_resources(
    kind: ResourceKind,
    env: dict[str, str],
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> list[dict[str, object]]:
    output = command_output_or_raise(
        kind.list_command,
        env,
        f"Failed to list {kind.name}.",
        timeout=timeout,
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
    options: RuntimeOptions,
) -> bool:
    identifier = resource_delete_identifier(kind, resource)
    if not identifier:
        log_error(
            f"Skipping {kind.name} item without a delete identifier: {resource}",
            options,
        )
        if options.fail_fast:
            raise FailFastError(
                f"Missing delete identifier for {kind.name}"
            )
        return False

    label = resource_label(kind, resource)
    log_info(f"Deleting {kind.name}: {label}...", options)
    result = run_command(
        kind.delete_command(identifier),
        env,
        timeout=options.command_timeout,
    )
    output = (result.stdout + result.stderr).strip()

    if result.returncode == 0:
        if output:
            log_info(output, options)
        log_info(f"Deleting {kind.name}: {label}... done.", options)
        return True

    if already_gone_error(kind, output):
        log_info(f"Deleting {kind.name}: {label}... already gone.", options)
        return True

    message = f"Deleting {kind.name}: {label}... failed."
    if output:
        message = f"{message}\n{output}"
    log_error(message, options)
    if options.fail_fast:
        raise FailFastError(message)
    return False


def run_openstack_action(
    description: str,
    command: list[str],
    env: dict[str, str],
    options: RuntimeOptions,
    ignore_failure_patterns: tuple[str, ...] = (),
) -> bool:
    log_info(f"{description}...", options)
    result = run_command(command, env, timeout=options.command_timeout)
    output = (result.stdout + result.stderr).strip()

    if result.returncode == 0:
        if output:
            log_info(output, options)
        log_info(f"{description}... done.", options)
        return True

    output_normalized = output.casefold()
    if any(pattern.casefold() in output_normalized for pattern in ignore_failure_patterns):
        if output:
            log_info(output, options)
        log_info(f"{description}... skipped.", options)
        return True

    message = f"{description}... failed."
    if output:
        message = f"{message}\n{output}"
    log_error(message, options)
    if options.fail_fast:
        raise FailFastError(message)
    return False


# Router cleanup needs Neutron router operations instead of generic port deletion.
def router_ports(
    router_id: str,
    env: dict[str, str],
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> list[dict[str, object]]:
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
        timeout=timeout,
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


def remove_router_port(
    router_id: str,
    port: dict[str, object],
    env: dict[str, str],
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
        if run_openstack_action(
            f"Removing subnet {subnet_id} from router {router_id}",
            ["openstack", "router", "remove", "subnet", router_id, subnet_id],
            env,
            options,
        ):
            return True

    return run_openstack_action(
        f"Removing port {label} from router {router_id}",
        ["openstack", "router", "remove", "port", router_id, port_id],
        env,
        options,
    )


def cleanup_router(
    router: dict[str, object],
    env: dict[str, str],
    options: RuntimeOptions,
) -> bool:
    router_id = resource_id(router)
    if not router_id:
        log_error(f"Skipping router without an ID: {router}", options)
        if options.fail_fast:
            raise FailFastError("Router missing ID")
        return False

    label = resource_label(ROUTER_KIND, router)
    ok = run_openstack_action(
        f"Unsetting external gateway for router {label}",
        ["openstack", "router", "unset", "--external-gateway", router_id],
        env,
        options,
        ignore_failure_patterns=(
            "no external gateway",
            "not currently set",
            "gateway is not set",
        ),
    )

    for port in router_ports(router_id, env, timeout=options.command_timeout):
        ok = remove_router_port(router_id, port, env, options) and ok

    return run_openstack_action(
        f"Deleting routers: {label}",
        ["openstack", "router", "delete", router_id],
        env,
        options,
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


def filter_resources(
    kind: ResourceKind,
    env: dict[str, str],
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> ResourceGroup:
    all_resources = list_resources(kind, env, timeout=timeout)
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
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
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
        timeout=timeout,
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
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
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
        for attachment in server_volumes_attached(server, env, timeout=timeout):
            volume_id = attachment_volume_id(attachment)
            if volume_id and attachment_delete_on_termination(attachment):
                auto_delete[volume_id] = server_name
    return auto_delete


def filter_volume_resources(
    servers: list[dict[str, object]],
    env: dict[str, str],
    indicator: ProgressIndicator | None = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> ResourceGroup:
    if indicator:
        indicator.update(gathering_message(VOLUME_KIND))
    all_volumes = list_resources(VOLUME_KIND, env, timeout=timeout)
    auto_delete = volumes_deleted_with_servers(
        servers, env, indicator, timeout=timeout
    )

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
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> ResourceGroup:
    all_ports = list_resources(PORT_KIND, env, timeout=timeout)
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


def collect_cleanup_plan(
    env: dict[str, str],
    options: RuntimeOptions,
) -> list[ResourceGroup]:
    timeout = options.command_timeout
    with progress(gathering_message(SERVER_KIND)) as indicator:
        servers = filter_resources(SERVER_KIND, env, timeout=timeout)

    with progress(gathering_message(VOLUME_KIND)) as indicator:
        volumes = filter_volume_resources(
            servers.to_delete, env, indicator, timeout=timeout
        )

    plan: list[ResourceGroup] = [servers, volumes]
    for kind in (
        FLOATING_IP_KIND,
        ROUTER_KIND,
    ):
        with progress(gathering_message(kind)):
            plan.append(filter_resources(kind, env, timeout=timeout))

    with progress(gathering_message(PORT_KIND)):
        plan.append(
            filter_port_resources(servers.to_delete, env, timeout=timeout)
        )

    for kind in (
        NETWORK_KIND,
        KEYPAIR_KIND,
    ):
        with progress(gathering_message(kind)):
            plan.append(filter_resources(kind, env, timeout=timeout))
    return plan


def resource_name_and_id(kind: ResourceKind, resource: dict[str, object]) -> tuple[str, str]:
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
            # Router-owned ports are internal bookkeeping; keep the table focused.
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
    lines.extend(["", "| Type | Name | ID | Details | Action |", "| --- | --- | --- | --- | --- |"])
    if not rows:
        lines.append("| — | — | — | — | none |")
    else:
        for row in rows:
            cells = [cell.replace("|", "\\|") for cell in row]
            lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def resource_not_found(output: str) -> bool:
    normalized = output.casefold()
    markers = (
        "could not be found",
        "no server with a name or id",
        "no volume with a name or id",
        "no volume found",
        "no server found",
        "not found",
    )
    return any(marker in normalized for marker in markers)


def wait_until_gone(
    kind: ResourceKind,
    resource: dict[str, object],
    show_command: list[str],
    env: dict[str, str],
    options: RuntimeOptions,
) -> None:
    identifier = resource_delete_identifier(kind, resource)
    label = resource_label(kind, resource)
    if not identifier:
        return

    singular = kind.name.rstrip("s")
    deadline = time.monotonic() + options.wait_timeout
    log_info(
        f"Waiting for {singular} {label} to disappear "
        f"(timeout {options.wait_timeout}s)...",
        options,
    )

    while True:
        result = run_command(
            show_command,
            env,
            timeout=options.command_timeout,
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0 and resource_not_found(output):
            log_info(f"Waiting for {singular} {label}... gone.", options)
            return
        if result.returncode != 0 and not resource_not_found(output):
            message = (
                f"Failed while waiting for {singular} {label} to disappear."
            )
            if output:
                message = f"{message}\n{output}"
            raise CleanupError(message)

        if time.monotonic() >= deadline:
            raise CleanupError(
                f"Timed out after {options.wait_timeout}s waiting for "
                f"{singular} {label} to disappear"
            )
        time.sleep(options.wait_interval)


def wait_for_deleted_resources(
    kind: ResourceKind,
    resources: list[dict[str, object]],
    env: dict[str, str],
    options: RuntimeOptions,
) -> None:
    if not options.wait or not resources:
        return

    for resource in resources:
        identifier = resource_delete_identifier(kind, resource)
        if not identifier:
            continue
        if kind.name == "servers":
            show_command = [
                "openstack",
                "server",
                "show",
                identifier,
                "-f",
                "value",
                "-c",
                "id",
            ]
        elif kind.name == "volumes":
            show_command = [
                "openstack",
                "volume",
                "show",
                identifier,
                "-f",
                "value",
                "-c",
                "id",
            ]
        else:
            continue
        wait_until_gone(kind, resource, show_command, env, options)


def delete_routers(
    routers: list[dict[str, object]],
    env: dict[str, str],
    options: RuntimeOptions,
) -> int:
    failures = 0
    for router in routers:
        try:
            ok = cleanup_router(router, env, options)
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
    env: dict[str, str],
    options: RuntimeOptions,
) -> tuple[int, list[dict[str, object]]]:
    failures = 0
    deleted: list[dict[str, object]] = []
    for resource in group.to_delete:
        if delete_resource(group.kind, resource, env, options):
            deleted.append(resource)
        else:
            failures += 1
    return failures, deleted


def execute_cleanup_plan(
    plan: list[ResourceGroup],
    env: dict[str, str],
    options: RuntimeOptions,
) -> int:
    failures = 0
    for group in plan:
        if not group.to_delete:
            continue
        print()
        print(f"Deleting {kind_display_name(group.kind)}...")
        if group.kind.name == "routers":
            failures += delete_routers(group.to_delete, env, options)
            deleted: list[dict[str, object]] = []
        else:
            kind_failures, deleted = delete_kind_resources(group, env, options)
            failures += kind_failures

        if group.kind.name in {"servers", "volumes"}:
            wait_for_deleted_resources(group.kind, deleted, env, options)
    return failures


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

    if not require_openstack_cli():
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

    env = project_env(project_name)

    print(f"Using OS_PROJECT_NAME={project_name}")
    print(
        "Deletion order: "
        "server, volume, floating IP, router, port, network, keypair."
    )

    try:
        plan = collect_cleanup_plan(env, options)
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
        failures = execute_cleanup_plan(plan, env, options)
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
