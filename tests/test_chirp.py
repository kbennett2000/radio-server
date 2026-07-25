"""The CHIRP CSV importer (ADR 0133) — translating a real channel list without guessing.

The fixture is the operator's actual 37-repeater export, because the properties that matter only
show up in real data:

* **26 of the 37 rows are `TSQL` with `rToneFreq = 88.5 != cToneFreq`.** Reading `rToneFreq` for
  TSQL — which is the *correct* source for a `Tone` row — would transmit 88.5 Hz at repeaters
  expecting 100.0/103.5/107.2/123.0/141.3 and none of them would key. The two rows where the two
  columns agree cannot tell the mappings apart, so the regression test pins a row where they differ.
* **The subtraction is where float arithmetic breaks**, not the parse: `(145.145 - 0.600) * 1e6` is
  `144545000.00000003`, and truncating that is 1 Hz low — which the backends reject as off-raster.
* **The export is 9 columns, with no `Cross Mode`,** so the one `Cross` row's receive tone cannot be
  known, only assumed. The importer says so rather than pretending.

The load-bearing proofs: the three tone mappings, the offset sign, integer-exact frequencies, and
that every unsupported shape is skipped out loud instead of silently approximated.
"""

from __future__ import annotations

import pytest

from radio_server.chirp import ChirpError, main, parse_chirp_csv
from radio_server.presets import CTCSS_TONES, resolve_presets

HEADER = "Location,Name,Frequency,Duplex,Offset,Tone,rToneFreq,cToneFreq,Mode"

# The operator's export, verbatim (ADR 0133).
CHIRP_CSV = HEADER + "\n" + "\n".join([
    "3,W0CRA145.14,145.145000,-,0.600000,TSQL,88.5,107.2,FM",
    "4,K0PRA145.25,145.250000,-,0.600000,TSQL,88.5,100.0,FM",
    "5,K0PRA448.525,448.525000,-,5.000000,TSQL,88.5,100.0,FM",
    "6,WT0C 145.220,145.220000,-,0.600000,TSQL,88.5,103.5,FM",
    "7,KE0SJ 145.28,145.280000,-,0.600000,Tone,100.0,88.5,FM",
    "8,KE4GUQ145.34,145.340000,-,0.600000,Cross,103.5,127.3,FM",
    "9,KB0UDD 145.4,145.400000,-,0.600000,Tone,100.0,88.5,FM",
    "10,W0CRA 145.46,145.460000,-,0.600000,TSQL,88.5,107.2,FM",
    "11,KE0SJ 145.47,145.475000,-,0.600000,TSQL,88.5,100.0,FM",
    "12,W0TX 145.490,145.490000,-,0.600000,TSQL,88.5,100.0,FM",
    "13,KE0NCQ 146.6,146.640000,-,0.600000,Tone,100.0,88.5,FM",
    "14,AB0PC 146.89,146.895000,-,0.600000,TSQL,88.5,100.0,FM",
    "15,KE0VEV 146.9,146.910000,-,0.600000,Tone,107.2,88.5,FM",
    "16,W0WYX 146.94,146.940000,-,0.600000,TSQL,88.5,103.5,FM",
    "17,K0FEZ 146.98,146.985000,-,0.600000,TSQL,88.5,100.0,FM",
    "18,WA0DE 147.10,147.105000,+,0.600000,TSQL,88.5,107.2,FM",
    "19,W0CRA 147.22,147.225000,+,0.600000,TSQL,88.5,107.2,FM",
    "20,N0PYY 147.30,147.300000,+,0.600000,TSQL,88.5,103.5,FM",
    "21,N0OWY 447.50,447.500000,-,5.000000,TSQL,88.5,88.5,FM",
    "22,W0CRA 447.57,447.575000,-,5.000000,TSQL,88.5,107.2,FM",
    "23,KE0SJ 447.62,447.625000,-,5.000000,Tone,100.0,88.5,FM",
    "24,W0UPS 447.70,447.700000,-,5.000000,TSQL,88.5,100.0,FM",
    "25,N0SZ 447.750,447.750000,-,5.000000,TSQL,88.5,141.3,FM",
    "26,KC0CVU 448.0,448.000000,-,5.000000,Tone,107.2,88.5,FM",
    "27,KC0CVU 448.1,448.100000,-,5.000000,TSQL,88.5,107.2,FM",
    "28,N0SZ 448.225,448.225000,-,5.000000,TSQL,88.5,141.3,FM",
    "29,W0CRA 448.42,448.425000,-,5.000000,TSQL,88.5,107.2,FM",
    "30,KC0KWD 448.4,448.475000,-,5.000000,TSQL,88.5,100.0,FM",
    "31,W0TX 448.625,448.625000,-,5.000000,TSQL,88.5,100.0,FM",
    "32,K0PWO 448.77,448.775000,-,5.000000,TSQL,88.5,123.0,FM",
    "33,K0IBM 448.85,448.850000,-,5.000000,TSQL,88.5,88.5,FM",
    "34,W0GV 448.975,448.975000,-,5.000000,Tone,123.0,88.5,FM",
    "35,WG0N 449.050,449.050000,-,5.000000,TSQL,88.5,107.2,FM",
    "36,N0SZ 449.225,449.225000,-,5.000000,TSQL,88.5,141.3,FM",
    "37,W0TX 449.350,449.350000,-,5.000000,TSQL,88.5,100.0,FM",
    "38,W0WYX 449.82,449.825000,-,5.000000,Tone,103.5,88.5,FM",
    "39,WE0FUN 449.9,449.975000,-,5.000000,TSQL,88.5,100.0,FM",
]) + "\n"


