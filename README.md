# OpenStack tools

Small helper scripts for OpenStack administration.

## Requirements

- Python 3.10+
- OpenStack credentials in the usual `OS_*` environment variables
- `openstacksdk` for `openstack-domain-quota-usage.py`
- OpenStack CLI for `openstack-image-share.py` and `openstack-project-cleanup.py`

```bash
pip install -r requirements.txt
```

## `openstack-domain-quota-usage.py`

Summarizes quota usage across projects in a domain: cores, RAM in GiB, and volume storage in TiB.

```bash
source openrc.sh
python3 openstack-domain-quota-usage.py
```

Useful options:

```bash
python3 openstack-domain-quota-usage.py --workers 4
python3 openstack-domain-quota-usage.py --limit-cores 400 --limit-ram 800 --limit-storage 20
```

Domain/project selection uses `OS_DOMAIN_ID`, `OS_DOMAIN_NAME`, `OS_PROJECT_DOMAIN_NAME`, or `OS_USER_DOMAIN_NAME`.

## `openstack-image-share.py`

Accepts a shared Glance image across visible projects. With `--add-image`, also runs `openstack image add project` before accepting, which requires authenticating as the image owner.

```bash
source openrc.sh
python3 openstack-image-share.py IMAGE_ID
python3 openstack-image-share.py --add-image IMAGE_ID
```

## `openstack-project-cleanup.py`

Lists all project resources to be cleaned up, then asks once before deleting them:

- Deletes servers, volumes, floating IPs, routers, ports, networks, then the `ssh_key` keypair.
- Skips volumes attached to VMs with `delete_on_termination=True` (removed when the VM is deleted).
- Skips compute ports attached to VMs (removed when the VM is deleted).
- Detaches router interfaces before deleting routers.
- Skips networks named `public`; only internal, vxlan, non-shared networks are eligible.
- With `--wait` (default in CI or with `--yes`), polls until deleted servers/volumes are gone before continuing.

```bash
source openrc.sh
python3 openstack-project-cleanup.py PROJECT_NAME
python3 openstack-project-cleanup.py --dry-run PROJECT_NAME
python3 openstack-project-cleanup.py --yes PROJECT_NAME
```

### GitHub Actions

In CI / non-TTY environments the script never prompts. Pass the project name and either `--yes` or `--dry-run`. Standard `OS_*` credentials must be present in the job environment.

```bash
python3 openstack-project-cleanup.py --yes --wait --fail-fast --quiet PROJECT_NAME
```

Useful CI options:

- `--wait` / `--no-wait` — wait for async server/volume deletes (default on in CI or with `--yes`)
- `--wait-timeout` / `--wait-interval` — wait polling controls (defaults: 600s / 5s)
- `--command-timeout` — per-CLI-call timeout (default: 120s)
- `--fail-fast` — abort on the first deletion failure
- `--quiet` — keep the plan table and final summary; suppress per-resource chatter
- `--github` — emit `::error::` / `::warning::` annotations and append a markdown table to `$GITHUB_STEP_SUMMARY` (on by default when `GITHUB_ACTIONS` is set)

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Success, nothing to do, or dry-run OK |
| `1` | OpenStack / operational failure |
| `2` | Usage / non-interactive misuse (missing project name or missing `--yes`) |

Run any script with `--help` for all options.
