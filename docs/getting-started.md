# Try it first — no radio needed

The easiest way to see what radio-server does is to run it on just a computer, with **no radio
connected at all**. It comes with a built-in *practice radio* — a pretend radio that lets you open
the control panel and click around safely. Nothing transmits. This is the best way to get a feel for
it before you connect any real equipment. And here's where it leads: once you do connect a radio,
keying **10#** on it links your handheld to a voice channel on the internet — and you hear your own
voice come back through the Mumble app on your phone. It ships already pointed at a demo server, so it
works on the first try.

This takes about 15 minutes, and works the same on Windows, macOS, and Linux.

> **In a hurry? There's a one-line shortcut.** If you'd rather not do the steps below by hand, one
> command does all of them for you and prints your password at the end:
>
> **macOS / Linux**
> ```sh
> curl -LsSf https://raw.githubusercontent.com/kbennett2000/radio-server/master/scripts/install.sh | sh
> ```
> **Windows (PowerShell)**
> ```powershell
> irm https://raw.githubusercontent.com/kbennett2000/radio-server/master/scripts/install.ps1 | iex
> ```
>
> When it's done, jump to [Step 5](#step-5--open-the-control-panel). The walk-through below is the
> same thing, one step at a time — worth reading if you'd like to understand what the shortcut did,
> or if a step ever needs fixing.

> **Feeling wary of the command line?** That's completely normal. There are a few lines below that
> you copy and paste — you don't need to understand them. Each one has a plain note saying what it
> does. If a step doesn't work, nothing is broken; you can close the window and start again.

---

## What you'll need

Three small, free tools. You install each one once:

- **Python** — the language radio-server is written in. Download it from
  [python.org/downloads](https://www.python.org/downloads/) (version 3.11 or newer). On the Windows
  installer, tick **"Add Python to PATH"** when asked.
- **uv** — a little helper that fetches everything else radio-server needs, so you don't have to
  chase down pieces yourself. Install instructions:
  [the uv website](https://docs.astral.sh/uv/getting-started/installation/).
- **Node.js** — another free tool, used only to build the web page (the control panel you'll open in
  your browser). Download it from [nodejs.org](https://nodejs.org/) (the "LTS" version).

You'll also need the radio-server files themselves. If you know how to use Git, clone the repository.
If not, use the green **"Code"** button on the project's GitHub page and choose **"Download ZIP,"**
then unzip it somewhere easy to find, like your Desktop.

---

## Step 1 — Open a terminal in the project folder

A *terminal* is just a window where you type commands.

- **Windows:** open the folder in File Explorer, click the address bar, type `powershell`, and press
  Enter.
- **macOS:** open the **Terminal** app, type `cd ` (with a space), drag the project folder onto the
  window, and press Enter.
- **Linux:** open your terminal and `cd` into the project folder.

Everything below is typed into this window.

## Step 2 — Let uv gather the pieces

```sh
uv sync --extra mumble
```

This downloads the parts radio-server needs and sets them up — including the Mumble voice link, so the
browser Connect button works. It runs for a minute or two the first time, then it's done. You only do
this once.

## Step 3 — Build the control panel

```sh
cd web
npm install
npm run build
cd ..
```

This builds the web page you'll open in your browser. Like the step above, it takes a minute the
first time and only needs doing once.

## Step 4 — Start it

You give the control panel a simple password (it's called a *token*), then start the program. Use any
word you like in place of `my-password`.

**Windows (PowerShell):**

```powershell
$env:RADIO_API_TOKEN = "my-password"
uv run python -m radio_server
```

**macOS / Linux:**

```sh
RADIO_API_TOKEN=my-password uv run python -m radio_server
```

You'll see a line saying it's running at `http://127.0.0.1:8000`. Leave this window open — that's the
program running. (To stop it later, come back to this window and press **Ctrl+C**.)

## Step 5 — Open the control panel

Open your web browser and go to:

```
http://127.0.0.1:8000
```

Type in the password you chose (`my-password`) and you're in. You'll see the control panel, with live
status from the practice radio. Have a look around — you can click **Monitor**, watch the status, and
explore the tabs. Because this is the pretend radio, it's all completely safe: nothing is being
transmitted.

---

## That worked — what now?

- **See what each control does** → [Using your station](using-it.md).
- **Ready to connect a real radio?** → [Setting it up with your radio](install.md). That's when the
  fun part unlocks: key **10#** on your handheld and it links to a voice channel on the internet —
  hear yourself come out of the Mumble app on your phone (the demo server comes configured, nothing
  extra to set up).
- **Letting people call in over the air?** They log in with a rolling code from a phone app — a quick
  one-time setup (install an app, scan a QR, restart), walked through in
  [Setting it up with your radio](install.md#set-your-callsign-and-login-code).
- **Want to change settings?** You can do most of it right in the browser — see
  [Changing the settings](configuration.md).

Nothing you did here touches a real radio or transmits anything. When you're ready for the real
thing, the setup guide picks up from here.
