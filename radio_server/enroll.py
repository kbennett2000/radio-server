"""``python -m radio_server.enroll`` — mint a TOTP secret and enroll Google Authenticator.

Over-RF callers authenticate with a time-based one-time code (TOTP), so the station needs a shared
secret that also lives in the operator's authenticator app. This CLI mints that secret (writing it to
the ``0600`` secrets file the server reads), prints the base32 secret and the ``otpauth://``
provisioning URI, and — when the optional ``qrcode`` package is installed — renders a scannable QR
right in the terminal. Nothing here transmits or touches the radio.

The secret lives on the separate secrets channel (ADR 0025), never in ``radio.toml``. Re-enrolling
mints a NEW secret and invalidates the one already in your phone, so an existing secret is refused
unless ``--force``. After enrolling, also set ``station.callsign`` (and a ``tts.voice``) in
``radio.toml`` and restart the server — the live controller only wires up when the TOTP secret is
present.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path
from typing import Mapping, TextIO

from .auth.totp import TotpVerifier
from .config.secrets import DEFAULT_SECRETS_PATH, load_secrets, rotate


def _render_qr(uri: str) -> str | None:
    """Return a terminal-drawable QR for ``uri``, or ``None`` if the optional ``qrcode`` dep is absent.

    ``qrcode`` is a pure-Python, zero-runtime-dependency package (no Pillow needed for ASCII output),
    carried by the ``hardware`` extra. When it isn't installed the caller falls back to the URI text.
    """
    try:
        import qrcode  # optional; part of the 'hardware' extra
    except ImportError:
        return None
    qr = qrcode.QRCode(border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    buf = io.StringIO()
    # invert=True renders dark modules on a light field — the right contrast for a dark terminal, so a
    # phone camera reads it. The URI/secret are always printed too, in case a scan doesn't take.
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()


def enroll(
    secrets_path: str | Path,
    account: str,
    *,
    force: bool = False,
    out: TextIO = sys.stdout,
    env: Mapping[str, str] | None = None,
) -> int:
    """Mint + persist a TOTP secret and print enrollment instructions. Returns a process exit code.

    ``env`` defaults to the real environment (so an existing ``RADIO_TOTP_SECRET`` is respected);
    tests pass ``{}`` to isolate from the ambient environment.
    """
    import os

    path = Path(secrets_path)
    existing = load_secrets(path, env=os.environ if env is None else env)
    if existing.totp_secret and not force:
        print(
            f"A TOTP secret is already configured (in {path} or the environment). Re-enrolling mints a "
            f"NEW secret and invalidates the one in your authenticator app. Re-run with --force to "
            f"replace it.",
            file=out,
        )
        return 1

    secret = rotate(path, "totp_secret")
    uri = TotpVerifier(secret).provisioning_uri(account)

    print(f"TOTP secret minted and written to {path} (chmod 600).", file=out)
    print("", file=out)

    qr = _render_qr(uri)
    if qr is not None:
        print("Scan this QR in Google Authenticator (or any TOTP app):", file=out)
        print("", file=out)
        print(qr, file=out)
    else:
        print(
            "Install the 'qrcode' package (pip install 'radio-server[hardware]') to render a scannable "
            "QR here. For now, either type the base32 secret into the app manually or paste the "
            "provisioning URI into any QR generator.",
            file=out,
        )
        print("", file=out)

    print(f"  Secret (base32, for manual entry): {secret}", file=out)
    print(f"  Provisioning URI:                  {uri}", file=out)
    print("", file=out)
    print(
        "It is time-based (TOTP), 30-second period, 6 digits. Next steps: set station.callsign (and a "
        "tts.voice) in radio.toml, then restart the server — the controller wires up only once the "
        "TOTP secret is present. On the radio, key <6-digit code># to log in, then 1# to announce the "
        "time.",
        file=out,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m radio_server.enroll",
        description="Mint a TOTP secret and enroll Google Authenticator (no radio, never transmits).",
    )
    parser.add_argument(
        "--secrets",
        default=str(DEFAULT_SECRETS_PATH),
        help=f"secrets file to write (default: {DEFAULT_SECRETS_PATH})",
    )
    parser.add_argument(
        "--account",
        default="radio-server",
        help="account label shown in the authenticator app (default: radio-server)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing TOTP secret (this invalidates the current phone enrollment)",
    )
    args = parser.parse_args(argv)
    return enroll(args.secrets, args.account, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