def by_name(result) -> dict[str, dict]:
    return {p["name"]: p for p in result.presets}


# --- the whole file ------------------------------------------------------------------------

def test_the_operators_export_imports_whole():
    result = parse_chirp_csv(CHIRP_CSV)
    assert len(result.presets) == 37
    assert result.skipped == []


def test_every_imported_preset_survives_the_config_validator():
    """The importer's output has to be loadable, or the station will not start (ADR 0133)."""
    result = parse_chirp_csv(CHIRP_CSV)
    presets = resolve_presets([{k: v for k, v in p.items() if k != "comment"} for p in result.presets])
    assert len(presets) == 37


def test_every_frequency_lands_exactly_on_the_tuning_raster():
    """Integer arithmetic, end to end. The float version is 1 Hz low often enough to matter, and the
    backends reject an off-raster frequency with a 422 at apply time — one repeater out of forty."""
    for preset in parse_chirp_csv(CHIRP_CSV).presets:
        assert preset["frequency"] % 10 == 0, preset["name"]
        if "tx_frequency" in preset:
            assert preset["tx_frequency"] % 10 == 0, preset["name"]


def test_every_leg_stays_inside_the_ham_bands():
    """A repeater's input is still a transmit frequency; an import that walked out of band would be
    an out-of-band transmission with an operator's name on it."""
    def in_band(hz: int) -> bool:
        return 144_000_000 <= hz <= 148_000_000 or 420_000_000 <= hz <= 450_000_000

    for preset in parse_chirp_csv(CHIRP_CSV).presets:
        assert in_band(preset["frequency"]), preset["name"]
        if "tx_frequency" in preset:
            assert in_band(preset["tx_frequency"]), preset["name"]


def test_every_tone_is_a_standard_ctcss_tone():
    for preset in parse_chirp_csv(CHIRP_CSV).presets:
        for key in ("tx_tone", "rx_tone"):
            if key in preset:
                assert preset[key] in CTCSS_TONES, (preset["name"], key)


# --- the three tone mappings ---------------------------------------------------------------

def test_tsql_takes_the_tone_from_the_ctone_column():
    """The mapping that would fail silently on the air: this row's rToneFreq is 88.5 and the tone
    that actually opens the repeater is 107.2. Deliberately NOT one of the two rows where the two
    columns agree — those cannot tell a correct importer from a broken one."""
    entry = by_name(parse_chirp_csv(CHIRP_CSV))["W0CRA145.14"]
    assert entry["tx_tone"] == 107.2
    assert entry["rx_tone"] == 107.2


def test_tone_mode_takes_the_tone_from_the_rtone_column_and_has_no_receive_tone():
    entry = by_name(parse_chirp_csv(CHIRP_CSV))["KE0SJ 145.28"]
    assert entry["tx_tone"] == 100.0
    assert "rx_tone" not in entry


def test_cross_takes_transmit_from_rtone_and_receive_from_ctone():
    entry = by_name(parse_chirp_csv(CHIRP_CSV))["KE4GUQ145.34"]
    assert entry["tx_tone"] == 103.5
    assert entry["rx_tone"] == 127.3


def test_a_cross_row_without_the_cross_mode_column_says_it_assumed():
    """Nine columns cannot express which cross mode it is. Assume, and name the row (guardrail 1)."""
    result = parse_chirp_csv(CHIRP_CSV)
    assert len(result.assumed) == 1
    assert "KE4GUQ145.34" in result.assumed[0]
    assert "Tone->Tone" in result.assumed[0]


def test_a_cross_mode_column_is_believed_when_present():
    csv = (
        HEADER + ",Cross Mode\n"
        "1,Good,145.340000,-,0.600000,Cross,103.5,127.3,FM,Tone->Tone\n"
        "2,Dtcs,145.360000,-,0.600000,Cross,103.5,127.3,FM,Tone->DTCS\n"
    )
    result = parse_chirp_csv(csv)
    assert [p["name"] for p in result.presets] == ["Good"]
    assert result.assumed == []  # it was read, not assumed
    assert "Tone->DTCS" in result.skipped[0]


# --- duplex / offset -----------------------------------------------------------------------

def test_a_minus_offset_transmits_below_the_output():
    entry = by_name(parse_chirp_csv(CHIRP_CSV))["W0CRA145.14"]
    assert entry["frequency"] == 145_145_000
    assert entry["tx_frequency"] == 144_545_000  # the case float subtraction gets wrong


def test_a_plus_offset_transmits_above_the_output():
    entry = by_name(parse_chirp_csv(CHIRP_CSV))["WA0DE 147.10"]
    assert entry["frequency"] == 147_105_000
    assert entry["tx_frequency"] == 147_705_000


