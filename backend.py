from __future__ import annotations

import json
import random
import copy
from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Any, Tuple

from data_layer import load_raw_events, save_simulation_state, save_errornous_xes, JSON_DIR
from knowledge_base import find_patterns_for_error_type


@dataclass
class DetectedIssue:
    event_idx: int
    error_type_id: str
    description: str
    confidence: float
    known: bool


def load_events_with_issues() -> List[Dict[str, Any]]:
    """Load raw events and inject artificial issues for the prototype."""

    events = load_raw_events()
    if not events:
        return []
    # Use the full event log (no truncation) so that the simulation
    # reflects the complete process execution.

    # Mark some events with artificial problems
    # Use stable RNG seed so that issues are reproducible across sessions
    rnd = random.Random(42)
    activities = sorted({e["activity"] for e in events})

    if activities:
        # Inconsistent terminology: import predefined synonym mapping

        with open(JSON_DIR / "synonym_events.json", "r", encoding="utf-8") as f:
            synonym_events = json.load(f)

        with open(JSON_DIR / "ambiguous_events.json", "r", encoding="utf-8") as f:
            ambiguous_events = json.load(f)
    # Phase 1: decide per event whether a specific error type
    # (inconsistent, imprecise, duplicate) should be injected.
    for ev in events:
        issue_flags: List[str] = []

        act = ev["activity"]
        attrs = ev.setdefault("attrs", {})
        # Keep the frequency of synthetic anomalies low so that the log
        # is mostly "clean" and issues appear as exceptions.
        if act in synonym_events and rnd.random() < 0.004:
            # Inconsistent terminology: use alternative label
            new_label = synonym_events[act]
            ev["activity"] = new_label
            # Also adjust the activity name in the attribute section so
            # that ``concept:name`` directly reflects the synonym.
            attrs["concept:name"] = new_label
            issue_flags.append("ET_INCONSISTENT_TERMINOLOGY")

        elif act in ambiguous_events and rnd.random() < 0.004:
            # Imprecise terminology – mark as ambiguous concept
            new_label = ambiguous_events[act]
            ev["activity"] = new_label
            # Again keep the attribute representation consistent so that
            # ``concept:name`` reflects the ambiguous label.
            attrs["concept:name"] = new_label
            issue_flags.append("ET_IMPRECISE_TERMINOLOGY")

        elif rnd.random() < 0.004:
            # Duplicated event – only mark as candidate here. The actual
            # duplicate event is created in phase 2.
            issue_flags.append("ET_DUPLICATED_EVENT")

        ev["sim_issue_types"] = issue_flags

    # Phase 2: for each event marked as a duplicate source, insert an
    # *exact copy* directly after it. The duplicate flag is moved to the
    # copied event so that the backlog only contains **one** entry per
    # duplicate pair (for the second, following event).
    new_events: List[Dict[str, Any]] = []
    for ev in events:
        flags = ev.get("sim_issue_types", [])
        is_dup_source = "ET_DUPLICATED_EVENT" in flags
        if is_dup_source:
            # The original event remains in the log but without the
            # duplicate flag.
            ev["sim_issue_types"] = [f for f in flags if f != "ET_DUPLICATED_EVENT"]
        new_events.append(ev)

        if is_dup_source:
            dup = copy.deepcopy(ev)
            # The following event is the actual duplicate and carries
            # the error marker.
            dup["sim_issue_types"] = ["ET_DUPLICATED_EVENT"]
            new_events.append(dup)

    # Assign stable IDs over the *extended* event list so that
    # simulation, queue and UI share a consistent index space.
    for idx, ev in enumerate(new_events):
        ev["id"] = idx

    events = new_events

    # Persist the initial simulation state after duplicate creation so
    # that "total" reflects the actual number of events including
    # duplicates.
    save_simulation_state({"status": "idle", "current_index": 0, "total": len(events)})

    # Persist this state (original log + artificially injected issues,
    # including explicit duplicates but **without** any human
    # intervention) once as `errornous_MainProcess.xes`. Later
    # validation steps only modify events in memory, not this file, so a
    # clean comparison between "erroneous" and "cleaned" remains
    # possible.
    save_errornous_xes(events)
    return events


