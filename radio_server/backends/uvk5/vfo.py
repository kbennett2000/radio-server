"""One repeater channel, and the two byte layouts the UV-K5 will accept it in. Pure; no I/O.

`VfoImage` is what a `Preset` becomes on its way to the radio: where to listen, where to transmit,
which tone, how wide, how hard. Both tuners consume it — `SetVfoTuner` packs it into the 13-byte
``0x0873`` payload, `EepromTuner` into the 16-byte channel record the firmware reads at boot — so
the field semantics are agreed in exactly one place.

Everything here is a mirror of the firmware, and every mirror is a thing that can drift. The
citations are load-bearing, not decoration:

* **Frequencies are stored in units of 10 Hz**, not Hz (``App/frequencies.c`` has 400 MHz as
  ``40000000``). This is the one that already bit: the ``0x0873`` host frame speaks Hz and the
  first firmware draft assigned it straight into the VFO, tuning ten times too high. Nothing
  complained, because ``FREQUENCY_GetBand()`` clamps instead of reporting a miss.
* **The power scale is not the wire's.** ``OUTPUT_POWER_*`` runs ``USER, LOW1..LOW5, MID, HIGH``
  (``App/settings.h:91-99``), so a naive "2 means high" writes ``LOW2`` — the tune looks perfect
  and the repeater never opens. :data:`FIRMWARE_POWER` is the mapping.
* **The CTCSS index is a position in a specific table** (:data:`~.frames.CTCSS_OPTIONS`, mirroring
  ``dcs.c``), so the order of that table decides which tone gets transmitted.
* **Band must match frequency.** For a frequency-mode channel the firmware takes
  ``band = channel - FREQ_CHANNEL_FIRST`` and the PA bias is calibrated per band, so writing a
  145 MHz frequency into the UHF slot is the wrong-band-PA fault of ADR 0132/0134.

The band edges below are those of the **Fusion** build, which sets ``ENABLE_WIDE_RX`` — that widens
band 1 down to 18 MHz and band 7 up to 1.3 GHz. Only the EEPROM path depends on them; ``0x0873``
lets the firmware decide the band and answers ``ERR_BAND`` when it cannot, so that path cannot be
wrong about this even if this table is.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import StrEnum

from .frames import CTCSS_OPTIONS, OFFSET_ADD, OFFSET_NONE, OFFSET_SUB

__all__ = [
    "HZ_PER_UNIT",
    "FREQ_CHANNEL_FIRST",
    "BAND_EDGES_HZ",
    "FIRMWARE_POWER",
    "POWER_LOW",
    "POWER_MID",
    "POWER_HIGH",
    "PowerLevel",
    "DEFAULT_POWER",
    "VFO_BLOCK_BASE",
    "ATTR_BASE",
    "BOOT_INDEX_BLOCK",
    "VFO_RECORD_LEN",
    "BandError",
    "band_for",
    "freq_channel",
    "vfo_addr",
    "attr_addr",
    "attribute_word",
    "pack_boot_indices",
    "unpack_boot_indices",
    "VfoImage",
]

#: The radio stores frequencies in units of 10 Hz. See the module note — this is the bug that was.
HZ_PER_UNIT = 10

#: ``misc.h:40`` — ``MR_CHANNELS_MAX``, above which channel numbers name a band VFO, not a memory.
FREQ_CHANNEL_FIRST = 1024

#: ``frequencies.c:30-46`` as built for Fusion (``ENABLE_WIDE_RX``), in **Hz**, ``[lower, upper]``
#: inclusive of the lower edge. Index is the firmware's ``FREQUENCY_Band_t``.
BAND_EDGES_HZ: tuple[tuple[int, int], ...] = (
    (18_000_000, 108_000_000),    # BAND1_50MHz  (widened by ENABLE_WIDE_RX)
    (108_000_000, 137_000_000),   # BAND2_108MHz
    (137_000_000, 174_000_000),   # BAND3_137MHz — 2 m
    (174_000_000, 350_000_000),   # BAND4_174MHz
    (350_000_000, 400_000_000),   # BAND5_350MHz
    (400_000_000, 470_000_000),   # BAND6_400MHz — 70 cm
    (470_000_000, 1_300_000_000),  # BAND7_470MHz (widened by ENABLE_WIDE_RX)
)

#: The wire's three-step power scale, which is what `Preset`-level code and ``0x0873`` speak.
POWER_LOW, POWER_MID, POWER_HIGH = 0, 1, 2

#: Wire step → the firmware's own ``OUTPUT_POWER_*`` index (``App/settings.h:91-99``). Mirrors
#: ``DOCK_POWER_MAP`` in ``app/uart.c``. Note ``OUTPUT_POWER_USER`` (0) is deliberately unused: it
#: is not "lowest", it is a user-configured special case (``radio.c:591``).
FIRMWARE_POWER: tuple[int, ...] = (1, 6, 7)   # LOW1, MID, HIGH


class PowerLevel(StrEnum):
    """How hard to transmit, in the operator's words (ADR 0146).

    The three ints above are the *wire's* scale and stay inside this module and the frame layer;
    everything an operator can type — ``radio.toml``, a ``[[presets]]`` entry, ``POST /power``,
    ``GET /status`` — speaks these names. Three steps and no more, because three is what the
    firmware's dock map offers (``DOCK_POWER_MAP``); inventing a fourth here would be a level the
    radio silently rounds off.

    What each one *is* in watts is the radio's business, not this repo's: the firmware runs
    ``RADIO_ConfigureSquelchAndOutputPower`` and computes ``TXP_CalculatedSetting`` from its own
    per-band flash calibration, which the host cannot read (ADR 0128/0132). That is a feature here —
    it is the calibrated path — but it means nothing in this codebase may claim a wattage.
    """

    LOW = "low"
    MID = "mid"
    HIGH = "high"

    @property
    def step(self) -> int:
        """The wire's 0/1/2, which is all `VfoImage` and ``0x0873`` know about."""
        return _POWER_STEPS[self]

    @classmethod
    def from_step(cls, step: int) -> "PowerLevel":
        return _POWER_BY_STEP[step]

    @classmethod
    def from_firmware(cls, value: int) -> "PowerLevel | None":
        """Read the radio's own ``OUTPUT_POWER_*`` back into a level, or ``None`` if it is not one.

        ``None`` is a real answer and must not be collapsed into a guess: the radio's front panel
        can put it on ``LOW2``..``LOW5`` or ``USER``, which are levels this server did not set and
        cannot name. Reporting "low" for those would be inventing a number (ADR 0134's rule).
        """
        return _POWER_BY_FIRMWARE.get(value)


