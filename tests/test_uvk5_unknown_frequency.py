"""A UV-K5 that does not know where it is refuses to key (ADR 0174).

`Uvk5Radio` seeds `self._frequency` at connect from BK4819 registers 0x38/0x39 — the synthesiser
the **host** owns in dock mode — and until this cycle it adopted whatever came back. A register
file that answers 0 therefore produced a model saying *I know the frequency, it is 0*, which is not
the same statement as *I do not know*, and `_key_on`'s `if self._frequency is None` guard (ADR 0173)
read the first as the second's opposite and keyed. ADR 0173 measured exactly that — `POST /ptt`
**200** on a radio reporting 0 — and correctly declined to probe it, because the only way to learn
what the chip does at 0 Hz is to radiate.

**Nothing here keys a real radio, and nothing here needs to.** The firmware settles the question
from source: `Dock_ForceTx` (`App/app/uart.c:778`) never calls `BK4819_SetFrequency`, so the
synthesiser stays wherever the host put it, and the dock TX path validates no frequency anywhere.
The host's own `_correct_tx_band` then reads 0 as VHF (`_pa_gain_for`) and forces the VHF LNA bit
and gain byte. So the committed state is a PA rail up, a band path chosen for VHF, and a synthesiser
commanded to DC — an emission nobody chose, on a chip whose behaviour there is undocumented in both
trees. That uncertainty is the reason to refuse before the write, not a thing to characterise.

**The fix is upstream of the guard, and that is the argument.** The guard was never missing; the
model was lying to it. `_seed_frequency` refuses to adopt a read that is not a frequency and reports
`None`, which is what this backend already does for every other seeded value: `_seed_reg30` repairs
a word the radio cannot be receiving on (ADR 0132), the split is never seeded at all because
"believe whatever you find" is a named fault class (ADR 0133), and `_pa` reports no reading as no
reading (ADR 0134). `base.py:324` had already written the rule down — *0 Hz is not a frequency*.

Every fake below **pops 0x38/0x39 explicitly** rather than relying on their absence, so the same
test text is meaningful before and after this cycle seeds them in `FirmwareFakeSerial`.
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from radio_server.api import create_app
from radio_server.backends import RadioNotReady
from radio_server.backends.uvk5.radio import _REG30_TX_ENABLED
from radio_server.doctor import _uvk5_keying_core
from tests.test_uvk5_radio import a_frame, make_radio, reg_writes
from tests.test_uvk5_transport import FirmwareFakeSerial

TOKEN = "test-lan-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: A frequency a radio can really be on, used as the healthy read-back. Deliberately a value no
#: other test tunes to, so "it keyed on the seed" can never be confused with "it keyed on a tune".
HEALTHY_HZ = 147_390_000


def _regs(hz: int) -> dict[int, int]:
    """The 0x38/0x39 pair a synthesiser sitting on ``hz`` reads back (10 Hz units, low then high)."""
    freq10 = hz // 10
    return {0x38: freq10 & 0xFFFF, 0x39: (freq10 >> 16) & 0xFFFF}


def _radio(seed: dict[int, int] | None = None, **kwargs):
    """A real `Uvk5Radio` over the firmware fake, with the synthesiser registers set by the caller.

    ``seed=None`` models the radio this cycle is about: 0x38/0x39 absent, so the register file
    answers **0** — the same thing a chip that has never been tuned, or one found in the state
    `_seed_reg30` exists to repair, reads back.

    ``tx_allowed`` is forced on because `_key_on` checks the RF gate *before* the frequency: a
    receive-only node refusing for that reason would prove nothing about this guard.
    """
    fake = FirmwareFakeSerial()
    fake.registers.pop(0x38, None)
    fake.registers.pop(0x39, None)
    if seed:
        fake.registers.update(seed)
    kwargs.setdefault("tx_allowed", True)
    return make_radio(fake, **kwargs), fake


def _client(radio) -> TestClient:
    return TestClient(create_app(radio, api_token=TOKEN))


# --- the transmitter ---------------------------------------------------------------------------

def test_ptt_on_a_radio_that_does_not_know_its_frequency_is_refused():
    """The fail-first case. On master this is a **200**: the host keys, on 0 Hz, over HTTP."""
    radio, _fake = _radio()
    try:
        resp = _client(radio).post("/ptt", json={"on": True}, headers=AUTH)
        assert resp.status_code == 409
    finally:
        radio.close()


def test_the_refusal_reaches_no_register_and_leaves_nothing_keyed():
    """The claim that matters more than the status code: no TX-enable word ever went on the wire.

    A 409 that had already written `(0x30, 0xC1FE)` would be a report of an emission, not a
    refusal. Same shape as the baofeng rule that a refused setter writes nothing to the radio.
    """
    radio, fake = _radio()
    try:
        fake.writes.clear()
        assert _client(radio).post("/ptt", json={"on": True}, headers=AUTH).status_code == 409
        assert (0x30, _REG30_TX_ENABLED) not in reg_writes(fake)
        assert radio.status().transmitting is False
    finally:
        radio.close()


def test_transmit_is_refused_by_the_same_invariant():
    """`transmit()` self-keys through `_key_on`, so the guard has to hold on both doors.

    One-shot TX is the door the station ID, the TTS services and `POST /transmit` all use — the
    unattended ones. If only `ptt` were guarded, the automatic transmissions would be the ones
    that kept going out on an unknown frequency.
    """
    radio, _fake = _radio()
    try:
        with pytest.raises(RadioNotReady, match="frequency"):
            radio.transmit(a_frame())
    finally:
        radio.close()


def test_the_refusal_names_the_remedy():
    radio, _fake = _radio()
    try:
        body = _client(radio).post("/ptt", json={"on": True}, headers=AUTH).json()
        assert body["detail"] == "cannot key before a frequency is set"
    finally:
        radio.close()


# --- what the model says it knows --------------------------------------------------------------

def test_the_model_reports_unknown_rather_than_zero():
    """`frequency: null`, not `frequency: 0`.

    This is the whole difference between the two places the fix could have gone. A guard on `ptt`
    would have stopped the transmission and left `/status` claiming the station was tuned to 0 Hz
    for the life of the process — the same lie one layer up, in the field an operator reads.
    """
    radio, _fake = _radio()
    try:
        assert radio.status().frequency is None
    finally:
        radio.close()


@pytest.mark.parametrize(
    "hz, why",
    [
        (5_000_000, "below the 18 MHz floor"),
        (2_000_000_000, "above the 1.3 GHz ceiling"),
    ],
)
def test_a_read_back_outside_the_band_is_not_a_frequency_either(hz, why):
    """0 is the reachable case, not the whole class — the seed is validated, not compared to zero.

    Reusing `_validate_frequency` is what keeps one definition of "a frequency this radio can be
    on". The raster half of that check cannot fail here (the registers are *in* 10 Hz units, so the
    read is always on the raster), which is why the range half is the one with teeth.
    """
    radio, _fake = _radio(_regs(hz))
    try:
        assert radio.status().frequency is None, why
        assert _client(radio).post("/ptt", json={"on": True}, headers=AUTH).status_code == 409
    finally:
        radio.close()


def test_a_healthy_read_back_is_adopted_and_still_keys():
    """The cost of the fix, bounded: a radio that answers with its real VFO is unchanged.

    "Unknown" means *the read is not a frequency*, not *the host has not tuned yet*. A read-back is
    the radio's own VFO and is known enough to transmit on — the posture `_seed_reg30` already
    takes. Refusing until the host itself wrote 0x38/0x39 would refuse `doctor --key-test` on a
    perfectly healthy unconfigured radio, on no evidence that anything is wrong with it.
    """
    radio, fake = _radio(_regs(HEALTHY_HZ))
    client = _client(radio)
    try:
        assert radio.status().frequency == HEALTHY_HZ
        fake.writes.clear()
        assert client.post("/ptt", json={"on": True}, headers=AUTH).status_code == 200
        assert (0x30, _REG30_TX_ENABLED) in reg_writes(fake)
        assert client.post("/ptt", json={"on": False}, headers=AUTH).status_code == 200
    finally:
        radio.close()


def test_tuning_is_the_remedy_the_refusal_names():
    """A 409 whose remedy did not actually work would be worse than the 200 it replaces."""
    radio, _fake = _radio()
    client = _client(radio)
    try:
        assert client.post("/ptt", json={"on": True}, headers=AUTH).status_code == 409
        assert client.post("/frequency", json={"hz": HEALTHY_HZ}, headers=AUTH).status_code == 200
        assert client.post("/ptt", json={"on": True}, headers=AUTH).status_code == 200
        client.post("/ptt", json={"on": False}, headers=AUTH)
    finally:
        radio.close()


def test_the_split_guard_comes_alive_with_the_same_change():
    """ADR 0173 converted two dead guards. Fixing the seed is what makes *both* of them reachable.

    `set_split` refusing here is not incidental: the TX leg is the number that actually radiates,
    and arming an offset against a receive frequency the host does not know is how a stray digit
    becomes a repeater's uplink.
    """
    radio, _fake = _radio()
    try:
        resp = _client(radio).post("/split", json={"tx_hz": 147_990_000}, headers=AUTH)
        assert resp.status_code == 409
    finally:
        radio.close()


# --- the shipped path that actually had this bug ------------------------------------------------

def test_the_doctor_key_test_reports_a_refusal_instead_of_crashing(capsys):
    """`doctor --key-test` is where this was live: `_uvk5_config` keeps `frequency: None` when the
    REQUIRED `uvk5.frequency` is unset, which is the state you are in the first time you bring a
    radio up — so doctor built the backend, adopted the seed, and keyed.

    It catches `Uvk5KeyingError` only, so the fix would otherwise land as a traceback out of the
    one shipped tool that has the defect. A diagnostic that crashes tells the operator less than
    the wrong answer did.
    """
    radio, _fake = _radio()
    rc = _uvk5_keying_core(radio, seconds=0.0)
    out = capsys.readouterr().out
    assert rc == 1
    assert "REFUSED" in out and "TX confirmed" not in out
    assert "uvk5.frequency" in out
