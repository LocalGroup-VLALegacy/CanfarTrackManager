"""SDM reader (trackmanager/sdm.py), against the real IC10 XML tables and generated SDMs."""
import shutil

import pytest

from helpers import FIXTURES, make_fake_sdm
from trackmanager import sdm

IC10 = FIXTURES / "sdm_ic10"


@pytest.fixture
def ic10(tmp_path):
    d = tmp_path / "20A-346.sb42440562.eb42455346.59786.372811249996"
    (d / "ASDMBinary").mkdir(parents=True)
    for x in IC10.glob("*.xml"):
        shutil.copy(x, d)
    for n in (IC10 / "bdf_names.txt").read_text().split():
        (d / "ASDMBinary" / n).write_bytes(b"0" * 10)
    return d


def test_real_ic10_tables_give_the_scan_to_field_to_intent_split(ic10):
    assert sdm.main_rows(ic10) == 7 == sdm.bdf_count(ic10)
    fi = sdm.field_intents(ic10)
    assert fi["IC10_1_CTR"] == {"scans": 1, "intents": ["OBSERVE_TARGET"]}
    assert fi["3C48"]["intents"] == ["CALIBRATE_BANDPASS", "CALIBRATE_DELAY", "CALIBRATE_FLUX", "SYSTEM_CONFIGURATION"]
    assert fi["3C48"]["scans"] == 4
    assert fi["J0102+5824"] == {"scans": 2, "intents": ["CALIBRATE_AMPLI", "CALIBRATE_PHASE"]}


def test_intent_syntax_variants():
    assert sdm.parse_intents("1 2 CALIBRATE_PHASE#UNSPECIFIED OBSERVE_TARGET#ON_SOURCE") == \
        ["CALIBRATE_PHASE", "OBSERVE_TARGET"]
    assert sdm.parse_intents("OBSERVE_TARGET") == ["OBSERVE_TARGET"]
    assert sdm.parse_intents("") == []


def test_byte_size_counts_files_and_does_not_follow_inner_symlinks(tmp_path):
    d = make_fake_sdm(tmp_path, bdf_bytes=1000)
    other = tmp_path / "big.bin"
    other.write_bytes(b"0" * 10_000)
    (d / "link").symlink_to(other)
    plain = sum(p.stat().st_size for p in d.rglob("*") if p.is_file() and not p.is_symlink())
    assert sdm.byte_size(d) == plain + (d / "link").lstat().st_size
    link = tmp_path / "alias"
    link.symlink_to(d)
    assert sdm.byte_size(link) == sdm.byte_size(d)     # an SDM reached through a symlink is measured, not 0


def test_mosaic_fields_share_a_source_but_are_reported_separately(tmp_path):
    d = make_fake_sdm(tmp_path, scans=[(1, "M31_A", ["OBSERVE_TARGET"]), (2, "M31_B", ["OBSERVE_TARGET"]),
                                       (3, "3C48", ["CALIBRATE_FLUX"])])
    assert set(sdm.field_intents(d)) == {"M31_A", "M31_B", "3C48"}


def test_broken_tables_raise_sdmerror(tmp_path):
    d = make_fake_sdm(tmp_path)
    (d / "Main.xml").write_text("<MainTable><row>")
    with pytest.raises(sdm.SdmError, match="Main.xml"):
        sdm.main_rows(d)
    (tmp_path / "x").mkdir()
    d2 = make_fake_sdm(tmp_path / "x")
    (d2 / "Field.xml").write_text('<FieldTable><row><fieldId>Other</fieldId><fieldName>z</fieldName></row></FieldTable>')
    with pytest.raises(sdm.SdmError, match="does not define"):
        sdm.field_intents(d2)
    assert sdm.bdf_count(tmp_path / "nowhere") == 0