_POWER_STEPS: dict[PowerLevel, int] = {
    PowerLevel.LOW: POWER_LOW,
    PowerLevel.MID: POWER_MID,
    PowerLevel.HIGH: POWER_HIGH,
}
_POWER_BY_STEP: dict[int, PowerLevel] = {v: k for k, v in _POWER_STEPS.items()}
_POWER_BY_FIRMWARE: dict[int, PowerLevel] = {
    FIRMWARE_POWER[step]: level for level, step in _POWER_STEPS.items()
}

#: What a channel transmits at when nothing says otherwise. HIGH preserves the behaviour every
#: measurement before ADR 0146 was taken under — the radio reported ``power=7`` on all 186 tunes.
DEFAULT_POWER = PowerLevel.HIGH

#: ``settings.c:1168-1171`` / ``radio.c:337`` — band VFOs live at ``base + band*32 + vfo*16``.
VFO_BLOCK_BASE = 0x9000
VFO_BLOCK_STRIDE = 32
VFO_RECORD_LEN = 16

#: ``0x8000 + channel*2``, a ``ChannelAttributes_t`` word. ``0xFFFF`` means "unused", and the
#: firmware then **never reads the VFO record at all** (``radio.c:302-313`` returns early after
#: initialising to the band's lower edge). Programming it is not optional.
ATTR_BASE = 0x8000
ATTR_UNSET = 0xFFFF

#: ``settings.c:843-871`` — eight u16: ScreenChannel[0], MrChannel[0], FreqChannel[0], then the
#: same three for VFO 1. What the radio boots onto.
BOOT_INDEX_BLOCK = 0xA010
BOOT_INDEX_LEN = 16

#: ``radio.h`` — ``MODULATION_FM`` is the first member, and ``CODE_TYPE_CONTINUOUS_TONE`` the
#: second of ``CODE_TYPE_OFF, CONTINUOUS_TONE, DIGITAL, REVERSE_DIGITAL`` (``dcs.h:24-27``).
_MODULATION_FM = 0
_CODE_TYPE_OFF = 0
_CODE_TYPE_CTCSS = 1
#: ``frequencies.h:46-53`` — ``STEP_12_5kHz`` is index 4. Only affects manual tuning from the
#: front panel, but an out-of-range byte would be replaced by the firmware anyway.
_STEP_12_5KHZ = 4


class BandError(ValueError):
    """A frequency that is not inside any band this radio has."""


