from __future__ import annotations

import json
import time
from datetime import datetime
import threading

import streamlit as st
import streamlit.components.v1 as components

from backend import (
    load_events_with_issues,
    apply_solution,
    log_decision,
    run_detection_simulation,
)
from data_layer import (
    save_validated_events,
    save_decision_log,
    save_modified_xes,
    save_corrected_xes,
    load_anomalies_queue,
    save_anomalies_queue,
    load_handled_issues,
    save_handled_issues,
    load_simulation_state,
)
from knowledge_base import (
    load_kb,
    save_kb,
    list_error_types,
    list_solution_patterns,
    add_solution_pattern,
    add_error_type,
    update_error_type,
    delete_error_type,
    update_solution_pattern,
    delete_solution_pattern,
    as_graph,
)


st.set_page_config(page_title="Human-Supported Data Validation", layout="wide", page_icon="👨‍💻")


# --- KIT / SYDSEN design constants ---
KIT_GREEN = "#009682"  # RGB 0/150/130
KIT_BLUE = "#4664AA"   # RGB 70/100/170
KIT_GREY = "#404040"   # RGB 64/64/64


# Flag auf Prozessebene, ob der Backlog (Anomalien + handled issues)
# bereits einmal beim App-Start zurückgesetzt wurde. So wird der Backlog
# nur bei einem echten Neustart der App geleert, nicht bei jedem neuen
# Browser-Tab.
BACKLOG_RESET_DONE = False


def _safe_rerun() -> None:
    """Compatibility wrapper for Streamlit rerun.

    Uses `st.experimental_rerun` if available (older versions),
    otherwise `st.rerun` (neuere Versionen). Falls beides nicht
    existiert, passiert einfach nichts und die Seite aktualisiert
    sich nicht automatisch.
    """

    fn = getattr(st, "experimental_rerun", None) or getattr(st, "rerun", None)
    if callable(fn):
        fn()


def make_realtime_view(ev: dict) -> dict:
    """Return a copy of the event with shifted timestamps for realism.

    - The *relative* time differences between events remain identical
      to the original XES log.
    - The entire trace is shifted so that the first event appears at
      the current wall-clock time when the app is (first) used.
    - Both the top-level ``timestamp`` field and the XES attribute
      ``time:timestamp`` (and other ``time:*`` attributes) are updated
      in the returned copy.

    The underlying data (und damit der Original-Log) bleibt unverändert
    im Backend erhalten; die Anpassung erfolgt nur für die Darstellung.
    """

    # Shallow copy des Events + eigene Kopie der Attribute, damit wir
    # nur die Darstellung verändern und nicht die Originaldaten.
    shown = dict(ev)
    attrs = dict(ev.get("attrs", {}))
    shown["attrs"] = attrs

    # Originaler Zeitstempel (aus dem Feld oder aus den XES-Attributen)
    orig_ts_str = shown.get("timestamp") or attrs.get("time:timestamp")

    # Hilfsfunktion für einen aktuellen ISO‑Zeitstempel
    def _now_iso() -> str:
        return datetime.now().isoformat(timespec="seconds")

    # Wenn gar kein Zeitstempel im Original vorhanden ist, lassen wir
    # alle Felder unverändert leer (dein Wunsch: fehlende Timestamps
    # sollen auch in der Darstellung leer bleiben).
    if not orig_ts_str:
        return shown

    from datetime import datetime as _dt

    def _parse_iso(s: str):
        try:
            # XES-Timestamps können ein "Z" tragen; Python erwartet
            # ein explizites Offset wie +00:00.
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return _dt.fromisoformat(s)
        except Exception:
            return None

    orig_dt = _parse_iso(orig_ts_str)
    # Falls ein Zeitstempel vorhanden ist, aber nicht geparst werden
    # kann, verwenden wir den aktuellen Zeitpunkt als Fallback.
    if orig_dt is None:
        now_str = _now_iso()
        shown["timestamp"] = now_str
        if "time:timestamp" in attrs:
            attrs["time:timestamp"] = now_str
        # operation_end_time: wenn vorhanden, ebenfalls auf "jetzt"
        # setzen, da der ursprüngliche Wert unlesbar ist.
        if "operation_end_time" in attrs and attrs["operation_end_time"]:
            attrs["operation_end_time"] = now_str
        return shown

    # Baseline für diese Session bestimmen: erster Eventzeitpunkt der
    # Simulation (log_start) wird auf den aktuellen Zeitpunkt
    # (real_start) gemappt, alle anderen Events behalten ihre
    # ursprünglichen zeitlichen Abstände relativ dazu.
    if "sim_time_origin" not in st.session_state:
        st.session_state.sim_time_origin = {
            "real_start": _dt.now(),
            "log_start": orig_dt,
        }

    origin = st.session_state.sim_time_origin
    log_start_dt = origin["log_start"]
    real_start_dt = origin["real_start"]

    delta = orig_dt - log_start_dt
    sim_dt = real_start_dt + delta
    sim_str = sim_dt.isoformat(timespec="seconds")

    # Oberflächen-Zeitstempel anpassen
    shown["timestamp"] = sim_str

    # Alle XES-Zeitattribute im Event entsprechend verschieben, so dass
    # ihre Abstände im Vergleich zum ersten Log-Zeitstempel erhalten
    # bleiben.
    for key, value in list(attrs.items()):
        if key.startswith("time:") and value:
            dt_attr = _parse_iso(str(value))
            if dt_attr is None:
                # Unlesbare Zeitwerte → aktueller Zeitpunkt
                attrs[key] = _now_iso()
            else:
                d_attr = dt_attr - log_start_dt
                attrs[key] = (real_start_dt + d_attr).isoformat(timespec="seconds")

    # Spezieller zweiter Zeitstempel im Datensatz: operation_end_time
    # soll ebenfalls realistisch verschoben werden, wobei sein Abstand
    # zum ursprünglichen Event-Zeitstempel erhalten bleibt.
    end_str = attrs.get("operation_end_time")
    if end_str:
        dt_end = _parse_iso(str(end_str))
        if dt_end is None:
            # Wert vorhanden aber unlesbar → aktueller Zeitpunkt
            attrs["operation_end_time"] = _now_iso()
        else:
            d_end = dt_end - log_start_dt
            attrs["operation_end_time"] = (real_start_dt + d_end).isoformat(timespec="seconds")

    return shown


