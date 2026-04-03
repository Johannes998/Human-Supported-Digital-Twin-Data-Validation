from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Any, List


BASE_DIR = Path(__file__).resolve().parent
JSON_DIR = BASE_DIR / "json_files"
KB_PATH = JSON_DIR / "knowledge_base.json"


def _default_kb() -> Dict[str, Any]:
    return {
        "error_types": [
            {
                "id": "ET_INCONSISTENT_TERMINOLOGY",
                "description": "Inconsistent terminology",
            },
            {
                "id": "ET_IMPRECISE_TERMINOLOGY",
                "description": "Imprecise terminology",
            },
            {
                "id": "ET_DUPLICATED_EVENT",
                "description": "Duplicated event",
            },
        ],
        "solution_patterns": [],
        "edges": [],
    }


def load_kb() -> Dict[str, Any]:
    JSON_DIR.mkdir(exist_ok=True)
    if not KB_PATH.exists():
        kb = _default_kb()
        save_kb(kb)
        return kb
    with KB_PATH.open("r", encoding="utf-8") as f:
        kb = json.load(f)
    # Ensure default error types exist
    if "error_types" not in kb:
        kb["error_types"] = _default_kb()["error_types"]
    return kb


def save_kb(kb: Dict[str, Any]) -> Path:
    JSON_DIR.mkdir(exist_ok=True)
    with KB_PATH.open("w", encoding="utf-8") as f:
        json.dump(kb, f, ensure_ascii=False, indent=2)
    return KB_PATH


def list_error_types(kb: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(kb.get("error_types", []))


def list_solution_patterns(kb: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(kb.get("solution_patterns", []))


def add_solution_pattern(
    kb: Dict[str, Any],
    error_type_id: str,
    description: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    patterns = kb.setdefault("solution_patterns", [])
    new_id = f"SP_{len(patterns) + 1}"
    pattern = {
        "id": new_id,
        "error_type_id": error_type_id,
        "description": description,
        "params": params,
    }
    patterns.append(pattern)
    kb.setdefault("edges", []).append(
        {"source": error_type_id, "target": new_id, "type": "solved_by"}
    )
    return kb


def add_error_type(kb: Dict[str, Any], et_id: str, description: str, fr: str | None = None) -> Dict[str, Any]:
    """Add a new error type node to the ontology-like structure.

    The optional argument ``fr`` is ignored and only kept for backward
    compatibility with earlier versions of the prototype that stored
    explicit requirement IDs in the knowledge base.
    """

    error_types = kb.setdefault("error_types", [])
    if any(et.get("id") == et_id for et in error_types):
        return kb
    entry: Dict[str, Any] = {"id": et_id, "description": description}
    error_types.append(entry)
    return kb


def update_error_type(
    kb: Dict[str, Any], et_id: str, description: str | None = None, fr: str | None = None
) -> Dict[str, Any]:
    """Update the label of an existing error type.

    The parameter ``fr`` is accepted for backward compatibility but is
    ignored, as requirement identifiers are no longer stored here.
    """

    for et in kb.get("error_types", []):
        if et.get("id") == et_id:
            if description is not None:
                et["description"] = description
            break
    return kb


def delete_error_type(kb: Dict[str, Any], et_id: str) -> Dict[str, Any]:
    """Delete an error type and any patterns/edges associated with it.

    Prototype-level cascading delete to keep the knowledge graph
    consistent.
    """

    # Remove error type itself
    kb["error_types"] = [
        et for et in kb.get("error_types", []) if et.get("id") != et_id
    ]

    # Collect and remove associated solution patterns
    removed_patterns: List[str] = []
    remaining_patterns = []
    for p in kb.get("solution_patterns", []):
        if p.get("error_type_id") == et_id:
            removed_patterns.append(p.get("id"))
        else:
            remaining_patterns.append(p)
    kb["solution_patterns"] = remaining_patterns

    # Remove edges touching the error type or removed patterns
    kb["edges"] = [
        e
        for e in kb.get("edges", [])
        if e.get("source") != et_id and e.get("target") not in removed_patterns
    ]
    return kb


def find_patterns_for_error_type(
    kb: Dict[str, Any], error_type_id: str
) -> List[Dict[str, Any]]:
    return [
        p
        for p in kb.get("solution_patterns", [])
        if p.get("error_type_id") == error_type_id
    ]


def update_solution_pattern(
    kb: Dict[str, Any],
    pattern_id: str,
    error_type_id: str | None = None,
    description: str | None = None,
    params: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Update fields of an existing solution pattern.

    Besides description and parameters, the assignment to an error type
    (``error_type_id``) can now also be changed. In that case the
    corresponding edges in the knowledge graph (``edges`` with
    ``type == 'solved_by'``) are updated so that they reference the new
    error type.
    """

    for p in kb.get("solution_patterns", []):
        if p.get("id") == pattern_id:
            if error_type_id is not None:
                p["error_type_id"] = error_type_id
                # Alle Kanten, die dieses Pattern als Ziel haben,
                # auf den neuen Fehlertyp als Quelle umbiegen.
                for e in kb.get("edges", []):
                    if e.get("target") == pattern_id and e.get("type") == "solved_by":
                        e["source"] = error_type_id
            if description is not None:
                p["description"] = description
            if params is not None:
                p["params"] = params
            break
    return kb


def delete_solution_pattern(kb: Dict[str, Any], pattern_id: str) -> Dict[str, Any]:
    """Delete a solution pattern and its connecting edges."""

    kb["solution_patterns"] = [
        p for p in kb.get("solution_patterns", []) if p.get("id") != pattern_id
    ]
    kb["edges"] = [
        e for e in kb.get("edges", []) if e.get("target") != pattern_id
    ]
    return kb


def as_graph(kb: Dict[str, Any]) -> Dict[str, Any]:
    """Return a simple graph view for visualization in the UI."""

    nodes = []
    for et in kb.get("error_types", []):
        nodes.append({"id": et["id"], "label": et.get("description", ""), "type": "error_type"})
    for sp in kb.get("solution_patterns", []):
        nodes.append(
            {
                "id": sp["id"],
                "label": sp["description"],
                "type": "solution_pattern",
            }
        )
    edges = list(kb.get("edges", []))
    return {"nodes": nodes, "edges": edges}
