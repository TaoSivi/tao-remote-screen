# Tao Remote Screen

A small self-hosted remote-screen tool for **your own computers**: a browser (Windows, Mac, Linux, Android, iPhone) shows and controls the screen of a PC that runs the agent, through a relay server you run yourself.

```
Browser  <-->  server.py (relay, you host it)  <-->  agent.py (on the PC you want to control)
```

> **Read this first.** This is a hobby-grade tool, not a packaged product like AnyDesk.
> - It needs a server you run (an always-on PC or VPS) **and** HTTPS in front of it (Tailscale `serve`, Cloudflare Tunnel or Caddy). There is no ID-and-password pairing.
> - The `.exe` files in Releases are **not code-signed**. Windows (SmartScreen / Smart App Control / Application Control) may warn or block them. The Python scripts work as a fallback.
> - If you only want "install, get an ID, connect", use [RustDesk](https://rustdesk.com) instead.
> - Only use it on computers you own or have permission to control. The agent prints a banner and every viewer connect/disconnect, and never starts by itself unless you opt in.

## What it does
- Full-resolution screen, sends only the changed part, sharp refresh when the screen is still, up to ~15-20 fps on a 1080p screen.
- Mouse, keyboard, wheel, Unicode text (Lao works) through the browser; touch gestures on phones (tap, long-press = right click, two-finger scroll).
- Fullscreen viewer with a hidden toolbar (pause, fullscreen, fit/stretch, keyboard text, special keys, exit).
- Login with user name and password, signed HttpOnly cookie for "remember me" (30 days), lockout after 5 wrong tries.

## Limits
Windows UAC prompts and the lock screen cannot be seen or controlled; no audio; no file transfer; one shared viewer account; a viewer on the same computer as the agent sees a mirror-in-mirror picture.

## Install (Python route)
```
pip install -r requirements.txt
```
**Server** (always-on machine). Secrets come from the environment; passwords need 12+ characters:
```
set RS_VIEWER_USER=you
set RS_VIEWER_PASSWORD=a-long-password-here
set RS_AGENT_TOKEN=a-different-long-token
python server.py            # listens on 127.0.0.1:8765
```
Put HTTPS in front of it. Easiest private option: install [Tailscale](https://tailscale.com) on all your devices, enable HTTPS certificates in its admin console, then run `tailscale serve --bg 8765`; use the `https://<machine>.<tailnet>.ts.net` address.

**Agent** (the PC to control):
```
set RS_AGENT_TOKEN=a-different-long-token
python agent.py --server wss://<your-address> --id office-pc
```
(`--view-only` to watch without control; `--autostart on` on Windows to start at logon.)

**Viewer:** open `https://<your-address>/`, sign in, pick the computer.

## .exe route (Windows, unsigned)
The [`downloads/`](downloads) folder holds `RemoteScreenServer.exe` and `RemoteScreenAgent.exe` (built with PyInstaller from this source) and `SHA256SUMS.txt`. Download a file with the "Download raw file" button, then check its hash (`Get-FileHash file.exe`) against `SHA256SUMS.txt` before running. They ask for the same settings on first start. `install-server.ps1` can register the server to start at logon.

## Security notes
- Anyone with the viewer user name and password controls every registered computer. Use a long password; keep the server private (Tailscale) or behind HTTPS.
- Behind a proxy every client looks like the proxy's address, so the lockout counts all clients together.
- `--allow-weak-password` exists only for local testing and refuses to start unless the server listens on 127.0.0.1.
- Tests: `python test_relay.py`, `python test_agent.py`.

More (Lao): see `ຄູ່ມື-ຕິດຕັ້ງ.md`.
