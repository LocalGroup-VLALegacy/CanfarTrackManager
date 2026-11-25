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
| Staging (`stages/stage_local.py`, `stage_worker.py`): symlink an SDM directory or unpack an NRAO tar into `raw/`, verify it (BDF count, name, TAP size, `Scan.xml` intents against the cfgs), write `stage/manifest.json` | Working (M3); fake backend and real-scheduler checks on the IC10 SDM |
| Calibrate worker (`workers/calibrate_worker.sh`): fresh workdir, CASA with cgroup limits, verify, QA; it reads the staged SDM and no longer stages | Working; IC10 D test track |
| Sheets (M4): tracker and template workbook built from `spec_v1.yaml` (`init --sheets`); the bridge mirrors every track each cycle, creates one flag spreadsheet per track in the flags folder when it starts calibrating (copied from the project `TEMPLATE`, whose ticked rows apply from iteration 1; saved to CSV at DONE and never hidden or deleted), stamps sign-ups, turns verdicts into decisions (RESTART uses every ticked row, rebuilt and checked by `flagcmds.py`), checks protections; `appsscript/ReviewNotifications.gs` emails reviewers; operator `reopen` reruns a DONE track | Complete 2026-11-25: run live on test spreadsheets (`docs/sheets_setup.md`); reviewer-refusal check and QA Guide deferred to M7 |
| Archive sync from NRAO TAP, preflight against the project cfgs, `init` setup report, `include` / `recheck` actions | Working (M2) on recorded 24B-224 fixtures and live TAP; archive tracks stop at `AWAITING_DATA` (M6), or go to `STAGING` when `inputs.source` is `arc_path` |
| Retention: trim `work/` after each verified run, drop old splits on restart, PROMOTING moves splits to `science_dir` and deletes `raw/` (verified; a `raw/` that will not delete keeps the track in PROMOTING), `provenance.json` cites the raw manifest, image, CASA, pipeline and ReductionPipeline versions | Working on the fake backend; the real-track run is the M3 acceptance run |
| QA for reviewers: every iteration in `qa.products_dir/<EB>/iter_<n>/`, group read for `qa.group`, QA home and per-track index pages, ARC files links, QAPlotter nav fix | Working on the fake backend; ARC serving checked by hand; reviewer-account check pending (`docs/qa_access.md`) |
| Project job log (`jobs/jobs.csv`, `jobs.log`, `python -m trackmanager.jobs`) and track log (`state/tracks.csv`) | Working on the fake backend |
| Disk gate (`disk.py`): `controller.quota_tb` minus a per-phase model of every track's footprint, `min_free_tb` kept free | Working (M3); real-scheduler check on the IC10 SDM |
| `fetch_raw` (NRAO download) | Not written (M6) |
| `project.yaml` loader and the 10-stage state machine (`config.py`, `states.py`) | Working (M1); `DISCOVERED` has its handler (M2); `STAGING` has its handler (M3); `AWAITING_DATA` is in the table but has none yet (M6) |
| Dashboard | Text status only (`python -m trackmanager.status`) |

## Layout

