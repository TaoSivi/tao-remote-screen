"""Agent: runs on the computer you want to control. Sends screen JPEGs, applies mouse/keyboard from the viewer.

Visible by design: prints a banner and every viewer connect/disconnect. Stop with Ctrl+C.
Token comes from the RS_AGENT_TOKEN environment variable.
"""
import argparse
import asyncio
import ctypes
import getpass
import io
import json
import os
import re
import sys
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import mss
import pyautogui
import pyperclip
import websockets
from PIL import Image, ImageChops
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0
if sys.platform == "win32":
    ctypes.windll.user32.SetProcessDPIAware()  # so mouse coordinates match the captured pixels

KEYS = {"Enter": "enter", "Backspace": "backspace", "Tab": "tab", "Escape": "esc", "ArrowUp": "up",
        "ArrowDown": "down", "ArrowLeft": "left", "ArrowRight": "right", "Delete": "delete", "Home": "home",
        "End": "end", "PageUp": "pageup", "PageDown": "pagedown", " ": "space", "Shift": "shift",
        "Control": "ctrl", "Alt": "alt", "Meta": "command" if sys.platform == "darwin" else "win",
        **{f"F{i}": f"f{i}" for i in range(1, 13)}}
BUTTONS = {"left", "right", "middle"}
PASTE = ("command", "v") if sys.platform == "darwin" else ("ctrl", "v")


def map_key(k):
    if k in KEYS:
        return KEYS[k]
    if isinstance(k, str) and len(k) == 1 and 32 <= ord(k) < 127:
        return k.lower() if k.isalpha() else k
    return None