def test_a_blank_duplex_is_simplex_with_no_transmit_leg():
    result = parse_chirp_csv(HEADER + "\n1,Simplex,146.520000,,0.000000,,88.5,88.5,FM\n")
    assert "tx_frequency" not in result.presets[0]


# --- refusing to guess ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "row, because",
    [
        ("1,Dtcs,145.340000,-,0.600000,DTCS,103.5,127.3,FM", "DTCS"),
        ("1,Rev,145.340000,-,0.600000,TSQL-R,103.5,127.3,FM", "TSQL-R"),
        ("1,Split,145.340000,split,144.900000,Tone,103.5,127.3,FM", "split"),
        ("1,Off,145.340000,off,0.600000,Tone,103.5,127.3,FM", "off"),
        ("1,Wide,145.340000,-,0.600000,Tone,103.5,127.3,WFM", "WFM"),
        ("1,OddTone,145.340000,-,0.600000,Tone,99.9,127.3,FM", "99.9"),
        ("1,,145.340000,-,0.600000,Tone,103.5,127.3,FM", "name"),
    ],
)
def test_a_row_it_cannot_translate_is_skipped_out_loud(row, because):
    """Never approximate. A DTCS row read as 'no tone' is a preset that quietly will not key its
    repeater — the same shape of silent wrongness as inverting TSQL."""
    result = parse_chirp_csv(HEADER + "\n" + row + "\n")
    assert result.presets == []
    assert len(result.skipped) == 1
    assert because in result.skipped[0]


def test_a_duplicate_name_is_skipped_rather_than_written_twice():
    """resolve_presets rejects case-insensitive duplicates — writing one would stop the station."""
    csv = (
        HEADER + "\n"
        "1,W0CRA 145.46,145.460000,-,0.600000,Tone,100.0,88.5,FM\n"
        "2,w0cra 145.46,146.460000,-,0.600000,Tone,100.0,88.5,FM\n"
    )
    result = parse_chirp_csv(csv)
    assert len(result.presets) == 1
    assert "duplicate" in result.skipped[0]


def test_a_file_missing_a_column_names_the_column():
    with pytest.raises(ChirpError, match="cToneFreq"):
        parse_chirp_csv("Location,Name,Frequency,Duplex,Offset,Tone,rToneFreq,Mode\n")


def test_an_empty_file_is_refused():
    with pytest.raises(ChirpError, match="empty"):
        parse_chirp_csv("")


# --- the CLI -------------------------------------------------------------------------------

def test_the_cli_merges_into_an_existing_radio_toml(tmp_path, capsys):
    from radio_server.config import load_presets

    csv_path = tmp_path / "channels.csv"
    csv_path.write_text(CHIRP_CSV)
    cfg = tmp_path / "radio.toml"
    cfg.write_text('[station]\ncallsign = "N0CALL"\n\n[[presets]]\nname = "Bench"\nfrequency = 445800000\n')

    assert main([str(csv_path), "--into", str(cfg)]) == 0
    entries = load_presets(cfg)
    assert len(entries) == 38  # the bench entry plus the operator's 37
    assert entries[0]["name"] == "Bench"  # appended, never prepended
    assert "[station]" in cfg.read_text()
    assert (tmp_path / "radio.toml.bak").is_file()
    resolve_presets(entries)


def test_the_cli_refuses_to_write_a_config_that_would_not_load(tmp_path, capsys):
    """One bad entry does not degrade the station — it stops it from starting, and radio.toml has no
    version control behind it. So validate the MERGED list first and leave the file alone."""
    csv_path = tmp_path / "channels.csv"
    csv_path.write_text(HEADER + "\n1,Bench,145.460000,-,0.600000,Tone,100.0,88.5,FM\n")
    cfg = tmp_path / "radio.toml"
    # An existing entry the current validator rejects (the pre-rename tone spelling).
    original = '[[presets]]\nname = "Old"\nfrequency = 445800000\ntone = 100.0\n'
    cfg.write_text(original)

    assert main([str(csv_path), "--into", str(cfg)]) == 1
    assert cfg.read_text() == original  # untouched
    assert "refusing to write" in capsys.readouterr().err


def test_the_cli_prints_without_writing_when_no_target_is_given(tmp_path, capsys):
    csv_path = tmp_path / "channels.csv"
    csv_path.write_text(CHIRP_CSV)
    assert main([str(csv_path)]) == 0
    out = capsys.readouterr().out
    assert "37 channel(s) read" in out
    assert "W0CRA145.14" in out
    assert "145.1450 -0.600" in out  # the offset a ham reads the repeater by


def test_running_the_cli_twice_leaves_the_file_byte_identical(tmp_path):
    csv_path = tmp_path / "channels.csv"
    csv_path.write_text(CHIRP_CSV)
    cfg = tmp_path / "radio.toml"
    cfg.write_text('[station]\ncallsign = "N0CALL"\n')

    main([str(csv_path), "--into", str(cfg)])
    once = cfg.read_text()
    main([str(csv_path), "--into", str(cfg)])
    assert cfg.read_text() == once