```
trackmanager/        Python package
  config.py          project.yaml loader and validation (python -m trackmanager.config)
  states.py          the 10-stage state machine as data: stages, edges, human gates
  controller.py      the controller (python -m trackmanager.controller): lease, cycle, handoff
  scheduler.py       which queued tracks get a worker this cycle (pure)
  archive.py         NRAO TAP query, one record per EB, selection, sync (stdlib only)
  preflight.py cfgs.py   checks on an EB before download; the pipeline cfgs via its own readers
  init.py            setup report (python -m trackmanager.init)
  stages/            discover.py (sync, preflight), calibrate.py (submit, reconcile, verify), qa_publish.py, promote.py
  review.py inbox.py reviewer verdicts and operator actions from inbox/
  qa.py retention.py QA folders, links and index pages; what is deleted when
  store.py           atomic JSON / JSONL, run-root paths
  backend.py         CanfarBackend (canfar.sessions.Session) and FakeBackend (local subprocesses)
  worker.py          placeholder worker for tests and scheduler checks
  status.py          text dashboard     decide.py   operator/reviewer decisions -> inbox/
  request.py         add a track to the queue
  jobs.py            project job log and track log (python -m trackmanager.jobs)
  sheets.py          sheet template -> Sheets API requests, row values, xlsx preview
  sheets_setup.py    build the managed tabs of both spreadsheets; protection drift check
  sheets_bridge.py   per-cycle Sheets bridge: rows, per-track flag sheets, sign-ups, verdicts -> inbox/
  sheet_context.py   one track's values for the sheets (and the metadata flag rows are checked against)
  gsheets.py         Sheets API v4 client on google-auth (no gspread); fake_sheets.py for tests
  flagcmds.py        rebuild and check flag commands from a flag tab's raw columns
  templates/project.yaml          annotated starter config
  templates/sheets/spec_v1.yaml
workers/calibrate_worker.sh   real calibrate stage (CASA pipeline + QA)
casa/casa_with_limits.sh      passes the session's cores and memory limit into CASA
qa/run_qa.sh, install_qadeps.sh   weblog + QAPlotter with CASA's python
qa/fix_nav.py        points QAPlotter's home links at the QA index pages
appsscript/ReviewNotifications.gs time-driven "QA ready" emails for the tracker
scripts/             run_controller.sh, submit_controller.sh, pydeps.sh, probe.sh, probe_sheets.sh, tap_probe.py, record_tap_fixture.py,
                     extract_sheet_fixture.py, check_archive_vs_sheet.py (live 20A-346 check)
examples/            project.yaml examples (fake backend, placeholder workers, queue test, IC10 calibrate, archive sync)
tests/               state table, config, scheduler, archive, preflight, init tests; fake-backend fault-injection, stage and discovery tests; offline Sheets tests
tests/fixtures/      recorded TAP result and old-sheet rows for 24B-224, a synthetic project, small pipeline cfgs
docs/                submission scheme and attempt lifecycle diagrams; qa_access.md; sheets_setup.md (Google setup)
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
python -m trackmanager.decide --root <run> --eb <EB> --action include   # a skip candidate (short or tiny EB) goes on
python -m trackmanager.decide --root <run> --eb <EB> --action recheck   # rerun preflight after fixing a cfg
python -m trackmanager.init --project <project.yaml>   # archive sync + preflight report; rerun until clean
python -m trackmanager.init --project <project.yaml> --sheets   # also build/check the two spreadsheets (controller stopped)
python -m trackmanager.request --root <run> --eb <EB> --priority 1 [--path <SDM dir or tar> --size-gb N]
python -m trackmanager.jobs --root <run> [--eb <EB>] [--status failed] [--csv]
```

The controller needs the `canfar` client (`/opt/conda/bin/python` in a CANFAR notebook session
has it; `scripts/run_controller.sh` installs a pinned copy in headless sessions).

## Stages

| Stage | Who acts | Progress inside it |
|---|---|---|
| `DISCOVERED` | controller | archive sync creates it; preflight runs here and decides the next stage |
| `AWAITING_DATA` ✋ | operator | `request_state`: `needs_request` → `requested` (M6) |
| `STAGING` | worker | `activity`: `queued` / `running`; `stage_attempt`; `stage/manifest.json` |
| `CALIBRATING` | worker | `activity`: `queued` / `running` / `publishing` |
| `AWAITING_REVIEW` ✋ | reviewer | `hold` for MANUAL REVIEW / HELP REQUESTED |
| `PROMOTING` | controller | |
| `DONE`, `NEEDS_MANUAL`, `SKIPPED` | | terminal |
| `AWAITING_DECISION` ✋ | operator | `reason`: `skip_candidate`, `preflight_failed`, `failed` (+ `failed_from`) |