class Screen:
    """Captures the screen and returns only what changed, at full resolution.

    A frame is a header (x, y, w, h as 4 little-endian uint16) plus a JPEG of that rectangle. Small changes (typing, cursor
    movement, menus) are tiny and fast; a big change (scrolling, video) is sent at lower quality, and once the screen has been
    still for 0.3 s the whole screen is sent again once, sharp. mss handles belong to the thread that made them, so call
    step() from one thread only.
    """

    def __init__(self, monitor, max_width, quality):
        self.monitor, self.max_width = monitor, max_width
        self.q_normal, self.q_rough, self.q_sharp = quality, max(40, quality - 25), min(95, quality + 7)
        self.local = threading.local()
        self.prev = None       # last picture as the raw B,G,R,A bytes read as RGBA: only compared, never shown
        self.prev_raw = None
        self.size = None
        self.changed_at = 0.0
        self.rough = False

    def step(self, force):
        """-> (monitor dict, (width, height), bytes | None); None means nothing changed."""
        if not hasattr(self.local, "sct"):
            self.local.sct = mss.MSS()
        mon = self.local.sct.monitors[self.monitor]
        shot = self.local.sct.grab(mon)
        raw, now = shot.bgra, time.monotonic()
        if not force and raw == self.prev_raw and not (self.rough and now - self.changed_at > 0.3):
            return mon, self.size, None      # identical bytes: skip all the image work
        self.prev_raw = raw
        cur = Image.frombytes("RGBA", shot.size, raw)
        if self.max_width and cur.width > self.max_width:
            cur = cur.resize((self.max_width, cur.height * self.max_width // cur.width), Image.BILINEAR)
        size = self.size = cur.size
        whole = (0, 0, *size)
        box, quality, subsampling = None, self.q_normal, 2
        if force or self.prev is None or self.prev.size != size:
            box, self.rough = whole, False
        else:
            diff = ImageChops.difference(self.prev, cur).getbbox(alpha_only=False)
            if diff:
                frac = (diff[2] - diff[0]) * (diff[3] - diff[1]) / (size[0] * size[1])
                box = whole if frac > 0.6 else diff
                if frac > 0.25:
                    quality, self.rough = self.q_rough, True
                self.changed_at = now
            elif self.rough and now - self.changed_at > 0.3:
                box, quality, subsampling, self.rough = whole, self.q_sharp, 0, False
        self.prev = cur
        if box is None:
            return mon, size, None
        buf = io.BytesIO()
        buf.write(struct.pack("<HHHH", box[0], box[1], box[2] - box[0], box[3] - box[1]))
        b, g, r, _ = cur.crop(box).split()   # the bytes are B,G,R,A; swap only the part that is sent
        Image.merge("RGB", (r, g, b)).save(buf, "JPEG", quality=quality, subsampling=subsampling)
        return mon, size, buf.getvalue()


class Agent:
    def __init__(self, args, token):
        self.args, self.token = args, token
        self.screen = Screen(args.monitor, args.max_width, args.quality)
        self.cap = ThreadPoolExecutor(1)
        self.inp = ThreadPoolExecutor(1)
        self.viewers = 0
        self.mon = None
        self.pressed = set()
        self.force = True
        self.move = None
        self.move_ev = None

    def apply(self, m):
        """Runs on the input thread. Unknown or malformed messages are ignored."""
        t = m.get("t")
        if t in ("move", "down", "up") and self.mon:
            x = self.mon["left"] + int(min(max(float(m["x"]), 0), 1) * (self.mon["width"] - 1))
            y = self.mon["top"] + int(min(max(float(m["y"]), 0), 1) * (self.mon["height"] - 1))
            pyautogui.moveTo(x, y, _pause=False)
            b = m.get("b", "left")
            if t != "move" and b in BUTTONS:
                (pyautogui.mouseDown if t == "down" else pyautogui.mouseUp)(button=b, _pause=False)
        elif t == "wheel":
            pyautogui.scroll(-int(max(-20, min(20, float(m["dy"])))) * 40, _pause=False)
        elif t == "key":
            k = map_key(m.get("k"))
            if k:
                if m.get("d"):
                    self.pressed.add(k)
                    pyautogui.keyDown(k, _pause=False)
                else:
                    self.pressed.discard(k)
                    pyautogui.keyUp(k, _pause=False)
        elif t == "text" and isinstance(m.get("s"), str) and len(m["s"]) <= 200:
            old = pyperclip.paste()  # paste via clipboard so Lao/Unicode text works; restore afterwards
            pyperclip.copy(m["s"])
            pyautogui.hotkey(*PASTE, _pause=False)
            time.sleep(0.15)
            pyperclip.copy(old)

    def release_all(self):
        for k in list(self.pressed):
            pyautogui.keyUp(k, _pause=False)
        self.pressed.clear()

    async def reader(self, ws):
        loop = asyncio.get_running_loop()
        async for raw in ws:
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if m.get("type") == "viewers":
                n = int(m["n"])
                if n != self.viewers:
                    print(f"[{'REMOTE VIEWER CONNECTED' if n > self.viewers else 'viewer left'}] viewers now: {n}")
                    self.viewers, self.force = n, True
                    if n == 0:
                        await loop.run_in_executor(self.inp, self.release_all)
            elif m.get("t") == "move":
                if not self.args.view_only:
                    self.move = m
                    self.move_ev.set()
            elif not self.args.view_only:
                try:
                    await loop.run_in_executor(self.inp, self.apply, m)
                except Exception as e:  # a bad input message must not kill the session
                    print("input error:", e)

    async def sender(self, ws):
        loop = asyncio.get_running_loop()
        size = None
        while True:
            t0 = time.monotonic()
            if self.viewers:
                force, self.force = self.force, False
                self.mon, sz, frame = await loop.run_in_executor(self.cap, self.screen.step, force)
                if sz != size:
                    size = sz
                    await ws.send(json.dumps({"type": "screen", "w": sz[0], "h": sz[1]}))
                if frame:
                    await ws.send(frame)
            # poll slowly while the screen is idle so the agent does not burn a CPU core
            idle = time.monotonic() - self.screen.changed_at > 1.0
            await asyncio.sleep(max(0.0, (0.1 if idle else 1 / self.args.fps) - (time.monotonic() - t0)))

    async def mover(self):
        """Mouse moves arrive faster than they can be applied; only the newest matters."""
        loop = asyncio.get_running_loop()
        while True:
            await self.move_ev.wait()
            self.move_ev.clear()
            await loop.run_in_executor(self.inp, self.apply, self.move)

    async def run(self):
        delay = 1
        while True:
            try:
                async with connect(self.args.server, additional_headers={"Authorization": "Bearer " + self.token},
                                   max_size=1 << 16, ping_interval=20) as ws:
                    delay = 1
                    print(f"Connected to {self.args.server} as '{self.args.id}'")
                    self.move_ev = asyncio.Event()
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self.reader(ws))
                        tg.create_task(self.sender(ws))
                        tg.create_task(self.mover())
            except* InvalidStatus as eg:
                if eg.exceptions[0].response.status_code in (401, 429):
                    sys.exit("Server refused the token or id (401/429). Check RS_AGENT_TOKEN and --id.")
            except* (OSError, websockets.ConnectionClosed, asyncio.TimeoutError) as eg:
                print("disconnected:", eg.exceptions[0])
            self.viewers = 0
            self.release_all()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


CONFIG = Path(os.environ.get("APPDATA") or Path.home()) / "RemoteScreen" / "config.json"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def set_autostart(on):
    """Windows only: start the agent at logon through the per-user Run key (visible console window)."""
    import winreg
    cmd = f'"{sys.executable}"' if getattr(sys, "frozen", False) else f'"{sys.executable}" "{Path(__file__).resolve()}"'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "RemoteScreenAgent", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, "RemoteScreenAgent")
            except FileNotFoundError:
                pass
    print("Auto-start", "ON: the agent will start when you log in." if on else "OFF.")


