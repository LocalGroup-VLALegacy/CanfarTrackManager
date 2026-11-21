"""Sample inputs for the Sheets preview and tests: the IC10 D test track (archive values from the
TAP probe, state from runs/ic10) plus a made-up second track. Reviewer names and emails are fake."""

PROJECT = {
    "project": {"name": "VVINGS"},
    "review": {"email_prefix": "VVINGS-QA"},
    "sheets": {"editors": ["koch.eric.w@gmail.com"],   # human editors of protected ranges; add more here
               "links": {"qa_guide": "https://example.org/qa-guide",
                         "flag_rows_help": "https://example.org/flag-rows-help"}},
}

IC10_ARCHIVE = {
    "ebid": "42455346", "sbid": "42440562", "project": "20A-346", "mjd": "59786.372811249996",
    "sdm": "20A-346.sb42440562.eb42455346.59786.372811249996", "date": "2022-07-26",
    "start": "2022-07-26 08:56:51", "end": "2022-07-26 09:21:41", "duration_min": 24.8,
    "config": "D", "target": "IC10", "fields": ["3C48", "IC10_1_CTR", "J0102+5824"],
    "on_source_min": 24.8, "est_size_gb": 16.6, "archive_status": "PUBLIC",
    "archive_url": "https://data.nrao.edu/portal/#/subscanViewer/20A-346.sb42440562.eb42455346.59786.372811249996",
    "scans": [1, 2, 3, 4, 5, 6, 7], "n_spw": 32,
    "antennas": [f"ea{i:02d}" for i in range(1, 29) if i != 20],
}


def samples():
    ic10 = {
        "archive": IC10_ARCHIVE,
        "state": {"stage": "AWAITING_REVIEW", "iteration": 2, "detail": "iteration 2 attempt 1 verified (platform: Completed)",
                  "updated": "2026-11-21 08:30:00"},
        "job": {"job_id": "yxd9dthj", "wall_h": 5.5, "peak_disk_gb": 121.4},
        "run": {"sdm_gb": 16.6},
        "review": {"reviewer": "Reviewer A", "signed_up": "2026-11-21 03:10:00", "verdict": "RESTART",
                   "refant_ignore": "ea26", "ack": "iteration 1 verdict RESTART applied 2026-11-21 02:55 UTC",
                   "completed": ""},
        "links": {"qa": "https://example.org/qa/ic10d-eb42455346/iter_2/products/index.html",
                  "flag_tab": "https://docs.google.com/spreadsheets/d/EXAMPLE/edit#gid=1"},
        "signup": {"QA Reviewer": "Reviewer A", "Email": "reviewer.a@example.org", "Notes": "", "Notified iter": 2},
        "flag_rows": [
            {"Apply Row": True, "Issue Description": "ea10 noisy in phase-cal scan", "Field Name": "",
             "Scan": "5", "Antenna Name": "ea10", "Name of QA person": "Reviewer A", "Added in iter": 2},
            {"Apply Row": True, "Issue Description": "RFI in spw 7 on the flux cal", "Field Name": "3C48",
             "SPW": "7", "Name of QA person": "Reviewer A", "Added in iter": 2},
        ],
    }
    other = {
        "archive": {**IC10_ARCHIVE, "ebid": "42000001", "sdm": "20A-346.sb42000000.eb42000001.59800.1",
                    "mjd": "59800.1", "date": "2022-08-09", "config": "C", "target": "WLM",
                    "fields": ["3C138", "WLM_1", "J0006-0623"], "est_size_gb": 184.0, "archive_status": "PUBLIC"},
        "state": {"stage": "CAL_RUNNING", "iteration": 1, "detail": "submitting iteration 1 attempt 1",
                  "updated": "2026-11-21 06:00:00"},
        "job": {"job_id": "abcd1234"}, "run": {}, "review": {}, "links": {},
        "signup": {"QA Reviewer": "Reviewer B", "Email": "reviewer.b@example.org", "Notes": "away until Dec 1"},
    }
    return [ic10, other]
