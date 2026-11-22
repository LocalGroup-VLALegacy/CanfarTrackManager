# CanfarTrackManager

Runs VLA track calibration and QA review on the CANFAR Science Platform. One controller per
project takes each execution block (EB) through calibration with the LGLBS pipeline, QA with
QAPlotter, and reviewer verdicts, submitting one headless session per job and keeping all
state in a file-based track database on `/arc`. Any VLA project or survey can use it through
its own project config.

It replaces AutoDataIngest (VM + Globus + SSH + Slurm on Fir) for work on CANFAR. Design and
decisions: `~/vla_processing/canfar-migration-plan.md`. The experiments that tested this design on
the real scheduler, and their results, are in `/arc/projects/VVINGS/vla-calib-mwe/docs/archive/walking-skeleton-record-2026-11.md`.

**Status: 0.1, early.** The core has run on the real scheduler; several stages in the plan are
not written yet.

| Part | State |
|---|---|
| Controller: lease and heartbeat, one `fetch()` per cycle, attempt file before `create()`, reconcile by ID or name, verify outputs, auto-retry, operator inbox, successor handoff | Working; real-scheduler checks E1–E7 |
| Per-stage worker limits (`controller.max_workers`), shared `max_sessions`, one `admit()` gate, queue held in the database with `blocked_by`, requests while running | Working; real-scheduler check Q1 (re-run 2026-11-22 on the M1 code) |
| Iterations and review: `iter_<n>/`, `pass` / `restart` / `manual_review` / `help_requested` | Working on the fake backend; first real RESTART run in progress |
| Calibrate worker (`workers/calibrate_worker.sh`): stage the SDM, fresh workdir, CASA with cgroup limits, verify, QA | Working; IC10 D test track |
| Sheets: template spec, Sheets API request builder, flag-command rebuild and checks | Offline only; no bridge to Google yet |
| Archive sync / `init` / preflight from NRAO TAP | Probe only (`scripts/tap_probe.py`) |
| Retention: trim `work/` after each verified run, drop old splits on restart, PROMOTING moves splits to `science_dir`, raw deleted on COMPLETE | Working on the fake backend; not yet on a real track |
| QA for reviewers: every iteration in `qa.products_dir/<EB>/iter_<n>/`, group read for `qa.group`, QA home and per-track index pages, ARC files links, QAPlotter nav fix | Working on the fake backend; ARC serving checked by hand; reviewer-account check pending (`docs/qa_access.md`) |
| Project job log (`jobs/jobs.csv`, `jobs.log`, `python -m trackmanager.jobs`) and track log (`state/tracks.csv`) | Working on the fake backend |
| `fetch_raw`, disk-quota gates | Not written (the `admit()` hook is in place) |
| `project.yaml` loader and the 10-stage state machine (`config.py`, `states.py`) | Working (M1); upstream stages (`DISCOVERED`, `AWAITING_DATA`, `STAGING`) are in the table but have no handlers yet |
| Dashboard | Text status only (`python -m trackmanager.status`) |

## Layout

```
trackmanager/        Python package
  config.py          project.yaml loader and validation (python -m trackmanager.config)
  states.py          the 10-stage state machine as data: stages, edges, human gates
  controller.py      the controller (python -m trackmanager.controller): lease, cycle, handoff
  scheduler.py       which queued tracks get a worker this cycle (pure)
  stages/            calibrate.py (submit, reconcile, verify), qa_publish.py, promote.py
  review.py inbox.py reviewer verdicts and operator actions from inbox/
  qa.py retention.py QA folders, links and index pages; what is deleted when
  store.py           atomic JSON / JSONL, run-root paths
  backend.py         CanfarBackend (canfar.sessions.Session) and FakeBackend (local subprocesses)
  worker.py          placeholder worker for tests and scheduler checks
  status.py          text dashboard     decide.py   operator/reviewer decisions -> inbox/
  request.py         add a track to the queue
  jobs.py            project job log and track log (python -m trackmanager.jobs)
  sheets.py          sheet template -> Sheets API requests, row values, xlsx preview
  flagcmds.py        rebuild and check flag commands from a flag tab's raw columns
  templates/project.yaml          annotated starter config
  templates/sheets/spec_v1.yaml
workers/calibrate_worker.sh   real calibrate stage (CASA pipeline + QA)
casa/casa_with_limits.sh      passes the session's cores and memory limit into CASA
qa/run_qa.sh, install_qadeps.sh   weblog + QAPlotter with CASA's python
qa/fix_nav.py        points QAPlotter's home links at the QA index pages
appsscript/ReviewNotifications.gs time-driven "QA ready" emails for the tracker
scripts/             run_controller.sh, submit_controller.sh, probe.sh, tap_probe.py
examples/            project.yaml examples (fake backend, placeholder workers, queue test, IC10 calibrate)
tests/               state table, config and scheduler tests; fake-backend fault-injection and stage tests; offline Sheets tests
docs/                submission scheme and attempt lifecycle diagrams
```