def apply_kit_theme() -> None:
    """Inject lightweight CSS using KIT/SYDSEN color palette.

    This keeps the prototype simple while visually aligning the interface
    with the provided corporate design.
    """

    st.markdown(
        f"""
        <style>
        :root {{
            --primary-color: {KIT_BLUE};
            --secondary-color: {KIT_GREEN};
            --text-color-main: #FFFFFF;
            --text-color-sidebar: #FFFFFF;
        }}

        /* Main screen in KIT grey */
        .stApp {{
            color: var(--text-color-main);
            background-color: {KIT_GREY};
        }}

        /* Headings in KIT blue (accent color) */
        h1, h2, h3, h4 {{
            color: var(--primary-color);
        }}

        /* Accent color for buttons */
        button[kind="primary"] {{
            background-color: var(--primary-color) !important;
            border-color: var(--primary-color) !important;
        }}

        /* Sidebar in KIT green */
        section[data-testid="stSidebar"] {{
            background-color: {KIT_GREEN};
            color: var(--text-color-sidebar);
        }}

        section[data-testid="stSidebar"] * {{
            color: var(--text-color-sidebar) !important;
        }}

        /* Custom "menu" styling for role selection in the sidebar */
        section[data-testid="stSidebar"] .stRadio > div {{
            flex-direction: column;
            align-items: stretch;
        }}

        section[data-testid="stSidebar"] .stRadio label {{
            display: block;
            padding: 0.35rem 0.75rem;
            border-radius: 0.25rem;
            margin-bottom: 0.15rem;
            cursor: pointer;
        }}

        /* Hide the default round radio indicators completely
           so that only the menu-like list items remain visible. */
        section[data-testid="stSidebar"] .stRadio input[type="radio"] {{
            display: none !important;
        }}

        section[data-testid="stSidebar"] .stRadio svg {{
            display: none !important;
        }}

        /* Hover state in KIT blue */
        section[data-testid="stSidebar"] .stRadio label:hover {{
            background-color: var(--primary-color);
        }}

        /* Active (selected) state in KIT blue – keep the full row blue
           as long as the item is selected. */
        section[data-testid="stSidebar"] .stRadio div[role="radio"][aria-checked="true"],
        section[data-testid="stSidebar"] .stRadio div[role="radio"][aria-checked="true"] + label,
        section[data-testid="stSidebar"] .stRadio label[data-checked="true"] {{
            background-color: var(--primary-color);
            border-radius: 0.25rem;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_header() -> None:
    """Main area header without logos (logos live in sidebar)."""

    st.markdown(
        "<h1 style='text-align:center;'>Human-Supported Data Validation</h1>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<p style='text-align:center;'>Digital Twin Data Validation Prototype</p>",
        unsafe_allow_html=True,
    )


def render_sidebar_logos() -> None:
    """Show KIT and SYDSEN logos at the top of the sidebar, stacked."""

    with st.sidebar:
        st.image("design/Logo_KIT.svg.png", width=180)
        st.image("design/logo_sydsen_hor1 copy 4 - Copy.png", width=180)


def init_state() -> None:
    global BACKLOG_RESET_DONE
    # Flag, ob der Hintergrund-Simulations-Thread bereits gestartet wurde
    if "sim_thread_started" not in st.session_state:
        st.session_state.sim_thread_started = False

    if "events" not in st.session_state:
        # Beim allerersten Aufruf nach App-Neustart den Backlog
        # vollständig leeren (Anomalien + bereits bearbeitete Issues),
        # damit jede Simulation mit einem frischen Backlog startet.
        if not BACKLOG_RESET_DONE:
            save_anomalies_queue([])
            save_handled_issues([])
            BACKLOG_RESET_DONE = True

        # Neuer App-Lauf: Events & KB laden und dabei den Backlog
        # explizit leeren, damit frühere Simulationen keinen Einfluss
        # auf diesen Durchlauf haben.
        st.session_state.events = load_events_with_issues() # Load events from the original log and randomly manipulate to make some events errornous
        st.session_state.current_idx = 0
        st.session_state.decisions = []
        st.session_state.kb = load_kb() # Load the knowledge base, which includes error types and solution patterns

        # Backlog/Queue & handled issues beim App-Neustart nur im State
        # zurücksetzen; die eigentliche anomalies_queue.json wird allein
        # vom System-Thread beschrieben.
        st.session_state.issue_queue = []
        st.session_state.handled_issues = load_handled_issues() # Load the list of issues that were already handled by a Human Sensor, to avoid showing them again in the backlog. (empty when starting the app, but will be populated as the Human Sensor handles issues)
        st.session_state.outputs_persisted = False
    # Ensure queues exist even if state was created in an older session
    if "issue_queue" not in st.session_state:
        st.session_state.issue_queue = []
    if "handled_issues" not in st.session_state:
        st.session_state.handled_issues = load_handled_issues() # Load the list of issues that were already handled by a Human Sensor, to avoid showing them again in the backlog. (empty when starting the app, but will be populated as the Human Sensor handles issues)

    if "outputs_persisted" not in st.session_state:
        st.session_state.outputs_persisted = False

    # Für die Messung der Bearbeitungszeiten pro Issue
    if "last_issue_finish_ts" not in st.session_state:
        st.session_state.last_issue_finish_ts = None
    if "issue_backlog_was_empty" not in st.session_state:
        # Zu Beginn ist der Backlog leer (noch keine Issues erkannt)
        st.session_state.issue_backlog_was_empty = True

    # Hintergrund-Simulation einmal pro Browser-Session starten
    if not st.session_state.sim_thread_started:
        events = st.session_state.events
        kb = st.session_state.kb

        def _worker() -> None:
            run_detection_simulation(events, kb, start_index=0) # start the simulated event replay (executed in a separate thread)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        st.session_state.sim_thread_started = True


def maybe_persist_outputs() -> None:
    """Persist validated artifacts once simulation is finished & all issues handled.

    Regeln laut Nutzerwunsch:
    - Die Daten werden *automatisch* gespeichert, sobald
      1) die Simulation vollständig durchgelaufen ist und
      2) alle vom System erkannten Issues bearbeitet wurden.
    - Das korrigierte Log wird vollständig aus den simulierten Events
      aufgebaut (siehe ``save_corrected_xes``).
    """

    # Mehrfaches Schreiben im selben Run vermeiden
    if st.session_state.get("outputs_persisted"):
        return

    events = st.session_state.get("events") or []
    decisions = st.session_state.get("decisions") or []

    # 1) Simulation muss fertig sein
    state = load_simulation_state() or {}
    if state.get("status") != "finished":
        return

    # 2) Es dürfen keine offenen Issues im Backlog sein
    raw_queue = load_anomalies_queue()
    handled = load_handled_issues() or []
    handled_keys = {
        (h.get("event_index"), h.get("error_type_id"))
        for h in handled
        if isinstance(h, dict)
    }

    open_queue = [
        item
        for item in raw_queue
        if isinstance(item, dict)
        and (
            item.get("event_index"),
            item.get("issue", {}).get("error_type_id"),
        ) not in handled_keys
    ]

    if open_queue:
        return

    # Bedingungen erfüllt → Artefakte persistieren
    # Validierte Artefakte (JSON + XES-Varianten + Decisions-Log)
    out_json = save_validated_events(events, "validated_events")
    out_xes = save_modified_xes(events)
    out_corrected = save_corrected_xes(events)
    out_decisions = save_decision_log(decisions)

    st.session_state.outputs_persisted = True

    # Show a short info message in the active view
    st.success(
        "Simulation finished and all issues handled. "
        "All resulting Event Logs were persisted automatically."
    )


def role_navigation() -> None:
    """Sidebar role navigation.

    Three roles/views:
    - System: pure simulation & automatic detection ("System role")
    - Human Sensor: handles the anomalies backlog
    - Knowledge Augmentator: knowledge repository & ontology
    """

    st.sidebar.header("Role View")

    if "active_view" not in st.session_state:
        # Default: System role for the simulation
        st.session_state.active_view = "System"

    options = ["System", "Human Sensor", "Knowledge Augmentator"]
    current = st.session_state.active_view
    try:
        idx = options.index(current)
    except ValueError:
        idx = 0

    view = st.sidebar.radio(
        "Choose your current perspective",
        options,
        index=idx,
    )
    st.session_state.active_view = view


def ui_detection() -> None:
    st.header("Data Quality Issue Detection – Simulated process execution replay")
    events = st.session_state.events
    if not events:
        st.info("No events loaded from MainProcess.xes")
        return

    st.write(
        "Use this screen to observe the **system status**. "
        "The actual handling of anomalies occurs in the "
        "*Human Sensor* view, which operates on the persistent "
        "`anomalies_queue.json` file. \n\n"
        
        "The replay runs automatically in a background thread, "
        "as soon as the app is launched. This screen displays the current "
        "progress and the most recent event from the simulation and "
        "updates automatically about once per second."
    )

    # Keep the simulation state stable in the UI: remember the last
    # known state in the session and only overwrite it when the JSON
    # file provides new values. This prevents the progress bar from
    # jumping back to 0 on transient read issues.

    # Set initial defaults only on the very first call
    if "sim_status" not in st.session_state:
        st.session_state.sim_status = "idle"
    if "sim_current_index" not in st.session_state:
        st.session_state.sim_current_index = 0
    if "sim_total" not in st.session_state:
        st.session_state.sim_total = len(events)

    state = load_simulation_state() or {}

    # Only copy keys that are present in the file; missing keys keep the
    # last known values.
    if "status" in state:
        st.session_state.sim_status = state["status"]
    if "current_index" in state:
        st.session_state.sim_current_index = state["current_index"]
    if "total" in state:
        st.session_state.sim_total = state["total"]

    status = st.session_state.sim_status
    current_idx = st.session_state.sim_current_index
    total = st.session_state.sim_total

    st.subheader("Replay Status")
    st.write(f"Status: `{status}`")
    st.progress(min(max(current_idx + 1, 1), total) / max(total, 1))
    st.write(f"Event {current_idx + 1} of {total} (chronologically sorted)")

    if 0 <= current_idx < len(events):
        ev = events[current_idx]
        st.subheader("Currently replayed event")
        # Show the full event including all attributes so that humans
        # can assess anomalies in full context (all XES attributes).
        st.json(make_realtime_view(ev))

        # Show whether any anomalies have been detected for this event
        from data_layer import load_anomalies_queue

        queue = load_anomalies_queue()
        current_issues = [
            item for item in queue if item.get("event_index") == current_idx
        ]

        st.subheader("Anomaly status for this event")
        if current_issues:
            st.warning(
                f"For this event, {len(current_issues)} anomaly(ies) were detected."
            )

            issues_preview = []
            for entry in current_issues:
                iss = entry.get("issue", {})
                issues_preview.append(
                    {
                        "Error Type": iss.get("error_type_id"),
                        "Confidence": round(iss.get("confidence", 0.0), 2),
                        "Known": iss.get("known"),
                        "Detected at": iss.get("detected_at"),
                    }
                )
            st.table(issues_preview)
        else:
            st.success(
                "For this event, no anomalies were detected in the current simulation."
            )
    else:
        st.info("No events simulated yet or simulation already completed.")

    # automatische Aktualisierung jede Sekunde, solange der Nutzer
    # auf der System-Ansicht bleibt
    time.sleep(1)
    _safe_rerun()


def ui_handling_and_solving(step: str) -> None:
    events = st.session_state.events
    kb = st.session_state.kb
    # In the Human Sensor view the backlog is loaded from the persistent
    # JSON queue so that the System role (simulation) and Human Sensor
    # can work in parallel, even from separate browser sessions. To be
    # safe we filter out entries whose event_index no longer matches the
    # currently loaded events (e.g., after switching logs or
    # event limits) to avoid index errors.
    raw_queue = load_anomalies_queue()
    handled = st.session_state.handled_issues or []
    handled_keys = {
        (h.get("event_index"), h.get("error_type_id")) for h in handled
        if isinstance(h, dict)
    }

    # The queue is only written by the System; the Human Sensor filters
    # out issues that have already been handled.
    queue = [
        item
        for item in raw_queue
        if isinstance(item, dict)
        and isinstance(item.get("event_index"), int)
        and 0 <= item["event_index"] < len(events)
        and (
            item["event_index"], item.get("issue", {}).get("error_type_id")
        ) not in handled_keys
    ]

    # Flag that indicates whether the backlog was empty *before* this
    # render. It is used to derive the start time for the first issue
    # in a new "handling session".
    was_empty_before = st.session_state.get("issue_backlog_was_empty", True)

    st.header(f"Data Quality Issue {step} – Human-supported resolution")

    if not queue:
        st.info(
            "Currently, there are no open issues from the simulation or all "
            "existing queue entries refer to events that are no longer present in this "
            "run."
        )
        # Backlog ist leer – die nächste neu auftretende Anomalie
        # gilt wieder als Startpunkt einer neuen Bearbeitungssession.
        st.session_state.issue_backlog_was_empty = True
        return

    # From here on the backlog is *not* empty.
    st.session_state.issue_backlog_was_empty = False

    # Overview of all open issues from the persistent JSON queue as a
    # table, analogous to the Knowledge view. The queue position is
    # already encoded in the data so we do not add a separate row index.
    overview_rows = []
    for idx, item in enumerate(queue):
        ev_idx = item["event_index"]
        iss = item["issue"]
        ev_i = events[ev_idx]
        overview_rows.append(
            {
                "Queue position": idx + 1,
                "Event #": ev_idx + 1,
                "Activity": ev_i.get("activity", ""),
                "Error Type": iss.get("error_type_id"),
                #"Confidence": round(iss.get("confidence", 0.0), 2),
                #"Known": iss.get("known"),
                "Detected at": iss.get("detected_at"),
            }
        )

    # Show the backlog in an expandable section so the view remains
    # readable even with many entries. Handling is strictly FIFO: always
    # the oldest (top) issue is processed. We hide the row index so only
    # the semantic "queue position" column is visible.
    with st.expander("Open Issues from the Simulation (Backlog)", expanded=True):
        st.dataframe(overview_rows, hide_index=True, width="stretch")

    # Currently processed issue (FIFO: first in the queue)
    current = queue[0]
    ev_index = current["event_index"]
    issue = current["issue"]
    ev = events[ev_index]

    st.markdown("---")
    st.write(f"Open Issues in Queue: {len(queue)}")
    st.write("Edit event", ev_index + 1, "with issue:", issue.get("error_type_id"))

    # Special view for duplicate errors: original event and duplicate
    # are shown below/next to each other so the Human Sensor can compare
    # them directly.
    is_duplicate_issue = issue.get("error_type_id") == "ET_DUPLICATED_EVENT"
    if is_duplicate_issue and ev_index > 0:
        base_ev = events[ev_index - 1]
    else:
        base_ev = None

    if is_duplicate_issue and base_ev is not None and base_ev.get("trace_id") == ev.get("trace_id"):
        st.subheader("Duplicate candidate – pair view")
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Original event** (kept in any case)")
            st.json(make_realtime_view(base_ev))
        with col2:
            st.markdown("**Duplicate event** (currently under review)")
            st.json(make_realtime_view(ev))
    else:
        # Default: single-event view with all XES attributes.
        st.json(make_realtime_view(ev))

    # --- Select or define error type ---
    # Sort error types alphabetically by description so that the Human
    # Sensor finds familiar types quickly.
    error_types = list_error_types(kb)
    sorted_error_types = sorted(
        error_types,
        key=lambda et: (et.get("description") or "").lower(),
    )
    et_labels = {et.get("description", ""): et["id"] for et in sorted_error_types}
    NEW_ERROR_LABEL = "Create new error type"
    et_options = list(et_labels.keys()) + [NEW_ERROR_LABEL]

    # Standardauswahl: der vom System erkannte Fehlertyp dieses Issues
    detected_et_id = issue.get("error_type_id")
    detected_label = None
    for desc, et_id in et_labels.items():
        if et_id == detected_et_id:
            detected_label = desc
            break

    if detected_label in et_options:
        default_index = et_options.index(detected_label)
    else:
        default_index = 0

    selected_label = st.selectbox(
        "Confirm / change error type",
        et_options,
        index=default_index,
    )

    # Case 1: user wants to define a new error type from the dropdown
    if selected_label == NEW_ERROR_LABEL:
        st.subheader("Define new error type")
        new_et_id = st.text_input(
            "New error type ID (e.g., ET_SENSOR_DRIFT)", key="hs_new_et_id"
        )
        new_et_description = st.text_input(
            "New error type description", key="hs_new_et_label"
        )
        st.markdown("---")
        st.subheader("Define new solution pattern")
        action = st.selectbox(
            "Action",
            ["rename_activity", "mark_duplicate"],
            key="hs_new_sp_action",
        )
        params = {"action": action}
        if action == "rename_activity":
            params["from"] = st.text_input(
                "From activity",
                value=ev.get("activity", ""),
                key="hs_new_sp_from",
            )
            params["to"] = st.text_input(
                "To activity",
                key="hs_new_sp_to",
            )
        else:
            keep = st.checkbox(
                "Keep this duplicate",
                value=False,
                key="hs_new_sp_keep",
            )
            params["keep"] = keep

        desc = st.text_input(
            "Short description of this solution pattern",
            key="hs_new_sp_desc",
        )

        if st.button(
            "Create error type, save pattern and apply",
            key="hs_create_et_sp_btn",
        ):
            if not new_et_id or not new_et_description or not desc:
                st.warning(
                    "Please provide at least an ID and description for the new error "
                    "type and a description for the solution pattern."
                )
            else:
                 # 1) Create error type
                kb = add_error_type(
                    kb, new_et_id, new_et_description
                )
                 # 2) Create solution pattern for this error type
                kb = add_solution_pattern(kb, new_et_id, desc, params)
                save_kb(kb)
                st.session_state.kb = kb

                 # 3) Apply pattern
                pattern = kb["solution_patterns"][-1]
                updated, explanation = apply_solution(ev, pattern)
                confidence_before = issue.get("confidence", 0.0)
                confidence_after = 0.9
                log_decision(
                    st.session_state.decisions,
                    ev,
                    step,
                    "Human Sensor",
                    f"Created error type {new_et_id} and pattern {pattern['id']}",
                    explanation,
                    confidence_before,
                    confidence_after,
                )
                st.session_state.events[ev_index] = updated

                 # 4) Mark current issue as handled (append-only list)
                handled = st.session_state.handled_issues or []
                finish_ts = datetime.utcnow().isoformat()
                last_finish = st.session_state.get("last_issue_finish_ts")
                if was_empty_before or last_finish is None:
                    start_ts = issue.get("detected_at") or finish_ts
                else:
                    start_ts = last_finish
                handled.append(
                    {
                        "event_index": ev_index,
                        "error_type_id": issue.get("error_type_id"),
                        "start_ts": start_ts,
                        "finish_ts": finish_ts,
                    }
                )
                st.session_state.handled_issues = handled
                st.session_state.last_issue_finish_ts = finish_ts
                save_handled_issues(handled)

                st.success(
                    "New error type and solution pattern stored. "
                    "Solution applied and issue removed from queue."
                )
                 # Refresh UI so that the next issue is loaded immediately
                _safe_rerun()
        # In this path we do not show any additional pattern selection
        return

    # Case 2: confirm or change an existing error type
    confirmed_et_id = et_labels[selected_label]

    patterns = list_solution_patterns(kb)
    patterns_for_et = [
        p for p in patterns if p["error_type_id"] == confirmed_et_id
    ]
    # Sort solution patterns alphabetically by description
    pattern_descriptions = sorted(
        (p.get("description") or "") for p in patterns_for_et
    )
    pattern_options = ["<Create new solution pattern>"] + pattern_descriptions
    choice = st.selectbox("Select solution pattern", pattern_options)

    if choice == "<Create new solution pattern>":
        st.subheader("Define new solution pattern")
        action = st.selectbox(
            "Action",
            ["rename_activity", "mark_duplicate"],
        )
        params = {"action": action}
        if action == "rename_activity":
            params["from"] = st.text_input(
                "From activity", value=ev.get("activity", "")
            )
            params["to"] = st.text_input("To activity")
        else:
            keep = st.checkbox("Keep this duplicate", value=False)
            params["keep"] = keep
        desc = st.text_input("Short description of this solution pattern")

        if st.button("Save pattern and apply") and desc:
            kb = add_solution_pattern(kb, confirmed_et_id, desc, params)
            save_kb(kb)
            st.session_state.kb = kb
            updated, explanation = apply_solution(ev, kb["solution_patterns"][-1])
            confidence_before = issue.get("confidence", 0.0)
            confidence_after = 0.9
            log_decision(
                st.session_state.decisions,
                ev,
                step,
                "Human Sensor",
                f"Created pattern {kb['solution_patterns'][-1]['id']}",
                explanation,
                confidence_before,
                confidence_after,
            )
            st.session_state.events[ev_index] = updated
            # Mark current issue as handled (append-only list)
            handled = st.session_state.handled_issues or []
            finish_ts = datetime.utcnow().isoformat()
            last_finish = st.session_state.get("last_issue_finish_ts")
            if was_empty_before or last_finish is None:
                start_ts = issue.get("detected_at") or finish_ts
            else:
                start_ts = last_finish
            handled.append(
                {
                    "event_index": ev_index,
                    "error_type_id": issue.get("error_type_id"),
                    "start_ts": start_ts,
                    "finish_ts": finish_ts,
                }
            )
            st.session_state.handled_issues = handled
            st.session_state.last_issue_finish_ts = finish_ts
            save_handled_issues(handled)
            st.success(
                "Pattern stored and solution applied. Issue removed from queue."
            )
            # Refresh UI so that the next issue is loaded immediately
            _safe_rerun()
    else:
        selected_pattern = next(
            p for p in patterns_for_et if p["description"] == choice
        )
        if st.button("Apply selected pattern"):
            updated, explanation = apply_solution(ev, selected_pattern)
            confidence_before = issue.get("confidence", 0.0)
            confidence_after = 0.9
            log_decision(
                st.session_state.decisions,
                ev,
                step,
                "Human Sensor",
                f"Applied pattern {selected_pattern['id']}",
                explanation,
                confidence_before,
                confidence_after,
            )
            st.session_state.events[ev_index] = updated
            # Mark current issue as handled (append-only list)
            handled = st.session_state.handled_issues or []
            finish_ts = datetime.utcnow().isoformat()
            last_finish = st.session_state.get("last_issue_finish_ts")
            if was_empty_before or last_finish is None:
                start_ts = issue.get("detected_at") or finish_ts
            else:
                start_ts = last_finish
            handled.append(
                {
                    "event_index": ev_index,
                    "error_type_id": issue.get("error_type_id"),
                    "start_ts": start_ts,
                    "finish_ts": finish_ts,
                }
            )
            st.session_state.handled_issues = handled
            st.session_state.last_issue_finish_ts = finish_ts
            save_handled_issues(handled)
            st.success(
                "Solution applied using existing pattern. Issue removed from queue."
            )
            # Refresh UI so that the next issue is loaded immediately
            _safe_rerun()


def ui_knowledge_repo() -> None:
    """Knowledge repository view with inline-editable tables for the augmentator.

    - Error types and solution patterns can be edited directly in tables
      (add, change, delete rows) without separate forms.
    - The edges view below provides a structural overview of the ontology
      / knowledge graph (read‑only).
    """

    st.header("Knowledge repository – Ontology & graph view")
    kb = st.session_state.kb

    # --- Inline editing of error types ---
    st.subheader("Error types (inline editable)")
    error_types = list_error_types(kb)
    error_rows = [
        {
            "id": et.get("id", ""),
            "description": et.get("description", ""),
        }
        for et in error_types
    ]

    edited_error_rows = st.data_editor(
        error_rows,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        key="kb_error_editor",
        column_config={
            "id": st.column_config.TextColumn(
                "id",
                help="Internal ID; changing it is treated as delete + new entry",
            ),
            "description": st.column_config.TextColumn("description"),
        },
    )

    if st.button("Save error type changes", key="kb_save_errors"):
        existing_ids = {et["id"] for et in error_types}
        edited_ids = {row["id"] for row in edited_error_rows if row.get("id")}

        # Deleted error types (and their patterns)
        deleted_ids = existing_ids - edited_ids
        for et_id in deleted_ids:
            kb = delete_error_type(kb, et_id)

        # New or updated error types
        for row in edited_error_rows:
            et_id = row.get("id")
            if not et_id:
                continue
            desc = row.get("description", "")
            if et_id not in existing_ids:
                kb = add_error_type(kb, et_id, desc or None)
            else:
                kb = update_error_type(kb, et_id, desc or None)

        save_kb(kb)
        st.session_state.kb = kb
        st.success("Error types updated from table.")

    # --- Inline editing of solution patterns ---
    st.subheader("Solution patterns (inline editable)")
    patterns = list_solution_patterns(kb)
    sp_rows = [
        {
            "id": p.get("id", ""),
            "error_type_id": p.get("error_type_id", ""),
            "description": p.get("description", ""),
            "params": json.dumps(p.get("params", {}), ensure_ascii=False),
        }
        for p in patterns
    ]

    edited_sp_rows = st.data_editor(
        sp_rows,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        key="kb_sp_editor",
        column_config={
            "id": st.column_config.TextColumn(
                "id",
                help="Pattern ID; changing it is treated as delete + new entry",
            ),
            "error_type_id": st.column_config.TextColumn(
                "error_type_id",
                help="ID of the error type this pattern solves",
            ),
            "description": st.column_config.TextColumn("description"),
            "params": st.column_config.TextColumn("params (JSON)"),
        },
    )

    if st.button("Save solution pattern changes", key="kb_save_patterns"):
        existing_ids = {p["id"] for p in patterns}
        edited_ids = {row["id"] for row in edited_sp_rows if row.get("id")}

        # Deleted patterns
        deleted_p_ids = existing_ids - edited_ids
        for p_id in deleted_p_ids:
            kb = delete_solution_pattern(kb, p_id)

        # New or updated patterns
        for row in edited_sp_rows:
            p_id = row.get("id")
            if not p_id:
                continue

            et_id = row.get("error_type_id", "")
            desc = row.get("description", "")
            params_raw = row.get("params", "") or "{}"
            try:
                params = json.loads(params_raw)
            except Exception as e:
                st.error(f"Invalid JSON in params for pattern {p_id}: {e}")
                break

            if p_id not in existing_ids:
                # Neues Pattern wird immer mit Kante error_type -> pattern angelegt
                kb = add_solution_pattern(kb, et_id, desc, params)
            else:
                # Bestehendes Pattern: auch error_type_id-Änderungen übernehmen
                kb = update_solution_pattern(
                    kb,
                    p_id,
                    error_type_id=et_id,
                    description=desc,
                    params=params,
                )

        save_kb(kb)
        st.session_state.kb = kb
        st.success("Solution patterns updated from table.")

    # --- Read‑only graph view ---
    graph = as_graph(kb)
    st.subheader("Edges (error ↔ solution patterns)")
    st.table(graph["edges"])


def main() -> None:
    apply_kit_theme()
    init_state()
    render_sidebar_logos()
    role_navigation()
    render_header()

    view = st.session_state.get("active_view", "System")

    # Reset scroll position when switching perspectives so that each
    # view consistently starts at the top.
    prev_view = st.session_state.get("prev_view", view)
    if prev_view != view:
        # Small JS snippet that scrolls to the top after rendering.
        components.html(
            "<script>window.parent.scrollTo(0, 0);</script>",
            height=0,
        )
    st.session_state.prev_view = view

    # Behaviour per role:
    # - System: pure simulation & automatic detection
    # - Human Sensor: handling/solving backlog, operates on the
    #   persistent anomalies list
    # - Knowledge Augmentator: knowledge repository & ontology
    if view == "Knowledge Augmentator":
        ui_knowledge_repo()
    elif view == "Human Sensor":
        ui_handling_and_solving("Handling and Solving")
    else:
        ui_detection()

    # After each rendered view check whether the conditions for
    # automatic persistence are met (simulation finished & no open
    # issues). This is intentionally lightweight and avoids the need for
    # a separate Evaluation view in the UI.
    maybe_persist_outputs()


if __name__ == "__main__":
    main()
