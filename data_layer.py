from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Any
import xml.etree.ElementTree as ET
import copy


BASE_DIR = Path(__file__).resolve().parent

# Centralized directories for logs and JSON artifacts
EVENT_LOG_DIR = BASE_DIR / "event_logs"
JSON_DIR = BASE_DIR / "json_files"

RAW_LOG_PATH = EVENT_LOG_DIR / "MainProcess.xes"
VALIDATED_DIR = BASE_DIR / "validated_logs"
ANOMALIES_PATH = JSON_DIR / "anomalies_queue.json"
SIM_STATE_PATH = JSON_DIR / "simulation_state.json"
HANDLED_ISSUES_PATH = JSON_DIR / "handled_issues.json"
CORRECTED_LOG_PATH = EVENT_LOG_DIR / "corrected_MainProcess.xes"
ERRONEOUS_LOG_PATH = EVENT_LOG_DIR / "errornous_MainProcess.xes"


def _indent(elem: ET.Element, level: int = 0) -> None:
    """In-place pretty-printer for XML trees (adds line breaks & spaces).

    ElementTree writes XML in a very compact single-line form by
    default. For manual inspection and comparison of the XES files, a
    simple indentation is much more readable. This helper inserts line
    breaks and indentation between elements.
    """

    indent_str = "  "  # two spaces per nesting level
    i = "\n" + level * indent_str
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = i + indent_str
        for child in elem:
            _indent(child, level + 1)
        if not elem.tail or not elem.tail.strip():
            elem.tail = i
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = i


def _attrs_from_children(elem: ET.Element) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {}
    for child in list(elem):
        key = child.attrib.get("key")
        if not key:
            continue
        value = child.attrib.get("value")
        if value is None:
            continue
        attrs[key] = value
    return attrs


def load_raw_events(path: Path | None = None) -> List[Dict[str, Any]]:
    """Parse XES log into a flat list of event dicts.

    Each event: {"trace_id", "event_index", "activity", "timestamp", "attrs": {...}}.
    """

    xes_path = path or RAW_LOG_PATH
    tree = ET.parse(xes_path)
    root = tree.getroot()

    # Namespaces in XES files vary; we ignore them via wildcard selectors
    events: List[Dict[str, Any]] = []
    trace_idx = 0
    for trace in root.findall(".//{*}trace"):
        t_attrs = _attrs_from_children(trace)
        trace_id = t_attrs.get("concept:name", f"trace_{trace_idx}")
        event_idx = 0
        for event in trace.findall("{*}event"):
            e_attrs = _attrs_from_children(event)
            activity = e_attrs.get("concept:name", "UNKNOWN_ACTIVITY")
            timestamp = e_attrs.get("time:timestamp", "")
            events.append(
                {
                    "trace_id": trace_id,
                    "event_index": event_idx,
                    "activity": activity,
                    "timestamp": timestamp,
                    "attrs": e_attrs,
                }
            )
            event_idx += 1
        trace_idx += 1
    # Keep original order from log which is already time-ordered within traces
    # For simulation we sort by timestamp+trace/event index as tie-breaker
    events.sort(key=lambda e: (e.get("timestamp", ""), e["trace_id"], e["event_index"]))
    return events


def _load_trace_templates() -> tuple[ET.Element, Dict[str, dict], List[str]]:
    """Load original XES log and prepare per-trace templates.

    Returns a tuple of
    - the original root element,
    - a mapping trace_id -> {"header_children": [...], "event_elems": [...]},
    - and the trace_id order as list.

    The *event_elems* list contains the original event elements of the
    trace in their original order. We use those as templates when
    rebuilding erroneous/validated/corrected logs so that type
    information and attribute structure are preserved.
    """

    tree_orig = ET.parse(RAW_LOG_PATH)
    root_orig = tree_orig.getroot()

    trace_templates: Dict[str, dict] = {}
    trace_order: List[str] = []
    trace_ids_seen: set[str] = set()

    for idx, trace_el in enumerate(root_orig.findall(".//{*}trace")):
        t_attrs = _attrs_from_children(trace_el)
        t_id = t_attrs.get("concept:name", f"trace_{idx}")
        trace_order.append(t_id)
        trace_ids_seen.add(t_id)

        header_children = []
        event_elems = []
        for child in list(trace_el):
            # All non-event nodes (e.g., the trace's concept:name and
            # other attributes) remain part of the trace header.
            if child.tag.endswith("event") or child.tag.endswith("}event"):
                event_elems.append(child)
            else:
                header_children.append(child)

        trace_templates[t_id] = {
            "header_children": header_children,
            "event_elems": event_elems,
        }

    return root_orig, trace_templates, trace_order


