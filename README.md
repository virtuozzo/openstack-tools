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
- Detaches router interfaces before deleting routers.
- Skips networks named `public`; only internal, vxlan, non-shared networks are eligible.

```bash
source openrc.sh
python3 openstack-project-cleanup.py PROJECT_NAME
python3 openstack-project-cleanup.py --dry-run PROJECT_NAME
```

Run any script with `--help` for all options.