def load_settings(args):
    """Command line > environment > saved file > ask once (and offer to save)."""
    saved = json.loads(CONFIG.read_text()) if CONFIG.exists() and not args.reset else {}
    asked = False
    server = args.server or saved.get("server")
    if not server:
        server, asked = input("Server address (e.g. wss://remote.example.com): ").strip(), True
    pc_id = args.id or saved.get("id") or os.environ.get("COMPUTERNAME", "pc")
    if not saved.get("id") and not args.id:
        pc_id, asked = input(f"Name for this computer [{pc_id}]: ").strip() or pc_id, True
    token = os.environ.get("RS_AGENT_TOKEN") or saved.get("token")
    if not token:
        token, asked = getpass.getpass("Agent token (hidden): ").strip(), True
    if len(token) < 12 or not ID_RE.match(pc_id) or not re.match(r"^wss?://", server):
        sys.exit("Invalid settings: token needs 12+ characters, name only A-Z a-z 0-9 _ -, address must start with ws:// or wss://")
    if asked and input("Remember these settings on this computer? [y/N] ").lower().startswith("y"):
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps({"server": server, "id": pc_id, "token": token}))
        print(f"Saved to {CONFIG} (readable only by your Windows account; delete it or use --reset to forget).")
    url = server if "/ws/agent" in server else f"{server.rstrip('/')}/ws/agent?id={pc_id}"
    return url, pc_id, token


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", help="ws://host:port or wss://host (asked on first run if missing)")
    ap.add_argument("--id", help="name shown to viewers (A-Z a-z 0-9 _ -)")
    ap.add_argument("--fps", type=float, default=30, help="upper limit while the screen is changing")
    ap.add_argument("--quality", type=int, default=85, help="JPEG quality of changed areas (sharp refresh uses +7)")
    ap.add_argument("--max-width", type=int, default=0, help="0 = native resolution; set e.g. 1600 to shrink on slow links")
    ap.add_argument("--monitor", type=int, default=1, help="1 = primary")
    ap.add_argument("--view-only", action="store_true", help="show the screen but ignore mouse and keyboard")
    ap.add_argument("--autostart", choices=["on", "off"], help="start at Windows logon (on) or stop doing that (off)")
    ap.add_argument("--reset", action="store_true", help="ignore and replace the saved settings")
    args = ap.parse_args()
    if args.autostart:
        return set_autostart(args.autostart == "on")
    args.server, args.id, token = load_settings(args)
    host = urlparse(args.server)
    if host.scheme == "ws" and host.hostname not in ("localhost", "127.0.0.1", "::1"):
        print("WARNING: ws:// sends your token and screen unencrypted. Use wss:// over the internet.")
    print("=" * 60)
    print(f" REMOTE CONTROL AGENT ON  ({'view only' if args.view_only else 'mouse + keyboard allowed'})")
    print(" Anyone with the viewer password can see and control this screen.")
    print(" Press Ctrl+C to stop.")
    print("=" * 60)
    try:
        asyncio.run(Agent(args, token).run())
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if isinstance(e.code, str) and getattr(sys, "frozen", False):  # keep a double-clicked window open long enough to read
            print(e.code)
            input("Press Enter to close")
            sys.exit(1)
        raise
