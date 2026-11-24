# Google Sheets setup for a project (M4)

Each project has a **tracker** spreadsheet (`Track Summary`, `QA Sign-up`), a **template workbook** (`README` and the project's flag `TEMPLATE`), and a Drive **flags folder** in which the controller creates **one flag spreadsheet per track**. The controller writes them through a Google Cloud **service account**. Reviewers sign up in the tracker, and give flags and verdicts in each track's own sheet; a "QA ready" email (Apps Script) tells them when. See plan §3.5 for the layout.

Do the Google-side steps once per Google account. Steps 5–9 are repeated for each project.

## Accounts
| Identity | Role | In `sheets.editors`? |
|---|---|---|
| Project account (a Google account for the project) | Owns the Cloud project and the service account. Ideally it also owns the spreadsheets and the notification script, so the emails come from it | yes |
| Operator(s) | Editors of both spreadsheets; can edit protected ranges | yes |
| Service account (`…@<project>.iam.gserviceaccount.com`) | The controller | added automatically |
| Reviewers | Editors of both spreadsheets; protection limits them to the sign-up cells and the flag rows/verdict cells | **no** |

The owner of a spreadsheet can always edit its protected ranges, as can the editors in `sheets.editors`. An account used to test reviewer access must therefore be neither.

## Google Cloud (once)
1. Sign in as the project account at console.cloud.google.com. Use a consumer account, or a Workspace account whose organisation does not enforce `iam.disableServiceAccountKeyCreation`.
2. Create a project. Billing is not needed.
3. **APIs & Services → Library: enable the Google Sheets API and the Google Drive API.** Drive is used only with the `drive.file` scope: the service account creates the track spreadsheets in the flags folder and can see only those files and the folder.
4. **IAM & Admin → Service Accounts → Create.** Grant it **no roles**: its access comes only from what is shared with it. It owns the track spreadsheets it creates (they use its own 15 GB Drive quota), so **don't delete the service account or its Cloud project** while a project's sheets are in use: its files go with it. The durable record stays on `/arc` (every iteration's frozen `inputs/` and `tracks/<EB>/flag_tab.csv`).
5. **Keys → Add key → JSON.** Copy the file to CANFAR, then lock it down:
   ```
   chmod 700 /arc/projects/<space>/secrets
   chmod 600 /arc/projects/<space>/secrets/<key>.json
   ```
   - The controller and `init` refuse a key that group or others can read.
   - Delete the downloaded copy from your laptop.
   - Keys don't expire. If one leaks, delete it in the console and make a new one.
   - One service account can serve every project.

## Spreadsheets (per project)
6. Create two **empty** spreadsheets (e.g. `<project> Tracks`, `<project> flag template`) and one **folder** (e.g. `<project> flags`). Share all three with the service-account email as **Editor** ("Notify" unticked).
7. Fill in `project.yaml`:
   ```yaml
   sheets:
     enabled: true
     tracker_id: <id between /d/ and /edit>
     flagbook_id: <id>            # the template workbook
     flags_folder_id: <id after /folders/ in the folder's URL>
     credentials: /arc/projects/<space>/secrets/<key>.json
     editors: [<project account>, <operators>]
     links: {qa_guide: <url>, flag_rows_help: <url>}
   ```
8. Build the tabs, with the controller stopped:
   ```
   python -m trackmanager.init --project project.yaml --sheets
   ```
   - It sets both spreadsheets to UTC, creates the managed tabs from `trackmanager/templates/sheets/spec_v1.yaml` and removes the empty `Sheet1`. It checks that the flags folder is writable.
   - It protects every tab, with the service account plus `sheets.editors` as the only editors.
   - It reads the protections back and reports any difference from the spec.
   - It is safe to rerun: existing tabs are only checked, never rebuilt.
   - The report is saved in `<root>/reports/sheets-<ts>.json`.
9. **Notification email.** As the account that owns the tracker: Extensions → Apps Script, paste `appsscript/ReviewNotifications.gs`, save, run `installTrigger()` once, and grant the permissions.
   - It emails a signed-up reviewer once per iteration when the track reaches `AWAITING_REVIEW`.
   - It is time-driven (every 10 minutes), because edits made through the API don't fire `onEdit`.
   - The quota is 100 emails a day for a consumer account and 1,500 for Workspace.
10. Share the tracker and the flags folder with the reviewers as **Editor**. Every track spreadsheet in the folder inherits the folder's sharing. The template workbook needs only the operators.

## The project's flag template
`TEMPLATE` in the template workbook holds the flags most tracks of the project are likely to need, such as known RFI in a given SPW. Each track's spreadsheet starts as a copy of it. Ticked rows are applied from each track's first run; unticked rows are suggestions a reviewer can tick.
- **It is hidden and locked to the operators** (`sheets.editors`), so reviewers never see it. To edit it, unhide it (View → Hidden sheets), edit rows 7 onwards, then hide it again.
- **Changes apply only to tracks whose sheet is made afterwards.** Existing track sheets keep the rows they were copied with.
- **Don't set a verdict (M1) or avoid-refant (O1) in `TEMPLATE`.** They are never copied, and the protection check raises an alarm if they hold anything. `init --sheets` clears them.
- **SPW numbers only mean something within one correlator setup.** A single template suits a project with one setup; templates per setup or per array configuration can be added later.

## Checks
- Headless reachability (probe P2):
  ```
  canfar create headless images.canfar.net/skaha/base-notebook:latest --name tm-sheets-probe -- \
      bash <repo>/scripts/probe_sheets.sh <project.yaml>
  ```
  It writes `<root>/probes/sheets-<session>.json`.
- Keep the spreadsheet locale as English (`en_CA`/`en_US`). In locales that use a decimal comma, a scan list typed as `3,4` would be read as a number.

## What the controller does with the sheets
With `sheets.enabled`, each controller cycle runs the Sheets bridge (`trackmanager/sheets_bridge.py`):
- **Rows.** Every track has one row in each tracker tab; rows are appended and never moved. Only changed cells are written, as one write per spreadsheet per cycle.
- **Track flag sheets.** When a track is first queued for calibration, the controller creates `<project> flags <track>` in the flags folder: its flag tab (copied from `TEMPLATE`) and the `README`. Reviewers can add rows while it runs. The tracker's "Flags" columns link to it.
  - The copy keeps the template's flag rows. Its verdict, avoid-refant, `Added in iter` and `Check` cells always start empty.
  - **Nothing is hidden or deleted.** At DONE (or SKIPPED / NEEDS_MANUAL) the sheet is also saved to `tracks/<EB>/flag_tab.csv`, and the sheet stays in the folder, readable and editable. The controller reads a sheet only while its track is calibrating or in review.
- **A run that no verdict set up uses the sheet's ticked rows.** That means iteration 1, and the run after a `reopen`. Before it is submitted, the ticked rows of the track's sheet and its avoid-refant cell are frozen into `iter_<n>/inputs/`. The first submission waits one cycle for the new sheet to be created and read.
  - A ticked row that fails the checks for that track is left out, rather than blocking the run (a project-wide default may not apply to every track). It is explained in the row's `Check` cell and in M2.
  - Rows that were used get `Added in iter` = the iteration that first used them.
- **Sign-ups.** The first time a name appears in `QA Reviewer` the bridge stamps `Signed up (UTC)` in Track Summary. A sign-up without a valid email shows as a "needs you" item.
- **Verdicts (M1).** Read only while the track is `AWAITING_REVIEW`; a verdict set earlier is cleared, with a note in M2.
  - **RESTART** reruns the pipeline with every ticked row, including rows from earlier iterations, plus the avoid-refant list in O1. Every row is checked first; problems go to the row's `Check` cell and M2, and the verdict stays in M1 until the rows are fixed.
  - **COMPLETE** promotes the track.
  - **MANUAL REVIEW** and **HELP REQUESTED** put the track on HOLD for the operator, with no rerun, until a later COMPLETE or RESTART.
  - Once the controller has applied a verdict, M2 says so and M1 is cleared, unless the reviewer has changed it in the meantime.
- **Bridge state** is in `<root>/sheets/` (`bridge.json`, `mirror.json`, `status.json`). `python -m trackmanager.status --root <root>` shows the sheets line, any alarm, and the "needs you" items.
- **Protections** are read back every `sheets.protection_check_cycles` cycles. Any drift raises an alarm but changes nothing.
- **If Google is unreachable,** no track state changes. The alarm is raised after 3 failed steps in a row.

## A track completed by mistake: `reopen`
COMPLETE takes effect at once, and the raw data is deleted at DONE. The track's flag sheet stays, editable, so its flags are never lost.
1. Correct the sheet if needed: tick or untick rows, fix the avoid-refant cell.
2. As the operator, run:
   ```
   python -m trackmanager.decide --root <run> --eb <EB> --action reopen --notes "why"
   ```
   Then start the controller again if it has exited; a sheets project's controller exits once every track is finished.
3. The track moves to the next iteration:
   - Its promoted splits are renamed to `<science_dir>/<EB>.superseded-<ts>/`, and the approved-QA marker is set aside the same way. Nothing is deleted.
   - It is staged again from its `/arc` input (`DONE → STAGING`), or recalibrated if the raw data was kept (`DONE → CALIBRATING`). A project that downloads from NRAO has to request the data again (`DONE → AWAITING_DATA`, with M6).
   - The run uses the sheet's ticked rows: the flags of the last restart plus any edits.
   - The completion record moves into `state/<EB>.json` `reopened` (a list), and the reviewer gets a new "QA ready" email when it is ready.

## Notes
- Nothing reviewers type into the sheets is executed. The controller rebuilds every flag command from the raw cells and checks it against the track's metadata (`trackmanager/flagcmds.py`).
- NRAO download links never go into the sheets, which are shared with reviewers. They come in only through the dashboard (plan §3.2).
