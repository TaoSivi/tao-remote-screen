"""Relay server: agents (controlled PCs) and browser viewers meet here.

Run behind TLS (see README). Secrets come from the environment, never the command line.
  RS_VIEWER_USER      user name the person at the browser types
  RS_VIEWER_PASSWORD  password the person at the browser types
  RS_AGENT_TOKEN      token each agent sends (different from the viewer password)
"""
import argparse
import asyncio
import getpass
import hashlib
import hmac
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

HERE = Path(__file__).parent
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_FRAME = 4 * 1024 * 1024
MAX_FAILS, LOCK_SECONDS, SESSION_DAYS = 5, 300, 30
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def make_app(viewer_user: str, viewer_password: str, agent_token: str, allow_weak: bool = False) -> web.Application:
    if not ID_RE.match(viewer_user):
        raise SystemExit("RS_VIEWER_USER may only contain A-Z a-z 0-9 _ - (max 32)")
    if len(agent_token) < 12 or len(viewer_password) < (4 if allow_weak else 12):
        raise SystemExit("RS_VIEWER_PASSWORD and RS_AGENT_TOKEN must each be at least 12 characters")
    agents = {}  # id -> {"ws", "viewers": set, "screen": str|None}
    fails = {}   # ip -> [count, locked_until]

    # Login cookie: "<expiry>.<HMAC>", keyed from the viewer password, so changing the password signs everyone out.
    key = hmac.new(f"{viewer_user}\0{viewer_password}".encode(), b"rs-session-v1", hashlib.sha256).digest()

    def sign(exp):
        return hmac.new(key, str(exp).encode(), hashlib.sha256).hexdigest()

    def make_cookie(days):
        exp = int(time.time()) + days * 86400
        return f"{exp}.{sign(exp)}"

    def logged_in(req):
        try:
            exp, sig = req.cookies.get("rs_session", "").split(".", 1)
            return int(exp) > time.time() and hmac.compare_digest(sig, sign(int(exp)))
        except ValueError:
            return False

    def same_origin(req):  # blocks other websites from using the cookie
        origin = req.headers.get("Origin")
        return origin is None or urlparse(origin).netloc == req.host

    def locked(ip):
        f = fails.get(ip)
        return bool(f and f[1] > time.time())

    def fail(ip):
        f = fails.setdefault(ip, [0, 0.0])
        f[0] += 1
        if f[0] >= MAX_FAILS:
            f[0], f[1] = 0, time.time() + LOCK_SECONDS

    async def tell_agent(a):
        if not a["ws"].closed:
            await a["ws"].send_str(json.dumps({"type": "viewers", "n": len(a["viewers"])}))

    async def index(_):
        return web.Response(text=(HERE / "viewer.html").read_text(encoding="utf-8"), content_type="text/html")

    async def agent_ws(req):
        aid = req.query.get("id", "")
        token = req.headers.get("Authorization", "").removeprefix("Bearer ")
        if locked(req.remote) or not same(token, agent_token) or not ID_RE.match(aid):
            fail(req.remote)
            return web.Response(status=401)
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME, heartbeat=20)
        await ws.prepare(req)
        if aid in agents:
            await agents[aid]["ws"].close()
        a = agents[aid] = {"ws": ws, "viewers": set(), "screen": None}
        await tell_agent(a)
        try:
            async for m in ws:
                # shortcut: one slow viewer slows the others; upgrade to per-viewer queues if that matters
                if m.type == WSMsgType.BINARY:
                    for v in list(a["viewers"]):
                        await v.send_bytes(m.data)
                elif m.type == WSMsgType.TEXT and len(m.data) < 1024:
                    a["screen"] = m.data
                    for v in list(a["viewers"]):
                        await v.send_str(m.data)
        finally:
            if agents.get(aid) is a:
                del agents[aid]
            for v in list(a["viewers"]):
                await v.close()
        return ws

    async def login(req):
        if locked(req.remote):
            return web.Response(status=429)
        if not same_origin(req):
            return web.Response(status=403)
        try:
            data = await req.json()
            ok_user = isinstance(data["username"], str) and same(data["username"], viewer_user)
            ok_pass = isinstance(data["password"], str) and same(data["password"], viewer_password)
            good = ok_user and ok_pass
        except Exception:
            good = False
        if not good:
            fail(req.remote)
            await asyncio.sleep(1)
            return web.json_response({"ok": False}, status=401)
        remember = data.get("remember") is True
        resp = web.json_response({"ok": True})
        resp.set_cookie("rs_session", make_cookie(SESSION_DAYS if remember else 1), httponly=True, samesite="Strict",
                        max_age=SESSION_DAYS * 86400 if remember else None, path="/",
                        secure=req.secure or req.headers.get("X-Forwarded-Proto") == "https")
        return resp

    async def logout(req):
        resp = web.json_response({"ok": True})
        resp.del_cookie("rs_session", path="/")
        return resp

    async def session(req):
        return web.json_response({"ok": logged_in(req)})

    async def computers(req):
        if not logged_in(req):
            return web.Response(status=401)
        return web.json_response(sorted(agents))

    async def view_ws(req):
        if not same_origin(req):
            return web.Response(status=403)
        ws = web.WebSocketResponse(max_msg_size=4096, heartbeat=20)
        await ws.prepare(req)
        if not logged_in(req):
            await ws.send_json({"type": "error", "msg": "login required"})
            await ws.close()
            return ws
        try:
            aid = json.loads((await asyncio.wait_for(ws.receive(), 10)).data)["id"]
        except Exception:
            aid = None
        a = agents.get(aid) if isinstance(aid, str) else None
        if not a:
            await ws.send_json({"type": "error", "msg": "that computer is offline"})
            await ws.close()
            return ws
        a["viewers"].add(ws)
        await tell_agent(a)
        if a["screen"]:
            await ws.send_str(a["screen"])
        try:
            async for m in ws:
                if m.type == WSMsgType.TEXT and not a["ws"].closed:
                    await a["ws"].send_str(m.data)
        finally:
            a["viewers"].discard(ws)
            await tell_agent(a)
        return ws

    @web.middleware
    async def headers(req, handler):
        resp = await handler(req)
        resp.headers.update({"Content-Security-Policy": CSP, "X-Frame-Options": "DENY",
                             "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
        return resp

    app = web.Application(middlewares=[headers])
    app.add_routes([web.get("/", index), web.get("/ws/agent", agent_ws), web.get("/ws/view", view_ws),
                    web.post("/api/login", login), web.post("/api/logout", logout),
                    web.get("/api/session", session), web.get("/api/computers", computers)])
    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="use 127.0.0.1 and put a TLS proxy in front")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--allow-weak-password", action="store_true",
                    help="accept a short viewer password (4+ chars); only allowed when listening on 127.0.0.1 for local testing")
    args = ap.parse_args()
    if args.allow_weak_password:
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            sys.exit("--allow-weak-password is only allowed with --host 127.0.0.1")
        print("WARNING: weak viewer password allowed. Do NOT expose this server to the internet (no tunnel, no port forwarding).")
    viewer_user = os.environ.get("RS_VIEWER_USER") or input("Viewer user name: ").strip()
    viewer_pw = os.environ.get("RS_VIEWER_PASSWORD") or getpass.getpass("Viewer password (12+ chars, hidden): ")
    agent_tok = os.environ.get("RS_AGENT_TOKEN") or getpass.getpass("Agent token (12+ chars, different, hidden): ")
    web.run_app(make_app(viewer_user, viewer_pw, agent_tok, args.allow_weak_password), host=args.host, port=args.port)