def run_detection_simulation(events: List[Dict[str, Any]], kb: Dict[str, Any], start_index: int = 0) -> None:
    """Background-like simulation loop that updates anomalies queue + state.

    This function is intended to be executed in a separate process or
    terminal (e.g., `python simulation_worker.py`). The Streamlit UI
    only reads from the anomalies queue and simulation_state files to
    reflect the current status.
    """

    from data_layer import load_anomalies_queue, save_anomalies_queue
    import time as _time

    queue = load_anomalies_queue()
    existing_keys = {
        (entry["event_index"], entry["issue"]["error_type_id"])
        for entry in queue
    }

    for i in range(start_index, len(events)):
        ev = events[i]
        issues = detect_issues_for_event(ev, kb)
        changed = False
        for issue in issues:
            key = (i, issue.error_type_id)
            if key not in existing_keys:
                queue.append(
                    {
                        "event_index": i,
                        "issue": {
                            "error_type_id": issue.error_type_id,
                            "description": issue.description,
                            "confidence": issue.confidence,
                            "known": issue.known,
                            "detected_at": datetime.utcnow().isoformat(),
                        },
                    }
                )
                existing_keys.add(key)
                changed = True

        if changed:
            save_anomalies_queue(queue)

        # Persist current progress for the UI
        save_simulation_state(
            {
                "status": "running",
                "current_index": i,
                "total": len(events),
            }
        )

        # Small delay for a "near real-time" feel (roughly 60ms per
        # event so that the simulation does not finish instantly).
        _time.sleep(0.06)

    save_simulation_state(
        {
            "status": "finished",
            "current_index": len(events) - 1 if events else 0,
            "total": len(events),
        }
    )


def detect_issues_for_event(
    event: Dict[str, Any], kb: Dict[str, Any]
) -> List[DetectedIssue]:
    issues: List[DetectedIssue] = []
    issue_types: List[str] = event.get("sim_issue_types", [])
    for et_id in issue_types:
        patterns = find_patterns_for_error_type(kb, et_id)
        known = bool(patterns)
        confidence = 0.9 if known else 0.5
        issues.append(
            DetectedIssue(
                event_idx=event["id"],
                error_type_id=et_id,
                description=et_id,
                confidence=confidence,
                known=known,
            )
        )
    return issues


def apply_solution(
    event: Dict[str, Any], pattern: Dict[str, Any]
) -> Tuple[Dict[str, Any], str]:
    """Apply a simple solution pattern to an event.

    Supported pattern params:
    - {"action": "rename_activity", "from": str, "to": str}
    - {"action": "mark_duplicate", "keep": bool}
    """

    params = pattern.get("params", {})
    action = params.get("action")
    updated = dict(event)
    explanation = "No change applied"

    if action == "rename_activity":
        if updated["activity"] == params.get("from"):
            new_label = params.get("to")
            updated["activity"] = new_label
            attrs = updated.setdefault("attrs", {})
            # Normalisierte Bezeichnung in separatem Feld festhalten
            attrs["concept:normalized"] = new_label
            # Und auch den eigentlichen Aktivitätsnamen im Attributbereich
            # (concept:name) auf das Synonym setzen, damit Event-Feld und
            # XES-Attribut konsistent sind.
            attrs["concept:name"] = new_label
            explanation = "Activity name normalized using solution pattern."
    elif action == "mark_duplicate":
        if params.get("keep") is False:
            updated["attrs"]["validation:drop"] = True
            explanation = "Event marked for removal as duplicate."
        else:
            updated["attrs"]["validation:duplicate_group"] = "kept"
            explanation = "Duplicate event kept and labeled."
    return updated, explanation


def log_decision(
    decisions: List[Dict[str, Any]],
    event: Dict[str, Any],
    step: str,
    actor_role: str,
    action: str,
    reason: str,
    confidence_before: float,
    confidence_after: float,
) -> None:
    decisions.append(
        {
            "timestamp": datetime.utcnow().isoformat(),
            "event_id": event.get("id"),
            "trace_id": event.get("trace_id"),
            "step": step,
            "actor_role": actor_role,
            "action": action,
            "reason": reason,
            "confidence_before": confidence_before,
            "confidence_after": confidence_after,
        }
    )


def detection_handling_summary(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(events)
    flagged = sum(bool(e.get("sim_issue_types")) for e in events)
    return {"total_events": total, "events_with_issues": flagged}
