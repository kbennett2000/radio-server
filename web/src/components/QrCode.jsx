// A thin wrapper over qrcode.react's SVG renderer (ADR 0027). Used to render the TOTP
// `otpauth://` provisioning URI as a scannable code for phone re-enrollment. qrcode.react is a
// zero-dependency MIT React component; SVG (not canvas) so it stays crisp and needs no ref.

import { QRCodeSVG } from "qrcode.react";

export default function QrCode({ value, size = 176 }) {
  // Light quiet-zone background so the code scans against the dark theme; margin included.
  return (
    <QRCodeSVG
      value={value}
      size={size}
      marginSize={2}
      bgColor="#ffffff"
      fgColor="#000000"
      level="M"
    />
  );
}
