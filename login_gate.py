"""Login gate for the Vercel deploy of Edge GEX Lab (pure ASGI, no extra dependencies).

Every request needs a signed session cookie except /login and /logout.
Credentials come from Vercel Environment Variables, never from this public repo:
  GEXLAB_LOGIN_USER, GEXLAB_LOGIN_PASSWORD
If either is missing, nobody can log in (fails closed).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
import time
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, quote

COOKIE = "gexlab_session"
TTL_S = 7 * 24 * 3600
MAX_BODY = 10_000


def _creds() -> tuple[str, str] | None:
    u = os.getenv("GEXLAB_LOGIN_USER", "")
    p = os.getenv("GEXLAB_LOGIN_PASSWORD", "")
    return (u, p) if u and p else None


def _key(c: tuple[str, str]) -> bytes:
    # Changing the user or password invalidates every existing session.
    return hashlib.sha256(f"gexlab-session|{c[0]}|{c[1]}".encode()).digest()


def make_token(c: tuple[str, str], exp: int) -> str:
    sig = hmac.new(_key(c), f"{c[0]}|{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def token_ok(value: str, c: tuple[str, str]) -> bool:
    try:
        exp = int(value.split(".", 1)[0])
    except ValueError:
        return False
    return exp > time.time() and hmac.compare_digest(make_token(c, exp), value)


def _safe_next(n: str) -> str:
    return n if n.startswith("/") and not n.startswith("//") and "\\" not in n else "/"


def _page(nxt: str, error: str = "") -> bytes:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · Edge GEX Lab</title>
<style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d0f12;color:#e6e8eb;
font:15px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}}
form{{width:300px;padding:28px;border:1px solid #23272e;border-radius:10px;background:#14171c}}
h1{{font-size:18px;margin:0 0 18px}} label{{display:block;font-size:13px;color:#9aa1ab;margin:12px 0 4px}}
input{{width:100%;box-sizing:border-box;padding:9px 10px;border:1px solid #2c313a;border-radius:6px;
background:#0d0f12;color:#e6e8eb;font-size:15px}} input:focus{{outline:none;border-color:#4fae5a}}
button{{width:100%;margin-top:20px;padding:10px;border:0;border-radius:6px;background:#3d8f47;color:#fff;
font-size:15px;cursor:pointer}} button:hover{{background:#4fae5a}} .err{{color:#ef6b6b;font-size:13px;margin:12px 0 0}}
</style></head><body>
<form method="post" action="/login">
<h1>Edge GEX Lab</h1>
<input type="hidden" name="next" value="{html.escape(nxt)}">
<label for="u">Username</label><input id="u" name="username" autocomplete="username" required autofocus>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
{err}<button type="submit">Sign in</button>
</form></body></html>""".encode()


async def _send(send, status: int, body: bytes = b"", headers: list[tuple[str, str]] | None = None,
                ctype: str = "text/html; charset=utf-8") -> None:
    hs = [(b"content-type", ctype.encode()), (b"cache-control", b"no-store")]
    hs += [(k.encode(), v.encode()) for k, v in (headers or [])]
    await send({"type": "http.response.start", "status": status, "headers": hs})
    await send({"type": "http.response.body", "body": body})


async def _read_body(receive) -> bytes:
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if len(body) > MAX_BODY or not msg.get("more_body"):
            return body[:MAX_BODY]


def _cookie(scope) -> str:
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            c = SimpleCookie()
            try:
                c.load(v.decode("latin-1"))
            except Exception:  # noqa: BLE001
                return ""
            if COOKIE in c:
                return c[COOKIE].value
    return ""


class LoginGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "/")
        creds = _creds()

        if path == "/logout":
            return await _send(send, 303, headers=[
                ("location", "/login"),
                ("set-cookie", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax")])

        if path == "/login":
            if scope.get("method") == "POST":
                f = parse_qs((await _read_body(receive)).decode("utf-8", "replace"))
                user, pw = f.get("username", [""])[0], f.get("password", [""])[0]
                nxt = _safe_next(f.get("next", ["/"])[0])
                exp_u, exp_p = creds or ("\0", "\0")
                ok_u = hmac.compare_digest(user.encode(), exp_u.encode())
                ok_p = hmac.compare_digest(pw.encode(), exp_p.encode())
                if creds and ok_u and ok_p:
                    tok = make_token(creds, int(time.time()) + TTL_S)
                    return await _send(send, 303, headers=[
                        ("location", nxt),
                        ("set-cookie", f"{COOKIE}={tok}; Path=/; Max-Age={TTL_S}; HttpOnly; Secure; SameSite=Lax")])
                await asyncio.sleep(1)  # slow down guessing
                msg = "Wrong username or password." if creds else "Login is not configured on the server yet."
                return await _send(send, 401, _page(nxt, msg))
            q = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            return await _send(send, 200, _page(_safe_next(q.get("next", ["/"])[0])))

        if creds and token_ok(_cookie(scope), creds):
            return await self.app(scope, receive, send)

        if path.startswith("/api/"):
            return await _send(send, 401, json.dumps({"detail": "Not signed in"}).encode(),
                               ctype="application/json")
        qs = scope.get("query_string", b"").decode("latin-1")
        target = path + ("?" + qs if qs else "")
        return await _send(send, 303, headers=[("location", "/login?next=" + quote(target, safe=""))])
