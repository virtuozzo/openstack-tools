#!/usr/bin/env python3
"""
Sum OpenStack quota usage (cores, ram, gigabytes) across all projects in a domain.
Uses OpenStack Python SDK (openstacksdk) with credentials from environment variables.
"""

import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

# Resources we aggregate (OpenStack quota resource names)
QUOTA_RESOURCES = ("cores", "ram", "gigabytes")
ZERO_QUOTA_USAGE: dict[str, int] = {r: 0 for r in QUOTA_RESOURCES}

# Table column layout: project, cores, ram GiB, storage TiB
_TABLE_COLS = (8, 10, 14)  # widths for cores, ram, storage


def _ram_mb_to_gib(mb: int) -> float:
    return mb / 1024.0


def _gb_to_tib(gb: int) -> float:
    return gb / 1024.0


def _accumulate_totals(totals: dict[str, int], usage: dict[str, int]) -> None:
    for r in QUOTA_RESOURCES:
        totals[r] += usage.get(r, 0)


def _apply_quota_limits_from_env(args: argparse.Namespace) -> None:
    """Fill limit_* from OS_QUOTA_LIMIT_* when CLI did not set them."""
    for attr, env_var, cast in (
        ("limit_cores", "OS_QUOTA_LIMIT_CORES", int),
        ("limit_ram", "OS_QUOTA_LIMIT_RAM", float),
        ("limit_storage", "OS_QUOTA_LIMIT_STORAGE", float),
    ):
        if getattr(args, attr) is not None:
            continue
        v = os.environ.get(env_var, "").strip()
        if not v:
            continue
        try:
            setattr(args, attr, cast(v))
        except ValueError:
            print(f"Warning: invalid {env_var} '{v}', ignoring.", file=sys.stderr)


def get_env(name: str, optional: bool = False) -> str | None:
    value = os.environ.get(name)
    if not value and not optional:
        print(f"Error: required env variable {name} is not set", file=sys.stderr)
        sys.exit(1)
    return value or None


def _in_use_from_usage_dict(usage: dict, name: str) -> int | None:
    if name not in usage:
        return None
    v = usage.get(name)
    if v is not None and isinstance(v, (int, float)):
        return int(v)
    return None


def _in_use_from_quota_resource(q, name: str) -> int:
    """
    Extract in-use value from an SDK QuotaSet resource. With usage=True the SDK
    exposes a nested "usage" dict (usage.cores, usage.ram, usage.gigabytes);
    top-level .cores/.ram/.gigabytes are limits. Prefer the usage dict.
    """
    usage = getattr(q, "usage", None)
    if isinstance(usage, dict):
        n = _in_use_from_usage_dict(usage, name)
        if n is not None:
            return n
    if hasattr(q, "to_dict"):
        usage = q.to_dict().get("usage")
        if isinstance(usage, dict):
            n = _in_use_from_usage_dict(usage, name)
            if n is not None:
                return n
    # Fallbacks: explicit _in_use attribute or nested {in_use: n} per resource
    in_use_attr = f"{name}_in_use"
    if hasattr(q, in_use_attr):
        v = getattr(q, in_use_attr)
        if v is not None and isinstance(v, (int, float)):
            return int(v)
    raw = getattr(q, name, None)
    if isinstance(raw, dict):
        return int(
            raw.get("in_use") or raw.get("in use") or raw.get("In Use") or 0
        )
    if hasattr(raw, "in_use"):
        return int(getattr(raw, "in_use") or 0)
    # Bare int from SDK is the limit, not in_use
    return 0


