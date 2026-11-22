# Test fixtures

- `tap_24B-224.csv.gz`: NRAO TAP result for project 24B-224, **not** restricted to Execution Blocks
  (3,494 EB scan rows for 47 EBs plus 1,565 image rows). Recorded with
  `scripts/record_tap_fixture.py 24B-224`.
- `sheet_24B-224.csv`: the 44 rows of the old tracker workbook for that project (Trackname, Target,
  SBID, EBID, Configuration only). Made with `scripts/extract_sheet_fixture.py`.
- `tap_synthetic.csv`: hand-made project 99A-001 (one EB per preflight outcome, an image row, and a
  second project code) for the archive, preflight and controller driver tests.
Used through `archive.tap_url: file://<path>` (see `trackmanager/archive.py`).