def ensure_validated_dir() -> Path:
    VALIDATED_DIR.mkdir(exist_ok=True)
    return VALIDATED_DIR


def ensure_json_dir() -> Path:
    JSON_DIR.mkdir(exist_ok=True)
    return JSON_DIR


def load_anomalies_queue() -> List[Dict[str, Any]]:
    """Load the persistent anomalies queue from JSON.

    If the file does not exist yet, an empty list is returned. This
    represents the backlog of issues that the Human Sensor can work on,
    independent of the current browser session.
    """

    if not ANOMALIES_PATH.exists():
        return []
    try:
        with ANOMALIES_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_anomalies_queue(queue: List[Dict[str, Any]]) -> None:
    """Persist the anomalies queue to a JSON file.

    This is updated whenever the Detection step finds new anomalies or
    the Human Sensor resolves issues and removes them from the queue.
    """

    ensure_json_dir()
    with ANOMALIES_PATH.open("w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)


def load_handled_issues() -> List[Dict[str, Any]]:
    """Load list of issues that were already handled by a Human Sensor.

    Prototype-level structure:
    [
        {
            "event_index": int,
            "error_type_id": str,
            "start_ts": str,   # optional ISO timestamp
            "finish_ts": str,  # optional ISO timestamp
        },
        ...
    ]
    """

    if not HANDLED_ISSUES_PATH.exists():
        return []
    try:
        with HANDLED_ISSUES_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_handled_issues(issues: List[Dict[str, Any]]) -> None:
    """Persist list of handled issues as simple JSON (append-only semantics)."""

    ensure_json_dir()
    with HANDLED_ISSUES_PATH.open("w", encoding="utf-8") as f:
        json.dump(issues, f, ensure_ascii=False, indent=2)


def load_simulation_state() -> Dict[str, Any]:
    """Load simulation progress/state persisted by the background worker.

    Structure (prototype-level):
    {"status": "running"|"finished", "current_index": int, "total": int}
    """

    if not SIM_STATE_PATH.exists():
        return {}
    try:
        with SIM_STATE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_simulation_state(state: Dict[str, Any]) -> None:
    """Persist simulation state so that any view can inspect progress."""

    ensure_json_dir()
    with SIM_STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def save_validated_events(events: List[Dict[str, Any]], name: str) -> Path:
    """Persist a versioned validated log as JSON for downstream DT modules."""

    ensure_validated_dir()
    out_path = VALIDATED_DIR / f"{name}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)
    return out_path


def save_decision_log(decisions: List[Dict[str, Any]]) -> Path:
    ensure_validated_dir()
    out_path = VALIDATED_DIR / "decisions_log.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(decisions, f, ensure_ascii=False, indent=2)
    return out_path


def _build_xes_from_events(
    events: List[Dict[str, Any]],
    out_path: Path,
    *,
    drop_marked: bool,
) -> Path:
    """Rebuild a XES log using the *original* log as template.

    Core idea:
    - Header/meta nodes (extensions, globals, classifiers, etc.) are
      copied 1:1 from the original log.
    - For each trace, the original event elements are used as
      templates. For each event in ``events`` the matching template
      event (by ``event_index``) is deep-copied and only the
      ``ev['attrs']`` and activity name (``concept:name``) are updated.
    - For duplicates with the same ``event_index`` the same template
      event is reused multiple times.
    - If ``drop_marked=True``, any event with
      ``attrs['validation:drop']`` is skipped in the output.

    As a result, ``MainProcess.xes``, ``errornous_MainProcess.xes``,
    ``MainProcess_validated.xes`` and ``corrected_MainProcess.xes`` keep
    the same structure and attribute typing; differences only appear
    where we rename activities, add attributes or explicitly remove
    duplicates.
    """

    root_orig, trace_templates, trace_order = _load_trace_templates()

    # Neues Log-Element mit denselben Meta-Informationen wie das Original
    log_el = ET.Element(root_orig.tag, root_orig.attrib)
    for child in list(root_orig):
        # Trace-Elemente werden aus den Events neu aufgebaut
        if child.tag.endswith("trace") or child.tag.endswith("}trace"):
            continue
        log_el.append(copy.deepcopy(child))

    # Group events by trace
    events_by_trace: Dict[str, List[Dict[str, Any]]] = {}
    for ev in events:
        t_id = ev.get("trace_id")
        if t_id is None:
            continue
        events_by_trace.setdefault(t_id, []).append(ev)

    # Within each trace restore the original order based on
    # ``event_index``; for duplicates the ``id`` field serves as an
    # additional, stable sort key.
    for t_id, evs in events_by_trace.items():
        evs.sort(key=lambda e: (int(e.get("event_index", 0)), int(e.get("id", 0))))

    # Rebuild traces according to the original order
    for t_id in trace_order:
        evs = events_by_trace.get(t_id)
        if not evs:
            continue

        tmpl = trace_templates.get(t_id) or {"header_children": [], "event_elems": []}
        tmpl_header = tmpl["header_children"]
        tmpl_events = tmpl["event_elems"]

        trace_el = ET.SubElement(log_el, "trace")

        # Header-Kinder (inkl. concept:name des Traces) kopieren
        for hdr in tmpl_header:
            trace_el.append(copy.deepcopy(hdr))

        # Fallback: if for some reason there is no concept:name in the
        # header (very unlikely), make sure one exists.
        if not any(
            (c.attrib.get("key") == "concept:name") for c in trace_el.findall("string")
        ):
            t_name = ET.SubElement(trace_el, "string")
            t_name.set("key", "concept:name")
            t_name.set("value", t_id)

        for ev in evs:
            attrs = dict(ev.get("attrs", {}))

            # Option: explicitly drop duplicates in the corrected log
            if drop_marked:
                drop_flag = attrs.get("validation:drop")
                if str(drop_flag).lower() in {"true", "1"}:
                    continue

            # Determine the template event; out-of-range indices are
            # mapped to the last known event of the trace.
            idx_orig = int(ev.get("event_index", 0))
            if tmpl_events:
                idx_orig = max(0, min(idx_orig, len(tmpl_events) - 1))
                base_el = tmpl_events[idx_orig]
                e_el = copy.deepcopy(base_el)
            else:
                # If a trace in the original log had no events (extremely
                # unlikely), create an empty event node.
                e_el = ET.SubElement(trace_el, "event")

            # Set activity name in both event fields and XES attribute
            activity = ev.get("activity", "")
            attrs["concept:name"] = activity
            if ev.get("timestamp"):
                attrs.setdefault("time:timestamp", ev["timestamp"])

            # Index existing attribute elements by key
            by_key: Dict[str, ET.Element] = {}
            for child in list(e_el):
                key = child.attrib.get("key")
                if key:
                    by_key[key] = child

            # Transfer attributes from the event into the XML node;
            # existing keys are overwritten, new keys are appended.
            for key, value in attrs.items():
                existing = by_key.get(key)
                if existing is not None:
                    existing.set("value", str(value))
                else:
                     # Type: reuse the type from the template if a
                     # matching attribute exists there; otherwise fall
                     # back to a simple string/date heuristic.
                    if key.startswith("time:"):
                        new_el = ET.Element("date")
                    else:
                        new_el = ET.Element("string")
                    new_el.set("key", key)
                    new_el.set("value", str(value))
                    e_el.append(new_el)

            trace_el.append(e_el)

    # Human-readable output with line breaks and indentation
    _indent(log_el)
    tree_new = ET.ElementTree(log_el)
    tree_new.write(out_path, encoding="utf-8", xml_declaration=True)
    return out_path


def save_modified_xes(events: List[Dict[str, Any]], path: Path | None = None) -> Path:
    """Export the *validated* log as XES using the original log as template.

    In contrast to an earlier synthetic export, this preserves the
    structure and attribute types of the original log.
    """

    out_path = path or (ensure_validated_dir() / "MainProcess_validated.xes")
    return _build_xes_from_events(events, out_path, drop_marked=False)


def save_errornous_xes(events: List[Dict[str, Any]], path: Path | None = None) -> Path:
    """Persist the *unvalidated* log with injected issues as XES.

    For the prototype this is built analogously to
    ``save_modified_xes`` but uses the dedicated file name
    ``errornous_MainProcess.xes``. The ``events`` list is expected to
    represent the state directly after artificial error injection (i.e.
    before any changes by Human Sensor / Knowledge Augmentator).
    """

    out_path = path or ERRONEOUS_LOG_PATH
    # Gleiche Struktur wie im Original-Log, aber mit injizierten Anomalien
    # (inkonsistente/unklare Terminologie, explizite Duplikate).
    return _build_xes_from_events(events, out_path, drop_marked=False)


def save_corrected_xes(events: List[Dict[str, Any]], path: Path | None = None) -> Path:
    """Export a corrected XES log based on the original log structure.

    In the corrected log all events marked by the Human Sensor as
    duplicates to be removed (``validation:drop = true``) are omitted.
    All other attributes and the structure of the original log are
    preserved.
    """

    out_path = path or CORRECTED_LOG_PATH
    return _build_xes_from_events(events, out_path, drop_marked=True)
