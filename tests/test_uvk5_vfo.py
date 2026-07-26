"""The VFO codec is a mirror of firmware layouts, so these tests are the mirror's only check.

Nothing here talks to a radio. What it pins is the set of facts that, when wrong, fail *silently*:
the 10 Hz unit, the power scale, the CTCSS index, the addresses, and the one byte whose unprogrammed
value locks the transmitter. Each of those has already cost a cycle somewhere in this project.
"""

from __future__ import annotations

import pytest

from radio_server.backends.uvk5 import vfo as V
from radio_server.backends.uvk5.frames import CTCSS_OPTIONS, OFFSET_ADD, OFFSET_NONE, OFFSET_SUB
from radio_server.presets import Preset


# --- band resolution -------------------------------------------------------------------------

@pytest.mark.parametrize(
    "hz, band",
    [
        (145_145_000, 2),   # 2 m
        (144_545_000, 2),   # its repeater input
        (448_525_000, 5),   # 70 cm
        (443_525_000, 5),
        (445_800_000, 5),   # the bench frequency
        (137_000_000, 2),   # exactly on a lower edge
        (400_000_000, 5),
    ],
)
def test_band_for_known_frequencies(hz, band):
    assert V.band_for(hz) == band


def test_band_for_refuses_instead_of_clamping():
    """The firmware's own GetBand clamps, which is exactly why this one must not.

    A host that clamped would write an out-of-band channel into a real VFO slot and calibrate the
    PA for a band the frequency is nowhere near (ADR 0132/0134).
    """
    with pytest.raises(V.BandError):
        V.band_for(1_000)
    with pytest.raises(V.BandError):
        V.band_for(4_485_250_000)      # 448.525 MHz mistakenly left in 10 Hz units


# --- addresses -------------------------------------------------------------------------------

def test_vfo_addresses_match_settings_c():
    # settings.c:1168-1171 -> base 0x9000, +32 per band, +16 for VFO B.
    assert V.vfo_addr(5, 0) == 0x90A0        # 70 cm, VFO A
    assert V.vfo_addr(5, 1) == 0x90B0        # 70 cm, VFO B
    assert V.vfo_addr(2, 0) == 0x9040        # 2 m, VFO A
    assert V.vfo_addr(0, 0) == V.VFO_BLOCK_BASE


def test_vfo_addr_rejects_nonsense():
    with pytest.raises(ValueError):
        V.vfo_addr(5, 2)
    with pytest.raises(ValueError):
        V.vfo_addr(99, 0)


def test_channel_and_attribute_addresses():
    assert V.freq_channel(5) == 1029         # FREQ_CHANNEL_FIRST (1024) + band
    assert V.attr_addr(1029) == 0x880A
    # Non-0xFFFF is the whole requirement: 0xFFFF makes radio.c:302-313 skip the VFO record
    # entirely and boot to the band's lower edge instead.
    assert V.attribute_word(5) != V.ATTR_UNSET


# --- the 16-byte record ----------------------------------------------------------------------

K0PRA = V.VfoImage(rx_hz=448_525_000, tx_hz=443_525_000, ctcss_tenths=1000,
                   narrow=False, power=V.POWER_HIGH)


def test_pack_eeprom_is_byte_exact():
    """Hand-computed against settings.c:1185-1208, not against pack_eeprom's own arithmetic."""
    assert K0PRA.pack_eeprom() == bytes([
        0x14, 0x65, 0xAC, 0x02,   # rx     44 852 500  = 448.525 MHz / 10
        0x20, 0xA1, 0x07, 0x00,   # offset    500 000  =   5.000 MHz / 10
        0x00,                     # [8]  rx code — no RX tone squelch
        12,                       # [9]  tx code — index of 1000 in dcs.c CTCSS_Options
        0x10,                     # [10] (CONTINUOUS_TONE << 4) | CODE_TYPE_OFF
        0x02,                     # [11] (MODULATION_FM << 4) | OFFSET_SUB
        0x1C,                     # [12] TX_LOCK=0, power HIGH(7)<<2, wide
        0x00,                     # [13] PTT-ID / DTMF
        0x04,                     # [14] STEP_12_5kHz
        0x00,                     # [15] scrambler
    ])


def test_tx_lock_bit_is_always_clear():
    """0xFF in byte 12 reads back as TX_LOCK=true (radio.c:377-383) — a radio that silently
    refuses to transmit. Every variant must write the byte, and never with bit 6 set."""
    for image in (
        K0PRA,
        V.VfoImage(445_800_000, 445_800_000),
        V.VfoImage(145_145_000, 144_545_000, ctcss_tenths=1072, narrow=True, power=V.POWER_LOW),
    ):
        flags = image.pack_eeprom()[12]
        assert flags != 0xFF
        assert not (flags >> 6) & 1


