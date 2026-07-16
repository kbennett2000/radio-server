// A banner shown when the page is NOT a secure context (ADR 0039). Browsers only expose the
// microphone (getUserMedia) and AudioWorklet — i.e. Talk and Listen — over HTTPS or on localhost.
// A phone loading http://<lan-ip>:8000 can log in but silently cannot hear or transmit; without
// this banner the failure is invisible (RX errors only to the console; TX shows a misleading "mic
// denied"). Renders nothing in a secure context, so the PC (localhost) and any HTTPS deploy see it
// never.

export default function SecureContextNotice() {
  if (typeof window === "undefined" || window.isSecureContext) return null;
  const host = window.location.host;
  return (
    <div className="notice secure-notice" role="alert">
      <strong>Listen and Talk are disabled — this page isn’t secure.</strong> Your browser only
      allows microphone and audio playback over HTTPS (or on <code>localhost</code>). You’re on{" "}
      <code>http://{host}</code>, so audio can’t start. Load this page over{" "}
      <strong>https://{host}</strong> — see the HTTPS setup in the deployment docs — to enable them.
    </div>
  );
}
