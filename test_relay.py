"""Self-check for the relay: python test_relay.py"""
import asyncio
import json

import aiohttp
from aiohttp import web

from server import make_app

USER, PW, TOKEN = "tester", "viewer-password-123", "agent-token-456789"


async def main():
    runner = web.AppRunner(make_app(USER, PW, TOKEN))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    jar = lambda: aiohttp.CookieJar(unsafe=True)  # aiohttp ignores cookies for bare IPs unless told otherwise
    async with aiohttp.ClientSession(cookie_jar=jar()) as s, aiohttp.ClientSession(cookie_jar=jar()) as anon:
        # agent with a wrong token is refused
        r = await s.get(f"http://{base}/ws/agent?id=pc1", headers={"Authorization": "Bearer nope"})
        assert r.status == 401, r.status

        agent = await s.ws_connect(f"http://{base}/ws/agent?id=pc1", headers={"Authorization": "Bearer " + TOKEN})
        assert json.loads((await agent.receive()).data) == {"type": "viewers", "n": 0}
        await agent.send_str(json.dumps({"type": "screen", "w": 10, "h": 5}))

        # not logged in: no computer list, no viewer socket
        assert (await anon.get(f"http://{base}/api/computers")).status == 401
        assert (await (await anon.get(f"http://{base}/api/session")).json())["ok"] is False
        w = await anon.ws_connect(f"http://{base}/ws/view")
        assert (await w.receive_json())["msg"] == "login required"

        # a forged cookie does not work
        forged = aiohttp.ClientSession(cookie_jar=jar(), cookies={"rs_session": "9999999999.deadbeef"})
        assert (await forged.get(f"http://{base}/api/computers")).status == 401
        await forged.close()

        # another website (Origin mismatch) cannot log in or open the viewer socket
        r = await anon.post(f"http://{base}/api/login", json={"username": USER, "password": PW}, headers={"Origin": "http://evil.example"})
        assert r.status == 403, r.status
        r = await anon.get(f"http://{base}/ws/view", headers={"Origin": "http://evil.example"})
        assert r.status == 403, r.status

        # login sets a cookie that works for the list and the socket
        r = await s.post(f"http://{base}/api/login", json={"username": USER, "password": PW, "remember": True})
        assert r.status == 200 and "rs_session" in r.cookies
        c = r.cookies["rs_session"]
        assert c["httponly"] and c["samesite"].lower() == "strict" and c["max-age"] == str(30 * 86400)
        assert (await (await s.get(f"http://{base}/api/computers")).json()) == ["pc1"]

        miss = await s.ws_connect(f"http://{base}/ws/view")
        await miss.send_json({"id": "nope"})
        assert "offline" in (await miss.receive_json())["msg"]

        v = await s.ws_connect(f"http://{base}/ws/view")
        await v.send_json({"id": "pc1"})
        assert json.loads((await agent.receive()).data) == {"type": "viewers", "n": 1}
        assert (await v.receive_json())["w"] == 10
        await agent.send_bytes(b"\xff\xd8frame")
        assert (await v.receive()).data == b"\xff\xd8frame"
        await v.send_json({"t": "move", "x": 0.5, "y": 0.5})
        assert json.loads((await agent.receive()).data)["t"] == "move"

        # oversized viewer message closes the connection
        await v.send_str("x" * 5000)
        assert (await v.receive()).type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING)

        # right password but wrong user name is refused (own sessions, so no lockout counted on s)
        async with aiohttp.ClientSession(cookie_jar=jar()) as other:
            assert (await other.post(f"http://{base}/api/login", json={"username": "other", "password": PW})).status == 401

        # logout clears the cookie
        await s.post(f"http://{base}/api/logout")
        assert (await (await s.get(f"http://{base}/api/session")).json())["ok"] is False

        # repeated wrong passwords lock the address out
        for _ in range(5):
            r = await anon.post(f"http://{base}/api/login", json={"username": USER, "password": "wrong"})
            assert r.status in (401, 429)
        assert (await anon.post(f"http://{base}/api/login", json={"username": USER, "password": PW})).status == 429
    await runner.cleanup()
    print("relay OK")


asyncio.run(main())


