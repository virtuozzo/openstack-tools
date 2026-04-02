# OpenStack domain quota usage (`openstack-domain-quota-usage.py`)

Summarizes **OpenStack quota usage** (compute + block storage) across all projects in a domain: **cores**, **RAM** (reported in GiB), and **volume storage** (reported in TiB). Uses the [OpenStack Python SDK](https://docs.openstack.org/openstacksdk/) only—no OpenStack CLI required.

## Requirements

- **Python** 3.10+ (uses `str | None`, `dict[str, int]`, etc.)
- **`openstacksdk`** (see `requirements.txt`)

```bash
pip install -r requirements.txt
```

## Authentication

Set the usual OpenStack environment variables (same as `openstack` CLI / `cloud='envvars'`):

| Variable | Required |
|----------|----------|
| `OS_AUTH_URL` | Yes |
| `OS_USERNAME` | Yes |
| `OS_PASSWORD` | Yes |
| `OS_REGION_NAME` | Optional (recommended if you use regions) |

Also set **user** domain if your cloud uses Keystone v3 domains:

| Variable | Notes |
|----------|--------|
| `OS_USER_DOMAIN_NAME` or `OS_USER_DOMAIN_ID` | Often required |

## Domain / project listing

You must provide **either** a domain id **or** a domain name (the script does **not** call `identity:list_domains`, which many domain admins cannot use):

| Variable | Purpose |
|----------|---------|
| `OS_DOMAIN_ID` | Filter projects by domain id |
| `OS_DOMAIN_NAME` | Filter projects by domain name |
| `OS_PROJECT_DOMAIN_NAME` | Used if the above are unset (typical with `openstack rc` files) |
| `OS_USER_DOMAIN_NAME` | Fallback for domain name when listing/scoping |

If listing by domain name fails, the script falls back to listing all projects visible to your token.

## How it works

1. Connects with `openstack.connect(cloud="envvars")` and lists projects (optionally filtered by domain).
2. For **each project**, opens a **project-scoped** connection (temporary `OS_PROJECT_ID` + project domain). That matches what domain admins can do: see each project’s own quota usage without cloud-wide admin APIs.
3. Reads Nova `get_quota_set(..., usage=True)` and Cinder `get_quota_set(..., usage=True)` and takes values from the SDK’s nested **`usage`** map (not top-level limits).
4. Prints a table sorted by **project name** (case-insensitive), then **TOTAL**. If domain limits are configured, adds **QUOTA** and **FREE** rows aligned with the same columns.

Progress and warnings go to **stderr**; the table goes to **stdout**.

## Optional domain limits (free capacity)

You can pass domain-wide caps so the script prints **QUOTA** (limits) and **FREE** (`max(0, limit − total usage)`):

**CLI**

```bash
python3 openstack-domain-quota-usage.py --limit-cores 378 --limit-ram 792 --limit-storage 19
```

**Environment** (used when the matching CLI flag is omitted)

| Variable | Type | Unit |
|----------|------|------|
| `OS_QUOTA_LIMIT_CORES` | integer | vCPUs |
| `OS_QUOTA_LIMIT_RAM` | float | GiB |
| `OS_QUOTA_LIMIT_STORAGE` | float | TiB |

CLI overrides env. If only some limits are set, missing **QUOTA** / **FREE** cells show `-`.

## Parallel fetching

| Flag | Default | Meaning |
|------|---------|---------|
| `--workers N` | `8` | Number of worker **processes** (capped by project count). |
| `--workers 1` | — | Sequential mode; per-project lines on stderr. |

With `N > 1`, workers use the same env-based auth as sequential mode; an `OS_*` snapshot is passed so **spawn** (e.g. Windows) still gets credentials. Progress is a single updating line on stderr (`Completed k/N...`).

## Examples

```bash
source openrc.sh   # or export OS_* by hand
python3 openstack-domain-quota-usage.py
```

```bash
export OS_QUOTA_LIMIT_CORES=400 OS_QUOTA_LIMIT_RAM=800 OS_QUOTA_LIMIT_STORAGE=20
python3 openstack-domain-quota-usage.py --workers 4
```

```bash
python3 openstack-domain-quota-usage.py --help
```

## Output columns

- **cores** — in-use vCPUs (sum across projects).
- **ram (GiB)** — in-use RAM from Nova quota usage (MiB → GiB, ÷1024).
- **storage (TiB)** — in-use volume gigabytes from Cinder quota usage (GB → TiB, ÷1024).

## Limitations

- Intended for users who can **list projects** in the domain and **assume** each project’s scope for quota (e.g. domain admin). Pure project members only see their own project unless policy allows more.
- Depends on Nova/Cinder exposing usage in the SDK quota response (`usage` sub-dict). If a service is unavailable, that column may show `0` with a warning on stderr.