def test_power_is_written_on_the_radios_scale_not_the_wires():
    """The bug this exists to prevent: wire "high" (2) written raw lands on OUTPUT_POWER_LOW2,
    and the repeater never opens."""
    assert (V.VfoImage(445_800_000, 445_800_000, power=V.POWER_HIGH).pack_eeprom()[12] >> 2) & 7 == 7
    assert (V.VfoImage(445_800_000, 445_800_000, power=V.POWER_MID).pack_eeprom()[12] >> 2) & 7 == 6
    assert (V.VfoImage(445_800_000, 445_800_000, power=V.POWER_LOW).pack_eeprom()[12] >> 2) & 7 == 1
    # OUTPUT_POWER_USER is never emitted: it is not "lowest", it is a user-configured special case.
    assert 0 not in V.FIRMWARE_POWER


@pytest.mark.parametrize("image", [
    K0PRA,
    V.VfoImage(445_800_000, 445_800_000),                                   # simplex, no tone
    V.VfoImage(445_800_000, 446_400_000, ctcss_tenths=1000),                # positive offset
    V.VfoImage(145_145_000, 144_545_000, ctcss_tenths=1072, narrow=True),   # 2 m, narrow
    V.VfoImage(446_000_000, 446_000_000, power=V.POWER_LOW),
])
def test_eeprom_round_trip(image):
    assert V.VfoImage.unpack_eeprom(image.pack_eeprom()) == image


def test_record_length_is_what_the_firmware_reads():
    assert len(K0PRA.pack_eeprom()) == V.VFO_RECORD_LEN == 16


# --- derived fields --------------------------------------------------------------------------

def test_offset_and_direction_mirror_radio_apply_offset():
    assert K0PRA.direction == OFFSET_SUB and K0PRA.offset_hz == 5_000_000
    up = V.VfoImage(445_800_000, 446_400_000)
    assert up.direction == OFFSET_ADD and up.offset_hz == 600_000
    flat = V.VfoImage(445_800_000, 445_800_000)
    assert flat.direction == OFFSET_NONE and flat.offset_hz == 0


def test_ctcss_index_is_a_table_position():
    assert K0PRA.ctcss_index == CTCSS_OPTIONS.index(1000)
    assert V.VfoImage(445_800_000, 445_800_000).ctcss_index == 0


# --- validation ------------------------------------------------------------------------------

def test_rejects_a_tone_the_radio_does_not_have():
    with pytest.raises(ValueError, match="not a tone this radio has"):
        V.VfoImage(445_800_000, 445_800_000, ctcss_tenths=999)


def test_rejects_a_transmit_leg_outside_every_band():
    """RX can be perfectly legal while the split puts TX nowhere — and TX is the one that
    radiates, so it is checked just as hard."""
    with pytest.raises(V.BandError):
        V.VfoImage(rx_hz=145_145_000, tx_hz=5_000)


def test_rejects_bad_power():
    with pytest.raises(ValueError, match="power must be"):
        V.VfoImage(445_800_000, 445_800_000, power=7)


# --- presets ---------------------------------------------------------------------------------

def test_from_preset_carries_the_repeater_split():
    preset = Preset(name="K0PRA448.525", frequency=448_525_000, tx_frequency=443_525_000,
                    tx_tone=100.0, rx_tone=100.0, mode="FM")
    image = V.VfoImage.from_preset(preset)
    assert (image.rx_hz, image.tx_hz, image.ctcss_tenths) == (448_525_000, 443_525_000, 1000)
    assert image.direction == OFFSET_SUB and not image.narrow


def test_from_preset_simplex_transmits_where_it_listens():
    preset = Preset(name="Bench", frequency=445_800_000, mode="NFM")
    image = V.VfoImage.from_preset(preset)
    assert image.rx_hz == image.tx_hz == 445_800_000
    assert image.direction == OFFSET_NONE and image.narrow


def test_from_preset_ignores_rx_tone():
    """rx_tone is round-trip fidelity only (ADR 0133). Honouring it here would be a squelch the
    rest of the stack does not implement, applied where nobody could see it."""
    preset = Preset(name="R", frequency=448_525_000, tx_frequency=443_525_000, rx_tone=100.0)
    assert V.VfoImage.from_preset(preset).ctcss_tenths == 0


# --- boot indices ----------------------------------------------------------------------------

def test_boot_indices_round_trip_in_settings_c_order():
    packed = V.pack_boot_indices(screen=(1029, 1026), mr=(0, 0), freq=(1029, 1026))
    assert len(packed) == V.BOOT_INDEX_LEN
    # settings.c:856-861 — Screen[0], Mr[0], Freq[0], Screen[1], Mr[1], Freq[1]
    assert packed[0:2] == (1029).to_bytes(2, "little")
    assert packed[2:4] == (0).to_bytes(2, "little")
    assert packed[4:6] == (1029).to_bytes(2, "little")
    assert packed[6:8] == (1026).to_bytes(2, "little")
    assert V.unpack_boot_indices(packed) == {
        "screen": (1029, 1026), "mr": (0, 0), "freq": (1029, 1026)
    }
