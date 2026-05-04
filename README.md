# Human-Supported Data Validation Component (Prototype)

> **Short summary**  
> This repository contains a research prototype for **human-supported data validation in a digital twin context**.  
> It represents the validation component of a DT and integrates human expertise (Human Sensor & Knowledge Augmentator) into the validation process.

---

## 1. Purpose and context

This application implements a **human-supported validation component** for production event logs. It takes an XES event log (`MainProcess.xes`) and replays it step by step as a simulated process execution.

### Data source and event log origin

The `MainProcess.xes` event log used in this prototype is derived from a dataset that was created by the IoT Lab of the Center for Informatics Research (CIRT) at the University of Trier.

- **Paper** Malburg, Lukas, Joscha Grüger, and Ralph Bergmann. 2022. “An IoT-Enriched Event Log for Process Mining in Smart Factories.” Version 1. Preprint, arXiv. https://doi.org/10.48550/ARXIV.2209.02702.
- **project page**: [An IoT-Enriched Event Log for Process Mining in Smart Factories](https://zenodo.org/records/7795547)

### Prototype Structure

The prototype focuses on the following (simplified) error types:

- **Inconsistent terminology**: different names for the same events (synonyms)
- **Imprecise terminology**: same names used for different events (semantic ambiguity)
- **Duplicated events**: events that are recorded twice

Automated detection and handling are intentionally imperfect so that human roles can step in, structure knowledge, and make it reusable for future cases.

**Roles / perspectives:**

- **System** – simulates process execution & automated anomaly detection (Detection)
- **Human Sensor** – validates and resolves concrete data issues (Handling & Solving)
- **Knowledge Augmentator** – maintains the knowledge base, error types, and solution patterns (Knowledge Management)

---

## 2. Project structure

Key files and directories:

```text
Prototype/
├─ app.py                 # Streamlit frontend & role views
├─ backend.py             # Simulated detection & anomaly generation
├─ data_layer.py          # XES/JSON I/O, versioning of logs
├─ knowledge_base.py      # Ontology-like knowledge base (error types & patterns)
├─ requirements.txt       # Python dependencies (Streamlit)
├─ event_logs/
│  ├─ MainProcess.xes             # Original event log
│  ├─ errornous_MainProcess.xes   # Log with injected anomalies
│  └─ corrected_MainProcess.xes   # System/human-corrected log
├─ json_files/
│  ├─ ambiguous_events.json   # System ruels to manulate ambigous data errors
│  ├─ knowledge_base.json   # Knowledge base (error types & solution patterns)
│  ├─ synonym_events.json   # System ruels to manipulate synonym data errors
│  ├─ anomalies_queue.json  # Backlog of detected anomalies
│  ├─ handled_issues.json   # Issues resolved by Human Sensors incl. timestamps
│  └─ simulation_state.json # Progress of event simulation
├─ validated_logs/
│  ├─ validated_events.json       # validated events as JSON
│  ├─ MainProcess_validated.xes   # validated XES log
│  └─ decisions_log.json          # log of all system/human decisions
└─ design/
   ├─ Logo_KIT.svg.png
   └─ logo_sydsen_hor1 copy 4 - Copy.png
```
---

## 3. Installation & running the app

You can run the application:

**Locally via Python & Streamlit** (recommended for development / modification)

### 3.1. Prerequisites

- **Local execution**
  - Python ≥ 3.10 (tested with 3.11)
  - `pip` (Python package manager)

### 3.2. Local installation (without Docker)

1. Clone or copy this repository to a local directory.
2. Open a terminal / PowerShell in that directory and (optionally) create a virtual environment.
3. Install dependencies:

```bash
pip install -r requirements.txt
```

### 3.3. Run the application locally

From the project directory:

```bash
streamlit run app.py
```

Then open in your browser:

- http://localhost:8501

---
## 4. Usage: roles & workflow

After startup, the **sidebar** offers a role selector:

- `System`
- `Human Sensor`
- `Knowledge Augmentator`

### 4.1. System view (Detection / process simulation)

**Purpose:** Observe the automated process simulation and system-side detection of data quality issues.

Behaviour:

- On startup, the backend loads the original event log (`MainProcess.xes`) and generates a **simulated execution** with realistic time differences.
- A background thread replays the events **step by step**.
- The view shows:
  - a progress bar over all events
  - the currently replayed event with all XES attributes
  - a summary of whether anomalies were detected for this event (including confidence and the `detected_at` timestamp)
- The view refreshes approximately **once per second**.

<img width="1920" height="1080" alt="Screenshot - System" src="https://github.com/user-attachments/assets/712fcef1-e530-4af2-a7ba-c6f9c882822f" />


### 4.2. Human Sensor view (Handling & Solving)

**Purpose:** Resolve all anomalies detected by the system, including confirming / adjusting error types and selecting or defining solution patterns.

Main elements:

1. **Backlog table** (anomalies queue)
   - Shows all open issues from `json_files/anomalies_queue.json`.
   - For each entry, among other fields:
     - queue position (FIFO)
     - event number & activity
     - suggested error type
     - detection timestamp

2. **Current issue (top of the queue)**
   - The corresponding event is shown with all attributes.
   - For **duplicate errors (ET_DUPLICATED_EVENT)** the original and duplicate event are displayed in a **pair view** side by side for direct comparison.

3. **Error type selection**
   - A dropdown with known error types (sorted alphabetically by description).
   - Option `Create new error type` to define a new type for previously unseen issues.

4. **Solution pattern selection**
   - For the confirmed error type, applicable patterns are listed (alphabetically by description).
   - Option `<Create new solution pattern>` to define new solution strategies.
   - Supported actions (in the current version):
     - `rename_activity` – systematically rename activity labels
     - `mark_duplicate` – mark duplicates as events that should be removed

5. **Handling durations per issue**
   - Each handled issue is recorded in `json_files/handled_issues.json` with:
     - `event_index`
     - `error_type_id`
     - `start_ts` (ISO timestamp)
     - `finish_ts` (ISO timestamp)
   - Semantics in this prototype:
     - First issue after an empty backlog: `start_ts = detected_at` of that issue.
     - Subsequent issues: `start_ts` = `finish_ts` of the previous issue (continuous handling block).

When a solution is applied:

- the event in memory is updated,
- the issue is marked as handled (persisted in `handled_issues.json`),
- a **decision record** (including confidence before/after, free-text explanation, role) is appended to `validated_logs/decisions_log.json`.

<img width="1920" height="1080" alt="Screenshot - Human Sensor" src="https://github.com/user-attachments/assets/2a9d2e23-9add-416d-b168-27e0f50942cf" />


### 4.3. Knowledge Augmentator view (Knowledge management)

**Purpose:** Structure and maintain explicit knowledge about error types and solution patterns for later automation.

The view offers:

1. **Error types (inline editable)**
   - Tabular representation of all error types (`knowledge_base.json`).
   - Columns: `id`, `description`.
   - Directly editable in the table; new rows can be added.

2. **Solution patterns (inline editable)**
   - Tabular representation of all solution patterns.
   - Columns: `id`, `error_type_id`, `description`, `params (JSON)`.
   - The `params` field is edited as JSON; syntax errors are reported when saving.

3. **Graph view (edges)**
   - Read-only table of edges between error types and solution patterns.
   - Helps to quickly see which patterns belong to which error types.

<img width="1920" height="1080" alt="Screenshot - Knowledge Augmentator" src="https://github.com/user-attachments/assets/f4a0d87a-e23e-43b7-b6ad-b47c1c24f792" />


---

## 5. Typical usage scenarios

One possible flow for a demo or study:

1. **Open the System view**
   - Let the simulation run and observe progress.
   - See when and where anomalies are detected.

2. **Switch to the Human Sensor view (in parallel)**
   - Inspect the anomaly backlog.
   - For each issue: confirm/change the error type, optionally create a new pattern.
   - Apply the solution to gradually improve data quality.

3. **Use the Knowledge Augmentator view**
   - Consolidate newly created error types and patterns.
   - Refine descriptions and parameters to increase reusability.

4. **Analyse artifacts**
   - Use `validated_logs/` and `json_files/handled_issues.json` in notebooks or external tools to analyse handling durations, distribution of error types, etc.

>**Caution:**
>Do not refresh the app in your browser since this restarts also the event replay, leading to an errornous event backlog and event log. Restart the whole application istead for restarting the demo.

---

## 6. Customization & extension

- **New error types** can be added directly from the Knowledge view or the Human Sensor view.
- **Additional solution patterns** (e.g. more complex attribute correction rules) can be implemented in `knowledge_base.py` and `backend.py`.
- **Changing the data source**: the underlying XES log can be replaced at `event_logs/MainProcess.xes`. The parser in `data_layer.load_raw_events` is designed to work with typical XES attributes.

---

## 7. Citations

The prototype is related to the paper "Human-Supported Data Validation in Digital Twins: An Illustrative Case Study" by Johannes Deufel and Sanja Lazarova-Molnar. Please cite it as following if you are referring or extending it in your own work: 
> [***will be added soon***]
