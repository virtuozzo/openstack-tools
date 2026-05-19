#!/usr/bin/env python3
"""Accept a shared OpenStack image across visible projects."""

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass


UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
EXAMPLE_IMAGE_ID = "80222c0c-f98d-46ac-bf4a-d31d416fc94b"


@dataclass
class Project:
    project_id: str
    name: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Accept an OpenStack image in projects.",
        epilog=(
            "IMAGE_ID is optional; if omitted, you will be prompted interactively.\n"
            "By default, the script only accepts the image. Accepting is possible as a\n"
            "member of the target project, or for any project as a domain admin."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-a",
        "--add-image",
        action="store_true",
        help=(
            "run 'openstack image add project' before accepting "
            "(only possible when authenticated as the image owner)"
        ),
    )
    parser.add_argument("image_id", nargs="?", help="OpenStack image UUID")
    return parser.parse_args()


def validate_uuid(image_id: str) -> bool:
    return bool(UUID_RE.match(image_id))


def get_image_id(image_id: str | None) -> str:
    if image_id:
        if not validate_uuid(image_id):
            print(f"Invalid ID: {image_id}", file=sys.stderr)
            print(f"ID should look like: {EXAMPLE_IMAGE_ID}", file=sys.stderr)
            sys.exit(1)
        return image_id

    print(f"Enter image ID. It should look like this: {EXAMPLE_IMAGE_ID}")
    while True:
        image_id = input("Enter ID: ")
        if validate_uuid(image_id):
            return image_id
        print(
            "Invalid ID format. You can obtain the ID using "
            "'openstack image show <image_name>'."
        )


def require_openstack_cli() -> bool:
    if shutil.which("openstack"):
        return True

    print(
        "Error: OpenStack CLI is required. Install python-openstackclient.",
        file=sys.stderr,
    )
    return False


def run_command(
    command: list[str],
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, env=env, text=True)


def command_output_or_exit(command: list[str], failure_message: str) -> str:
    result = run_command(command)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        print(failure_message, file=sys.stderr)
        if output:
            print(output, file=sys.stderr)
        sys.exit(1)
    return output


def run_openstack_action(
    description: str,
    command: list[str],
    env: dict[str, str] | None = None,
) -> bool:
    print(f"{description}...")
    result = run_command(command, env=env)
    output = (result.stdout + result.stderr).strip()

    if result.returncode == 0:
        if output:
            print(output)
        print(f"{description}... done.")
        return True

    print(f"{description}... failed.", file=sys.stderr)
    if output:
        print(output, file=sys.stderr)
    return False


def parse_projects(project_list_output: str) -> list[Project]:
    projects: list[Project] = []
    for line in project_list_output.splitlines():
        line = line.strip()
        if not line:
            continue

        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue

        projects.append(Project(project_id=parts[0], name=parts[1]))
    return projects


def project_env(project_name: str) -> dict[str, str]:
    env = os.environ.copy()
    env["OS_PROJECT_NAME"] = project_name
    return env


def confirm(project_count: int) -> bool:
    choice = input(f"Proceed with these {project_count} projects? y/n: ")
    return choice.lower().startswith("y")


def main() -> int:
    args = parse_args()
    if not require_openstack_cli():
        return 1

    image_id = get_image_id(args.image_id)
    authenticated_project_name = os.environ.get("OS_PROJECT_NAME", "")

    image_name = command_output_or_exit(
        ["openstack", "image", "show", image_id, "-f", "value", "-c", "name"],
        f"Failed to find image {image_id}.",
    )
    image_owner = command_output_or_exit(
        ["openstack", "image", "show", image_id, "-f", "value", "-c", "owner"],
        f"Failed to find owner for image {image_id}.",
    )
    project_list_output = command_output_or_exit(
        [
            "openstack",
            "project",
            "list",
            "-f",
            "value",
            "-c",
            "ID",
            "-c",
            "Name",
            "--sort-column",
            "Name",
        ],
        "Failed to list OpenStack projects.",
    )

    all_projects = parse_projects(project_list_output)
    image_owner_name = next(
        (project.name for project in all_projects if project.project_id == image_owner),
        "unknown",
    )
    projects = [
        project
        for project in all_projects
        if "edu-vap-region" not in f"{project.project_id} {project.name}"
    ]

    print(
        f"Working with image {image_name} owned by {image_owner_name} "
        f"({image_owner}). Authenticated project: {authenticated_project_name}."
    )

    if not projects:
        print("No projects found.", file=sys.stderr)
        return 1

    mode = "add-image + accept" if args.add_image else "accept-only"
    print()
    print(f"Mode: {mode}")
    print(f"Projects ({len(projects)}):")
    for index, project in enumerate(projects, start=1):
        print(f"  {index}. {project.name} ({project.project_id})")
    print()

    if not confirm(len(projects)):
        print("Aborted.")
        return 0

    failed_projects = 0
    for project in projects:
        print()

        if args.add_image:
            if not run_openstack_action(
                f"Adding image to project {project.name}",
                ["openstack", "image", "add", "project", image_id, project.name],
                env=project_env(authenticated_project_name),
            ):
                failed_projects += 1
                continue

        if not run_openstack_action(
            f"Accepting in project {project.name}",
            ["openstack", "image", "set", "--accept", "--project", project.project_id, image_id],
            env=project_env(project.name),
        ):
            failed_projects += 1

    if failed_projects:
        print()
        print(f"Completed with {failed_projects} project failure(s).")
        return 1

    print()
    print("Completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