def band_for(hz: int) -> int:
    """The firmware band index containing ``hz``. Raises :class:`BandError` if there is none.

    Selection mirrors ``FREQUENCY_GetBand`` exactly — the **highest** band whose lower edge is at
    or below ``hz``, scanning down — because the bands share edges and a "first match going up"
    scan disagrees with the firmware on every one of them (400 MHz is band 6, not band 5).

    The *refusal* is what differs, and deliberately: ``FREQUENCY_GetBand`` clamps, answering
    ``BAND7_470MHz`` for 4 GHz and ``BAND1_50MHz`` for 1 Hz, so it can never say "no". A host that
    copied that would write an out-of-band channel into a real VFO slot and calibrate the PA for a
    band the frequency is nowhere near. So take its choice, then range-check it — which is what
    ``Dock_FreqInBand`` does on the radio side too.
    """
    for index in range(len(BAND_EDGES_HZ) - 1, -1, -1):
        lower, upper = BAND_EDGES_HZ[index]
        if hz >= lower:
            if hz > upper:
                break
            return index
    raise BandError(
        f"{hz} Hz is outside every band this radio has "
        f"({BAND_EDGES_HZ[0][0]}-{BAND_EDGES_HZ[-1][1]} Hz)"
    )


def freq_channel(band: int) -> int:
    """The channel number naming ``band``'s frequency-mode VFO."""
    return FREQ_CHANNEL_FIRST + band


def vfo_addr(band: int, vfo_index: int) -> int:
    """EEPROM address of the 16-byte VFO record for ``band`` on VFO A (0) or B (1)."""
    if vfo_index not in (0, 1):
        raise ValueError(f"vfo_index must be 0 or 1, got {vfo_index}")
    if not 0 <= band < len(BAND_EDGES_HZ):
        raise ValueError(f"band {band} is outside 0-{len(BAND_EDGES_HZ) - 1}")
    return VFO_BLOCK_BASE + band * VFO_BLOCK_STRIDE + vfo_index * VFO_RECORD_LEN


def attr_addr(channel: int) -> int:
    """EEPROM address of ``channel``'s two-byte attribute word."""
    return ATTR_BASE + channel * 2


def attribute_word(band: int) -> int:
    """A valid (non-``0xFFFF``) attribute word for a band VFO.

    Only "not unset" actually matters: for a frequency-mode channel the firmware overwrites both
    the band and the scan-list participation from the channel number itself (``radio.c:322-328``).
    The band is still written truthfully so a human reading the EEPROM sees something coherent.
    """
    return band & 0x07


def pack_boot_indices(screen: tuple[int, int], mr: tuple[int, int],
                      freq: tuple[int, int]) -> bytes:
    """The 16-byte block at :data:`BOOT_INDEX_BLOCK`, in ``settings.c``'s order."""
    return struct.pack(
        "<8H", screen[0], mr[0], freq[0], screen[1], mr[1], freq[1], 0, 0
    )


def unpack_boot_indices(data: bytes) -> dict[str, tuple[int, int]]:
    if len(data) != BOOT_INDEX_LEN:
        raise ValueError(f"boot index block is {BOOT_INDEX_LEN} bytes, got {len(data)}")
    s0, m0, f0, s1, m1, f1, _n0, _n1 = struct.unpack("<8H", data)
    return {"screen": (s0, s1), "mr": (m0, m1), "freq": (f0, f1)}