def get_quota_usage_sdk(conn, project_id: str) -> dict[str, int]:
    """
    Get quota usage (in_use) for a project via OpenStack SDK (compute + block_storage).
    The connection must be scoped to this project (so we request "our own" quota).
    """
    result = ZERO_QUOTA_USAGE.copy()

    # Compute: cores, ram (conn must be project-scoped to this project_id)
    try:
        q = conn.compute.get_quota_set(project_id, usage=True)
        result["cores"] = _in_use_from_quota_resource(q, "cores")
        result["ram"] = _in_use_from_quota_resource(q, "ram")
    except Exception as e:
        print(f"Warning: compute quota for {project_id}: {e}", file=sys.stderr)

    # Block storage: gigabytes
    try:
        if hasattr(conn, "block_storage") and conn.block_storage is not None:
            q = conn.block_storage.get_quota_set(project_id, usage=True)
            result["gigabytes"] = _in_use_from_quota_resource(q, "gigabytes")
    except Exception as e:
        print(f"Warning: block_storage quota for {project_id}: {e}", file=sys.stderr)

    return result


def _connect_scoped_to_project(
    project_id: str,
    project_domain_id: str | None,
    project_domain_name: str | None,
    fallback_domain_id: str | None,
    fallback_domain_name: str | None,
):
    """Create a new SDK connection scoped to the given project (for domain-admin quota access)."""
    import openstack

    domain_id = project_domain_id or fallback_domain_id
    domain_name = project_domain_name or fallback_domain_name
    env_keys = (
        "OS_PROJECT_ID",
        "OS_PROJECT_NAME",
        "OS_PROJECT_DOMAIN_ID",
        "OS_PROJECT_DOMAIN_NAME",
    )
    saved = {k: os.environ.get(k) for k in env_keys}
    try:
        os.environ["OS_PROJECT_ID"] = project_id
        if domain_id:
            os.environ["OS_PROJECT_DOMAIN_ID"] = domain_id
            os.environ.pop("OS_PROJECT_DOMAIN_NAME", None)
        elif domain_name:
            os.environ["OS_PROJECT_DOMAIN_NAME"] = domain_name
            os.environ.pop("OS_PROJECT_DOMAIN_ID", None)
        os.environ.pop("OS_PROJECT_NAME", None)
        return openstack.connect(cloud="envvars")
    finally:
        for k in env_keys:
            if saved.get(k) is not None:
                os.environ[k] = saved[k]
            else:
                os.environ.pop(k, None)


def _fetch_quota_worker(
    name: str,
    pid: str,
    project_domain_id: str | None,
    project_domain_name: str | None,
    fallback_domain_id: str | None,
    fallback_domain_name: str | None,
    env_snapshot: dict | None = None,
) -> tuple[str, str, dict]:
    """
    Fetch quota usage for one project; runs in a separate process.
    If env_snapshot is provided (e.g. on Windows spawn), set env from it so auth works.
    Otherwise the process uses inherited env (fork).
    Returns (name, pid, usage).
    """
    if env_snapshot:
        os.environ.update(env_snapshot)
    try:
        conn = _connect_scoped_to_project(
            pid,
            project_domain_id,
            project_domain_name,
            fallback_domain_id,
            fallback_domain_name,
        )
        usage = get_quota_usage_sdk(conn, pid)
        return (name, pid, usage)
    except Exception as e:
        print(f"Warning: could not get quota for project {name} ({pid}): {e}", file=sys.stderr)
        return (name, pid, ZERO_QUOTA_USAGE.copy())


def _project_scope_args(
    project,
    fallback_domain_id: str | None,
    fallback_domain_name: str | None,
):
    """Build the tuple used to create a project-scoped quota connection."""
    return (
        (project.name or "unknown").strip(),
        project.id,
        getattr(project, "domain_id", None),
        getattr(project, "domain_name", None),
        fallback_domain_id,
        fallback_domain_name,
    )


