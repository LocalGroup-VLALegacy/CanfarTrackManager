# Google Sheets setup for a project (M4)

Each project has two spreadsheets: a **tracker** (`Track Summary`, `QA Sign-up`) and a **flag book** (`README`, `TEMPLATE`, then one tab per track while it is in work). The controller writes them through a Google Cloud **service account**. Reviewers sign up in the tracker and give flags and verdicts in the flag book. See plan §3.5 for the layout.

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
3. **APIs & Services → Library → Google Sheets API → Enable.** The Drive API is not needed: sheets are opened by id and shared by hand.
4. **IAM & Admin → Service Accounts → Create.** Grant it **no roles**: its access comes only from the spreadsheets shared with it.
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
6. Create two **empty** spreadsheets (e.g. `<project> Tracks`, `<project> Flags`). Share each as **Editor** with the service-account email ("Notify" unticked) and with the operators.
7. Fill in `project.yaml`:
   ```yaml
   sheets:
     enabled: true
     tracker_id: <id between /d/ and /edit>
     flagbook_id: <id>
     credentials: /arc/projects/<space>/secrets/<key>.json
     editors: [<project account>, <operators>]
     links: {qa_guide: <url>, flag_rows_help: <url>}
   ```
8. Build the tabs, with the controller stopped:
   ```
   python -m trackmanager.init --project project.yaml --sheets
   ```
   - It sets both spreadsheets to UTC, creates the managed tabs from `trackmanager/templates/sheets/spec_v1.yaml` and removes the empty `Sheet1`.
   - It protects every tab, with the service account plus `sheets.editors` as the only editors.
   - It reads the protections back and reports any difference from the spec.
   - It is safe to rerun: existing tabs are only checked, never rebuilt.
   - The report is saved in `<root>/reports/sheets-<ts>.json`.
9. **Notification email.** As the account that owns the tracker: Extensions → Apps Script, paste `appsscript/ReviewNotifications.gs`, save, run `installTrigger()` once, and grant the permissions.
   - It emails a signed-up reviewer once per iteration when the track reaches `AWAITING_REVIEW`.
   - It is time-driven (every 10 minutes), because edits made through the API don't fire `onEdit`.
   - The quota is 100 emails a day for a consumer account and 1,500 for Workspace.
10. Share both spreadsheets with the reviewers as **Editor**.

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
- **Flag tabs.** A track's tab is copied from `TEMPLATE` when it first starts calibrating, so reviewers can add rows while it runs. At DONE (or SKIPPED / NEEDS_MANUAL) the tab is saved to `tracks/<EB>/flag_tab.csv` and hidden; `sheets.drop_done_tabs_after_d` days later it is deleted.
- **Sign-ups.** The first time a name appears in `QA Reviewer` the bridge stamps `Signed up (UTC)` in Track Summary. A sign-up without a valid email shows as a "needs you" item.
- **Verdicts (M1).** Read only while the track is `AWAITING_REVIEW`; a verdict set earlier is cleared, with a note in M2.
  - **RESTART** reruns the pipeline with every ticked row, including rows from earlier iterations, plus the avoid-refant list in O1. Every row is checked first; problems go to the row's `Check` cell and M2, and the verdict stays in M1 until the rows are fixed.
  - **COMPLETE** promotes the track.
  - **MANUAL REVIEW** and **HELP REQUESTED** put the track on HOLD for the operator, with no rerun, until a later COMPLETE or RESTART.
  - Once the controller has applied a verdict, M2 says so and M1 is cleared, unless the reviewer has changed it in the meantime.
- **Bridge state** is in `<root>/sheets/` (`bridge.json`, `mirror.json`, `status.json`). `python -m trackmanager.status --root <root>` shows the sheets line, any alarm, and the "needs you" items.
- **Protections** are read back every `sheets.protection_check_cycles` cycles. Any drift raises an alarm but changes nothing.
- **If Google is unreachable,** no track state changes. The alarm is raised after 3 failed steps in a row.

## Notes
- Nothing reviewers type into the sheets is executed. The controller rebuilds every flag command from the raw cells and checks it against the track's metadata (`trackmanager/flagcmds.py`).
- NRAO download links never go into the sheets, which are shared with reviewers. They come in only through the dashboard (plan §3.2).