@dataclass(frozen=True)
class VfoImage:
    """One channel: listen on ``rx_hz``, transmit on ``tx_hz``, with ``ctcss_tenths``.

    ``tx_hz`` is absolute and equals ``rx_hz`` for simplex — the same "store the absolute
    frequency, derive the offset" rule `presets.Preset` follows, because an offset is a
    presentation detail and a frequency is not.
    """

    rx_hz: int
    tx_hz: int
    ctcss_tenths: int = 0
    narrow: bool = False
    power: int = POWER_HIGH

    def __post_init__(self) -> None:
        if self.rx_hz <= 0 or self.tx_hz <= 0:
            raise ValueError(f"frequencies must be positive: rx={self.rx_hz} tx={self.tx_hz}")
        if self.power not in (POWER_LOW, POWER_MID, POWER_HIGH):
            raise ValueError(f"power must be 0/1/2, got {self.power}")
        if self.ctcss_tenths and self.ctcss_tenths not in CTCSS_OPTIONS:
            raise ValueError(
                f"ctcss_tenths {self.ctcss_tenths} is not a tone this radio has (dcs.c "
                f"CTCSS_Options); the radio would refuse the tune rather than key without it"
            )
        # Both legs, because the one that radiates deserves at least as much checking as the one
        # that only listens — and a split can put TX outside the band RX is comfortably inside.
        band_for(self.rx_hz)
        band_for(self.tx_hz)

    @classmethod
    def from_preset(cls, preset, *, power: "PowerLevel" = DEFAULT_POWER) -> "VfoImage":
        """Build from a `presets.Preset`. ``rx_tone`` is not carried: nothing implements RX tone
        squelch (ADR 0133), and inventing one here would be the silent-difference kind of bug.

        ``power`` is the **station** level. A preset that names its own overrides it — a channel
        that needs low power needs it whatever the station was last set to — and the caller then
        adopts that as the new station level, so there is exactly one current level and it is the
        one `status()` reports (ADR 0146).
        """
        return cls(
            rx_hz=preset.frequency,
            tx_hz=preset.tx_frequency if preset.tx_frequency is not None else preset.frequency,
            ctcss_tenths=round(preset.tx_tone * 10) if preset.tx_tone else 0,
            narrow=(preset.mode.upper() == "NFM"),
            power=PowerLevel(preset.power or power).step,
        )

    @property
    def level(self) -> PowerLevel:
        """:attr:`power` as the name an operator uses."""
        return PowerLevel.from_step(self.power)

    @property
    def band(self) -> int:
        return band_for(self.rx_hz)

    @property
    def offset_hz(self) -> int:
        return abs(self.tx_hz - self.rx_hz)

    @property
    def direction(self) -> int:
        """``OFFSET_NONE`` / ``ADD`` / ``SUB``, mirroring ``RADIO_ApplyOffset``."""
        if self.tx_hz > self.rx_hz:
            return OFFSET_ADD
        if self.tx_hz < self.rx_hz:
            return OFFSET_SUB
        return OFFSET_NONE

    @property
    def ctcss_index(self) -> int:
        """Position in ``dcs.c``'s ``CTCSS_Options`` — what the EEPROM record stores."""
        return CTCSS_OPTIONS.index(self.ctcss_tenths) if self.ctcss_tenths else 0

    def pack_eeprom(self) -> bytes:
        """The 16-byte channel record, mirroring ``SETTINGS_SaveChannel`` (``settings.c:1174-1211``).

        Byte 12 is written explicitly on every path, never left at ``0xFF``: the loader reads that
        as "unprogrammed" and turns on ``TX_LOCK`` (``radio.c:377-383``), i.e. a radio that quietly
        refuses to transmit at all.
        """
        flags = (
            (0 << 6)                             # TX_LOCK — must be clear, see above
            | (0 << 5)                           # BUSY_CHANNEL_LOCK
            | (FIRMWARE_POWER[self.power] << 2)  # the radio's scale, not the wire's
            | (int(self.narrow) << 1)
            | 0                                  # FrequencyReverse
        )
        tone_type = _CODE_TYPE_CTCSS if self.ctcss_tenths else _CODE_TYPE_OFF
        return struct.pack(
            "<IIBBBBBBBB",
            self.rx_hz // HZ_PER_UNIT,
            self.offset_hz // HZ_PER_UNIT,
            0,                                       # [8]  rx code — no RX tone squelch (ADR 0133)
            self.ctcss_index,                        # [9]  tx code
            (tone_type << 4) | _CODE_TYPE_OFF,       # [10] (tx type << 4) | rx type
            (_MODULATION_FM << 4) | self.direction,  # [11]
            flags,                                   # [12]
            0,                                       # [13] PTT-ID / DTMF decode
            _STEP_12_5KHZ,                           # [14]
            0,                                       # [15] scrambler
        )

    @classmethod
    def unpack_eeprom(cls, data: bytes) -> "VfoImage":
        """Inverse of :meth:`pack_eeprom`, for read-back verification."""
        if len(data) != VFO_RECORD_LEN:
            raise ValueError(f"a VFO record is {VFO_RECORD_LEN} bytes, got {len(data)}")
        rx_u, off_u, _rx_code, tx_code, types, mod_dir, flags, _d13, _step, _scr = struct.unpack(
            "<IIBBBBBBBB", data
        )
        rx_hz = rx_u * HZ_PER_UNIT
        offset_hz = off_u * HZ_PER_UNIT
        direction = mod_dir & 0x0F
        if direction == OFFSET_ADD:
            tx_hz = rx_hz + offset_hz
        elif direction == OFFSET_SUB:
            tx_hz = rx_hz - offset_hz
        else:
            tx_hz = rx_hz
        tone = CTCSS_OPTIONS[tx_code] if ((types >> 4) & 0x0F) == _CODE_TYPE_CTCSS else 0
        fw_power = (flags >> 2) & 0x07
        power = FIRMWARE_POWER.index(fw_power) if fw_power in FIRMWARE_POWER else POWER_HIGH
        return cls(
            rx_hz=rx_hz,
            tx_hz=tx_hz,
            ctcss_tenths=tone,
            narrow=bool((flags >> 1) & 1),
            power=power,
        )