`trackmanager/states.py` has every allowed edge, and `Controller.set_stage` refuses any other.
Tracks from the config's `tracks:` and operator requests enter at `STAGING/queued` if they name an input (`path`, or `request --path`), else at `CALIBRATING/queued`; tracks found by the archive sync enter at `DISCOVERED`, and preflight sends them to `AWAITING_DATA` (`nrao_link`), `STAGING/queued` (`arc_path`; the input is `inputs.arc_dir/<SDM>[.tar]`) or `AWAITING_DECISION` (`skip_candidate`: `include` or `skip`; `preflight_failed`: fix the cfgs, then `recheck`). EBs outside `archive.bands_ghz` or in `exclude_ebs` never become tracks. `retry` returns a track to the stage
it failed in. Queued tracks show `queue_position` and `blocked_by` in `state/<EB>.json`.

## Main settings (`project.yaml`; see `trackmanager/templates/project.yaml`)

| Block / key | Meaning |
|---|---|
| `controller.max_workers` | live worker sessions per stage (`calibrate`, `stage`); re-read every cycle; `0` holds the stage, queued tracks go once it is raised |
| `controller.max_sessions` | shared cap on live worker sessions across stages |
| `controller.quota_tb`, `min_free_tb`, `pipeline.disk_factor` | the disk gate: the space's allocation (unset = no gate), the part of it kept free, and calibration's footprint as a multiple of the SDM (re-read every cycle) |
| `inputs.arc_dir`, `pipeline.stage_worker` | where `stage_local` finds `<SDM>` or `<SDM>.tar` for archive tracks; its command, image and `size_tolerance` |
| `controller.poll_s`, `grace_s`, `auto_retry`, `relaunch_after_d` | cycle time, how long a new attempt may be invisible, automatic retries, successor handoff (days) |
| `review.enabled` | `true`: a verified iteration waits in `AWAITING_REVIEW` for a verdict |
| `pipeline.casa_home`, `qa_deps`, `source_configs`, `repo` | CASA install (the config check fails if `bin/casa` is missing), QAPlotter deps, per-project cfgs (`VLA_PIPELINE_CONFIG_DIR`), ReductionPipeline checkout; they become the worker's environment |
| `pipeline.resources` | `default` and `by_config` (array config) cores and RAM |
| `pipeline.worker`, `pipeline.env` | what a worker session runs; extra environment for every track |
| `project.session_prefix` | session-name prefix (default `tm`); also marks this project's workers when counting slots |
| `retention` | `trim_work`, `drop_prev_iter_work` (both default `true`), `keep_raw_after_done` (`false`), `science_dir` (default `<root>/science`) |
| `qa` | `products_dir` (default `<root>/products`), `group` (read-only reviewer group, see `docs/qa_access.md`), `url_base` (ARC files service) |
| `project.vla_project_codes`, `inputs` | VLA project codes to sync; `source` (`nrao_link` or `arc_path`) and `eb_list` (`archive`, or `file:<csv>` as an allow-list) |
| `archive` | `tap_url` (`https://…/tap`, or `file://<csv[.gz]>` for fixtures), `poll_h`, `bands_ghz`, `include_ebs` / `exclude_ebs` (SDM name or EB id), `min_duration_min`, `size_calibration`; `cones` is accepted but not used yet |
| `sheets` | `enabled`, `tracker_id`, `flagbook_id`, `credentials` (service-account key, mode 600), `editors` (humans who may edit protected ranges), `links.qa_guide` / `flag_rows_help`, `drop_done_tabs_after_d`, `protection_check_cycles`; setup in `docs/sheets_setup.md`. With sheets on, the controller runs until every track is terminal |
| `tracks` | tracks seeded from the file (tests, the IC10 run): `eb`, `priority`, `config`, `path` (SDM directory or tar) |

Relative paths are taken from `project.root`; `--root` overrides it.

## Related repositories

- **ReductionPipeline**: the CASA pipeline the calibrate worker runs. Per-project configs need its
  `VLA_PIPELINE_CONFIG_DIR` support (added 2026-11-21).
- **QAPlotter**: builds the QA pages; installed for CASA's python with `qa/install_qadeps.sh`.
- **AutoDataIngest**: the legacy Fir workflow. Not imported; a few pieces were ported (flag-sheet
  format, QA extraction steps).
