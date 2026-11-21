# CanfarTrackManager

Runs VLA track calibration and QA review on the CANFAR Science Platform. One controller per
project takes each execution block (EB) through calibration with the LGLBS pipeline, QA with
QAPlotter, and reviewer verdicts, submitting one headless session per job and keeping all
state in a file-based track database on `/arc`. Any VLA project or survey can use it through
its own project config.

It replaces AutoDataIngest (VM + Globus + SSH + Slurm on Fir) for work on CANFAR. Design and
decisions: `~/vla_processing/canfar-migration-plan.md`. The experiments that tested this design on
the real scheduler, and their results, are in `/arc/projects/VVINGS/vla-calib-mwe/README.md`.

**Status: 0.1, early.** The core has run on the real scheduler; several stages in the plan are
not written yet.

| Part | State |
|---|---|
| Controller: lease and heartbeat, one `fetch()` per cycle, attempt file before `create()`, reconcile by ID or name, verify outputs, auto-retry, operator inbox, successor handoff | Working; real-scheduler checks E1–E7 |
| Worker limit (`max_workers`) and queue held in the database, requests while running | Working; real-scheduler check Q1 |
| Iterations and review: `iter_<n>/`, `pass` / `restart` / `manual_review` / `help_requested` | Working on the fake backend; first real RESTART run in progress |
| Calibrate worker (`workers/calibrate_worker.sh`): stage the SDM, fresh workdir, CASA with cgroup limits, verify, QA | Working; IC10 D test track |
| Sheets: template spec, Sheets API request builder, flag-command rebuild and checks | Offline only; no bridge to Google yet |
| Archive sync / `init` / preflight from NRAO TAP | Probe only (`scripts/tap_probe.py`) |
| `fetch_raw`, `promote_science`, retention, disk-quota gates | Not written |
| `project.yaml` loader | Not written; configs are JSON with worker env (`examples/`) |
| Dashboard | Text status only (`python -m trackmanager.status`) |

## Layout

```
trackmanager/        Python package
  controller.py      the controller (python -m trackmanager.controller)
  store.py           atomic JSON / JSONL, run-root paths
  backend.py         CanfarBackend (canfar.sessions.Session) and FakeBackend (local subprocesses)
  worker.py          placeholder worker for tests and scheduler checks
  status.py          text dashboard     decide.py   operator/reviewer decisions -> inbox/
  request.py         add a track to the queue
  sheets.py          sheet template -> Sheets API requests, row values, xlsx preview
  flagcmds.py        rebuild and check flag commands from a flag tab's raw columns
  templates/sheets/spec_v1.yaml
workers/calibrate_worker.sh   real calibrate stage (CASA pipeline + QA)
casa/casa_with_limits.sh      passes the session's cores and memory limit into CASA
qa/run_qa.sh, install_qadeps.sh   weblog + QAPlotter with CASA's python
appsscript/ReviewNotifications.gs time-driven "QA ready" emails for the tracker
scripts/             run_controller.sh, submit_controller.sh, probe.sh, tap_probe.py
examples/            example configs (fake backend, placeholder workers, queue test, IC10 calibrate)
tests/               fake-backend fault-injection tests and offline Sheets tests
docs/                submission scheme and attempt lifecycle diagrams
```

## Running

```bash
# tests (about 4 min; no CANFAR or Google access needed)
python -m pytest -q

# controller on the fake backend
python -m trackmanager.controller --config examples/project_fake.json --root /tmp/tm-fake --backend fake

# controller as a headless CANFAR session; the run root is the track database in the project space
bash scripts/submit_controller.sh examples/project_ic10.json /arc/projects/<space>/<run>
python -m trackmanager.status --root /arc/projects/<space>/<run>
python -m trackmanager.decide --root <run> --eb <EB> --action restart --flags flags.txt --refant-ignore ea26
python -m trackmanager.decide --root <run> --eb <EB> --action pass
python -m trackmanager.request --root <run> --eb <EB> --priority 1
```

The controller needs the `canfar` client (`/opt/conda/bin/python` in a CANFAR notebook session
has it; `scripts/run_controller.sh` installs a pinned copy in headless sessions).

## Main settings (config JSON, top level)

| Key | Meaning |
|---|---|
| `max_workers` | live worker sessions at once (pending or running); re-read every cycle, `0` pauses |
| `review` | `true`: a verified iteration waits in `AWAITING_REVIEW` for a verdict |
| `casa_home` | CASA install the workers use; the controller refuses to start if `bin/casa` is missing |
| `session_prefix` | session-name prefix (default `tm`); also marks this project's workers when counting slots |
| `auto_retry`, `poll_s`, `grace_s`, `relaunch_after_s`, `retention.drop_prev_iter_work` | see `trackmanager/controller.py` |
| `worker.env` | `PIPELINE`, `TAR`, `CASA_WRAPPER`, `QA_DEPS`, `QA_SCRIPT`, optional `VLA_PIPELINE_CONFIG_DIR`, `ANTCORR_DIR` |

## Related repositories

- **ReductionPipeline**: the CASA pipeline the calibrate worker runs. Per-project configs need its
  `VLA_PIPELINE_CONFIG_DIR` support (added 2026-11-21).
- **QAPlotter**: builds the QA pages; installed for CASA's python with `qa/install_qadeps.sh`.
- **AutoDataIngest**: the legacy Fir workflow. Not imported; a few pieces were ported (flag-sheet
  format, QA extraction steps).
