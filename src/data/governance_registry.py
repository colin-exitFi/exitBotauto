"""
Governance committee registry for Velox.

This turns governance roles and doctrine into first-class platform assets so
humans, agents, and dashboard surfaces can all reference the same source of truth.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional


REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_FILE = REPO_ROOT / "config" / "governance_committee.json"


def _safe_load_json(path: Path) -> Dict:
    try:
        raw = json.loads(path.read_text()) if path.exists() else {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _resolve_repo_path(relative_path: str) -> Path:
    return (REPO_ROOT / str(relative_path or "")).resolve()


@lru_cache(maxsize=1)
def load_governance_registry() -> Dict:
    raw = _safe_load_json(REGISTRY_FILE)
    committee = dict(raw.get("committee", {}) or {})
    roles = list(raw.get("roles", []) or [])
    rollout_states = list(raw.get("rollout_states", []) or [])
    book_lifecycle = list(raw.get("book_lifecycle", []) or [])

    normalized_roles: List[Dict] = []
    for role in roles:
        if not isinstance(role, dict):
            continue
        role_id = str(role.get("id", "") or "").strip()
        if not role_id:
            continue
        normalized = dict(role)
        normalized["id"] = role_id
        normalized["title"] = str(role.get("title", role_id) or role_id)
        normalized["summary"] = str(role.get("summary", "") or "")
        normalized["persona"] = str(role.get("persona", "") or "")
        normalized["order"] = int(role.get("order", 999) or 999)
        normalized["focus_layers"] = list(role.get("focus_layers", []) or [])
        normalized["required_questions"] = list(role.get("required_questions", []) or [])
        doc_path = str(role.get("doc_path", "") or "")
        normalized["doc_path"] = doc_path
        normalized["doc_abspath"] = str(_resolve_repo_path(doc_path)) if doc_path else ""
        normalized_roles.append(normalized)

    normalized_roles.sort(key=lambda row: (int(row.get("order", 999) or 999), row.get("id", "")))

    artifacts = dict((committee.get("artifacts", {}) or {}))
    normalized_artifacts = {}
    for key, relative_path in artifacts.items():
        rel = str(relative_path or "")
        normalized_artifacts[key] = {
            "path": rel,
            "abspath": str(_resolve_repo_path(rel)) if rel else "",
        }
    committee["artifacts"] = normalized_artifacts

    return {
        "committee": committee,
        "rollout_states": rollout_states,
        "book_lifecycle": book_lifecycle,
        "roles": normalized_roles,
    }


def list_governance_roles(include_docs: bool = False) -> List[Dict]:
    rows = []
    for role in load_governance_registry().get("roles", []):
        item = dict(role)
        if include_docs:
            item["doc_markdown"] = load_role_document(role.get("id", ""))
        rows.append(item)
    return rows


def get_governance_role(role_id: str, include_doc: bool = False) -> Dict:
    target = str(role_id or "").strip().lower()
    for role in load_governance_registry().get("roles", []):
        if str(role.get("id", "")).lower() == target:
            item = dict(role)
            if include_doc:
                item["doc_markdown"] = load_role_document(target)
            return item
    return {}


def load_role_document(role_id: str) -> str:
    role = get_governance_role(role_id, include_doc=False)
    path = Path(str(role.get("doc_abspath", "") or ""))
    if not path.exists():
        return ""
    try:
        return path.read_text()
    except Exception:
        return ""


def get_governance_committee_summary(include_docs: bool = False) -> Dict:
    registry = load_governance_registry()
    committee = dict(registry.get("committee", {}) or {})
    return {
        "committee": {
            "name": committee.get("name", ""),
            "operator_role": committee.get("operator_role", ""),
            "mission": committee.get("mission", ""),
            "doctrine": committee.get("doctrine", ""),
            "artifacts": committee.get("artifacts", {}),
        },
        "rollout_states": list(registry.get("rollout_states", []) or []),
        "book_lifecycle": list(registry.get("book_lifecycle", []) or []),
        "roles": list_governance_roles(include_docs=include_docs),
    }


def get_governance_artifact(name: str) -> Optional[Dict]:
    committee = load_governance_registry().get("committee", {}) or {}
    artifacts = dict(committee.get("artifacts", {}) or {})
    artifact = artifacts.get(str(name or "").strip())
    return dict(artifact) if isinstance(artifact, dict) else None