## Running

```bash
# tests (about 4 min; no CANFAR or Google access needed)
python -m pytest -q

# controller on the fake backend
python -m trackmanager.controller --project examples/project_fake.yaml --root /tmp/tm-fake --backend fake
python -m trackmanager.config examples/project_ic10.yaml --check   # validate, print the derived worker env

# controller as a headless CANFAR session; the run root is the track database in the project space
bash scripts/submit_controller.sh examples/project_ic10.yaml [/arc/projects/<space>/<run>]   # default: project.root
python -m trackmanager.status --root /arc/projects/<space>/<run>
python -m trackmanager.decide --root <run> --eb <EB> --action restart --flags flags.txt --refant-ignore ea26
python -m trackmanager.decide --root <run> --eb <EB> --action pass
python -m trackmanager.decide --root <run> --eb <EB> --action retry|skip|needs_manual   # from AWAITING_DECISION
python -m trackmanager.request --root <run> --eb <EB> --priority 1
python -m trackmanager.jobs --root <run> [--eb <EB>] [--status failed] [--csv]
```

The controller needs the `canfar` client (`/opt/conda/bin/python` in a CANFAR notebook session
has it; `scripts/run_controller.sh` installs a pinned copy in headless sessions).

## Stages

| Stage | Who acts | Progress inside it |
|---|---|---|
| `DISCOVERED` | controller | preflight (M2) |
| `AWAITING_DATA` ✋ | operator | `request_state`: `needs_request` → `requested` (M6) |
| `STAGING` | worker | `activity`: `queued` / `running` (M3) |
| `CALIBRATING` | worker | `activity`: `queued` / `running` / `publishing` |
| `AWAITING_REVIEW` ✋ | reviewer | `hold` for MANUAL REVIEW / HELP REQUESTED |
| `PROMOTING` | controller | |
| `DONE`, `NEEDS_MANUAL`, `SKIPPED` | | terminal |
| `AWAITING_DECISION` ✋ | operator | `reason`: `skip_candidate`, `preflight_failed`, `failed` (+ `failed_from`) |

`trackmanager/states.py` has every allowed edge, and `Controller.set_stage` refuses any other.
In M1 tracks from the config enter at `CALIBRATING/queued`. `retry` returns a track to the stage
it failed in. Queued tracks show `queue_position` and `blocked_by` in `state/<EB>.json`.

## Main settings (`project.yaml`; see `trackmanager/templates/project.yaml`)

| Block / key | Meaning |
|---|---|
| `controller.max_workers` | live worker sessions per stage (`calibrate`, `stage`); re-read every cycle; `0` holds the stage, queued tracks go once it is raised |
| `controller.max_sessions`, `min_free_tb` | shared session cap; shared disk gate (M3) |
| `controller.poll_s`, `grace_s`, `auto_retry`, `relaunch_after_d` | cycle time, how long a new attempt may be invisible, automatic retries, successor handoff (days) |
| `review.enabled` | `true`: a verified iteration waits in `AWAITING_REVIEW` for a verdict |
| `pipeline.casa_home`, `qa_deps`, `source_configs`, `repo` | CASA install (the config check fails if `bin/casa` is missing), QAPlotter deps, per-project cfgs (`VLA_PIPELINE_CONFIG_DIR`), ReductionPipeline checkout; they become the worker's environment |
| `pipeline.resources` | `default` and `by_config` (array config) cores and RAM |
| `pipeline.worker`, `pipeline.env` | what a worker session runs; extra environment for every track |
| `project.session_prefix` | session-name prefix (default `tm`); also marks this project's workers when counting slots |
| `retention` | `trim_work`, `drop_prev_iter_work` (both default `true`), `keep_raw_after_done` (`false`), `science_dir` (default `<root>/science`) |
| `qa` | `products_dir` (default `<root>/products`), `group` (read-only reviewer group, see `docs/qa_access.md`), `url_base` (ARC files service) |
| `tracks` | tracks seeded from the file until archive sync (M2) discovers them: `eb`, `priority`, `config`, `path` (SDM directory or tar) |

Relative paths are taken from `project.root`; `--root` overrides it.

## Related repositories

- **ReductionPipeline**: the CASA pipeline the calibrate worker runs. Per-project configs need its
  `VLA_PIPELINE_CONFIG_DIR` support (added 2026-11-21).
- **QAPlotter**: builds the QA pages; installed for CASA's python with `qa/install_qadeps.sh`.
- **AutoDataIngest**: the legacy Fir workflow. Not imported; a few pieces were ported (flag-sheet
  format, QA extraction steps).