def _fetch_quota_worker_unpack(args: tuple) -> tuple[str, str, dict]:
    """Unpack task tuple for ProcessPoolExecutor (lambdas are not picklable)."""
    return _fetch_quota_worker(*args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sum OpenStack quota usage (cores, ram, storage) across projects in a domain.",
    )
    parser.add_argument(
        "--limit-cores",
        type=int,
        default=None,
        metavar="N",
        help="Domain limit for cores; free = limit - total usage. Also from OS_QUOTA_LIMIT_CORES.",
    )
    parser.add_argument(
        "--limit-ram",
        type=float,
        default=None,
        metavar="GiB",
        help=(
            "Domain limit for RAM in GiB; free = limit - total usage. "
            "Also from OS_QUOTA_LIMIT_RAM."
        ),
    )
    parser.add_argument(
        "--limit-storage",
        type=float,
        default=None,
        metavar="TiB",
        help=(
            "Domain limit for storage in TiB; free = limit - total usage. "
            "Also from OS_QUOTA_LIMIT_STORAGE."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        metavar="N",
        help=(
            "Number of parallel workers to fetch project quotas (default: 8). "
            "Use 1 for sequential."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    _apply_quota_limits_from_env(args)

    get_env("OS_AUTH_URL")
    get_env("OS_USERNAME")
    get_env("OS_PASSWORD")
    domain_id = get_env("OS_DOMAIN_ID", optional=True)
    domain_name = (
        get_env("OS_DOMAIN_NAME", optional=True)
        or get_env("OS_PROJECT_DOMAIN_NAME", optional=True)
        or get_env("OS_USER_DOMAIN_NAME", optional=True)
    )
    if not domain_id and not domain_name:
        print(
            "Error: set one of OS_DOMAIN_ID, OS_DOMAIN_NAME, "
            "OS_PROJECT_DOMAIN_NAME, or OS_USER_DOMAIN_NAME",
            file=sys.stderr,
        )
        return 1

    try:
        import openstack
    except ImportError:
        print(
            "Error: openstacksdk is required. Install with: pip install openstacksdk",
            file=sys.stderr,
        )
        return 1

    # Connect using env vars (cloud='envvars' uses OS_* variables)
    conn = openstack.connect(cloud="envvars")

    # Do not call find_domain(domain_name): that requires identity:list_domains, which
    # domain admins often lack. With only domain_name, list via domain_name
    # filter or all visible projects.

    # List projects (optionally filtered by domain)
    try:
        if domain_id:
            projects = list(conn.identity.projects(domain_id=domain_id))
        elif domain_name:
            try:
                projects = list(conn.identity.projects(domain_name=domain_name))
            except Exception:
                projects = list(conn.identity.projects())
        else:
            projects = list(conn.identity.projects())
    except Exception as e:
        print(f"Error listing projects: {e}", file=sys.stderr)
        return 1

    if not projects:
        print(
            "No projects found. Check domain and token scope "
            "(e.g. unscoped or domain-scoped).",
            file=sys.stderr,
        )
        return 1

    n_projects = len(projects)
    workers = max(1, min(args.workers, n_projects))

    totals = ZERO_QUOTA_USAGE.copy()
    project_usages = []

    if workers == 1:
        print(f"Found {n_projects} project(s). Fetching quota usage...", file=sys.stderr)
        for idx, project in enumerate(projects, start=1):
            name, pid, p_dom_id, p_dom_name, fb_id, fb_name = _project_scope_args(
                project, domain_id, domain_name
            )
            print(f"  [{idx}/{n_projects}] {name} ({pid})", file=sys.stderr)
            try:
                conn_scoped = _connect_scoped_to_project(
                    pid, p_dom_id, p_dom_name, fb_id, fb_name
                )
                usage = get_quota_usage_sdk(conn_scoped, pid)
            except Exception as e:
                print(
                    f"Warning: could not get quota for project {name} ({pid}): {e}",
                    file=sys.stderr,
                )
                usage = ZERO_QUOTA_USAGE.copy()
            project_usages.append((name, pid, usage))
            _accumulate_totals(totals, usage)
    else:
        print(
            f"Found {n_projects} project(s). Fetching quota usage ({workers} workers)...",
            file=sys.stderr,
        )
        # Snapshot OS_* env so workers have auth when using spawn (e.g. Windows)
        env_snapshot = {k: v for k, v in os.environ.items() if k.startswith("OS_")}
        tasks = [
            (*_project_scope_args(project, domain_id, domain_name), env_snapshot)
            for project in projects
        ]
        results_by_index: list[tuple[int, tuple[str, str, dict]]] = []
        with ProcessPoolExecutor(max_workers=workers) as executor:
            future_to_index = {
                executor.submit(_fetch_quota_worker_unpack, t): i
                for i, t in enumerate(tasks)
            }
            done = 0
            for future in as_completed(future_to_index):
                idx = future_to_index[future]
                try:
                    result = future.result()
                    results_by_index.append((idx, result))
                except Exception as e:
                    name = tasks[idx][0]
                    pid = tasks[idx][1]
                    print(
                        f"Warning: could not get quota for project {name} ({pid}): {e}",
                        file=sys.stderr,
                    )
                    results_by_index.append((idx, (name, pid, ZERO_QUOTA_USAGE.copy())))
                done += 1
                msg = f"  Completed {done}/{n_projects}..."
                pad = max(0, len(f"  Completed {n_projects}/{n_projects}...") - len(msg))
                print(f"\r{msg}{' ' * pad}", end="", file=sys.stderr)
                sys.stderr.flush()
        print(file=sys.stderr)
        for _idx, (name, pid, usage) in sorted(results_by_index, key=lambda x: x[0]):
            project_usages.append((name, pid, usage))
            _accumulate_totals(totals, usage)

    project_usages.sort(key=lambda row: row[0].casefold())

    w_c, w_r, w_s = _TABLE_COLS
    name_width = max(len(name) for name, _, _ in project_usages) if project_usages else 20
    name_width = max(name_width, 8)
    table_sep = 2 + name_width + 2 + w_c + 2 + w_r + 2 + w_s
    header = (
        f"  {'Project':<{name_width}}  {'cores':>{w_c}}  "
        f"{'ram (GiB)':>{w_r}}  {'storage (TiB)':>{w_s}}"
    )
    print("Project quota usage (cores, ram GiB, storage TiB):")
    print(header)
    print("  " + "-" * table_sep)
    for name, _pid, usage in project_usages:
        c = usage.get("cores", 0)
        r = _ram_mb_to_gib(usage.get("ram", 0))
        s = _gb_to_tib(usage.get("gigabytes", 0))
        print(f"  {name:<{name_width}}  {c:>{w_c}}  {r:>{w_r}.2f}  {s:>{w_s}.2f}")
    print("  " + "-" * table_sep)
    t_c = totals["cores"]
    t_r = _ram_mb_to_gib(totals["ram"])
    t_s = _gb_to_tib(totals["gigabytes"])
    print(
        f"  {'TOTAL':<{name_width}}  {t_c:>{w_c}}  "
        f"{t_r:>{w_r}.2f}  {t_s:>{w_s}.2f}"
    )

    if (
        args.limit_cores is not None
        or args.limit_ram is not None
        or args.limit_storage is not None
    ):
        lc, lr, ls = args.limit_cores, args.limit_ram, args.limit_storage

        def _cell_int(v: int | None) -> str:
            return f"{v:>{w_c}}" if v is not None else f"{'-':>{w_c}}"

        def _cell_float(v: float | None) -> str:
            return f"{v:>{w_r}.2f}" if v is not None else f"{'-':>{w_r}}"

        def _cell_float_s(v: float | None) -> str:
            return f"{v:>{w_s}.2f}" if v is not None else f"{'-':>{w_s}}"

        print(
            f"  {'QUOTA':<{name_width}}  {_cell_int(lc)}  {_cell_float(lr)}  {_cell_float_s(ls)}"
        )
        print("  " + "-" * table_sep)
        fc = max(0, lc - t_c) if lc is not None else None
        fr = max(0.0, lr - t_r) if lr is not None else None
        fs = max(0.0, ls - t_s) if ls is not None else None
        print(
            f"  {'FREE':<{name_width}}  {_cell_int(fc)}  {_cell_float(fr)}  {_cell_float_s(fs)}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
