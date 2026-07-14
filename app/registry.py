"""Loads projects.json — the file-based project registry.

Supports an optional top-level `defaults` block that provides Celonis-specific
(or any org-specific) defaults. Each project inherits from defaults; anything
the project explicitly sets wins. This keeps per-project entries minimal —
in the simplest case a project only needs: key, name, product_scope_value.
"""
import json
from copy import deepcopy
from pathlib import Path

REGISTRY_PATH = Path(__file__).resolve().parents[1] / "projects.json"


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep-merge `override` into a copy of `base`. Values in override win.
    Lists are replaced whole (not concatenated) to avoid surprises."""
    out = deepcopy(base) if isinstance(base, dict) else {}
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def _resolve_project(project: dict, defaults: dict) -> dict:
    """Return a project with `defaults` merged in and per-project convenience
    fields propagated into the right nested locations (e.g. product_scope_value
    → both security_filter and customer_filter)."""
    resolved = _deep_merge(defaults, project)

    # Ensure jira.project_keys reflects the project's key by default
    resolved.setdefault("jira", {}).setdefault("project_keys", [resolved.get("key")])

    # If the project has a top-level product_scope_value, propagate it into
    # the security_filter and customer_filter product_scope_value fields.
    psv = project.get("product_scope_value") or resolved.get("product_scope_value")
    if psv:
        jira = resolved.setdefault("jira", {})
        jira.setdefault("security_filter", {})["product_scope_value"] = psv
        jira.setdefault("customer_filter", {})["product_scope_value"] = psv

    return resolved


def load():
    raw = json.loads(REGISTRY_PATH.read_text())
    defaults = raw.get("defaults", {})
    default_jira = defaults.get("jira", {})
    projects_raw = raw.get("projects", [])

    resolved_projects = [_resolve_project(p, {"jira": default_jira}) for p in projects_raw]

    return {"projects": resolved_projects, "defaults": defaults}


def find(key: str):
    for p in load()["projects"]:
        if p["key"] == key:
            return p
    return None


def load_raw():
    """Return the on-disk projects.json without merging defaults.
    Used when the write path needs to persist a minimal entry."""
    return json.loads(REGISTRY_PATH.read_text())


def save_raw(cfg: dict) -> None:
    REGISTRY_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
