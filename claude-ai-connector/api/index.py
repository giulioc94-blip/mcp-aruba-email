"""Connettore Aruba Studio: server MCP remoto con login OAuth.

Quando si aggiunge il connettore su claude.ai, la pagina /authorize chiede email e password
delle caselle Aruba, le verifica con un login IMAP e le mette, cifrate con AES-GCM, dentro il
token che claude.ai conserva. Il server non salva niente: ogni richiesta decifra il token.
"""
import base64
import hashlib
import html
import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlencode, urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, os.path.dirname(__file__))
import aruba_tools  # noqa: E402

_SECRET = os.environ.get("SECRET_KEY", "")
if len(_SECRET) < 32:
    raise RuntimeError("SECRET_KEY mancante o troppo corta: servono almeno 32 caratteri casuali")
KEY = hashlib.sha256(_SECRET.encode()).digest()
CODE_TTL = 600
ACCESS_TTL = 3600
REFRESH_TTL = 365 * 24 * 3600
ALLOWED_REDIRECT_HOSTS = {"claude.ai", "claude.com", "localhost", "127.0.0.1"}


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def seal(kind, data, ttl):
    """Cifra un oggetto con scadenza; kind impedisce di usare un codice come token e viceversa."""
    nonce = secrets.token_bytes(12)
    payload = json.dumps({"k": kind, "exp": int(time.time()) + ttl, "d": data}).encode()
    return _b64(nonce + AESGCM(KEY).encrypt(nonce, payload, kind.encode()))


def unseal(kind, token):
    try:
        raw = _unb64(token)
        obj = json.loads(AESGCM(KEY).decrypt(raw[:12], raw[12:], kind.encode()))
    except Exception:
        return None
    if obj.get("k") != kind or obj.get("exp", 0) < time.time():
        return None
    return obj["d"]


def _redirect_ok(uri):
    try:
        u = urlparse(uri)
    except Exception:
        return False
    host = (u.hostname or "").lower()
    if host in ("localhost", "127.0.0.1"):
        return u.scheme in ("http", "https")
    return u.scheme == "https" and host in ALLOWED_REDIRECT_HOSTS


PAGE = """<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Collega le caselle Aruba</title>
<style>
:root{{--bg:#f6f4f1;--card:#fff;--ink:#1f1b16;--muted:#6b6259;--line:#ddd6cc;--accent:#5b2a86;--err:#a1261b}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16130f;--card:#211d18;--ink:#f1ece6;--muted:#a89f95;--line:#3a332b;--accent:#b48be0;--err:#ff8a7a}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,system-ui,Segoe UI,sans-serif}}
main{{max-width:460px;margin:40px auto;padding:0 16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px}}
h1{{font-size:20px;margin:0 0 6px}} p{{color:var(--muted);margin:0 0 18px}}
fieldset{{border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:0 0 14px}}
legend{{font-size:13px;color:var(--muted);padding:0 4px}}
label{{display:block;font-size:14px;margin:8px 0 4px}}
input{{width:100%;box-sizing:border-box;padding:10px;border:1px solid var(--line);border-radius:6px;background:transparent;color:var(--ink);font-size:16px}}
button{{width:100%;padding:12px;border:0;border-radius:8px;background:var(--accent);color:#fff;font-size:16px;cursor:pointer}}
.err{{color:var(--err);margin:0 0 14px}} small{{color:var(--muted);display:block;margin-top:14px}}
</style></head><body><main><div class="card">
<h1>Collega le caselle Aruba</h1>
<p>Inserisci le caselle Aruba da collegare. Claude potrà leggere, inviare, rispondere, spostare ed eliminare email, chiedendoti conferma prima di inviare o eliminare.</p>
{error}
<form method="post" action="/authorize">
<input type="hidden" name="req" value="{req}">
{boxes}
<button type="submit">Verifica e collega</button>
</form>
<small>Le password vengono verificate con Aruba e conservate solo in forma cifrata nella connessione di Claude.</small>
</div></main></body></html>"""


def _boxes(values):
    out = []
    for i in range(3):
        email_v = (values.get(f"email{i}") or [""])[0]
        title = "Casella principale" if i == 0 else "Altra casella (facoltativa)"
        req = "required" if i == 0 else ""
        out.append(
            f'<fieldset><legend>{title}</legend>'
            f'<label for="e{i}">Email</label><input id="e{i}" name="email{i}" type="email" autocomplete="username" '
            f'value="{html.escape(email_v)}" {req} placeholder="nome@studiocurcioterremoto.it">'
            f'<label for="p{i}">Password</label><input id="p{i}" name="password{i}" type="password" autocomplete="current-password" {req}>'
            f"</fieldset>"
        )
    return "".join(out)


class handler(BaseHTTPRequestHandler):
    def _base(self):
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host")
        return f"https://{host}"

    def _send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        if body:
            self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Mcp-Protocol-Version, Mcp-Session-Id")
        self.send_header("Access-Control-Expose-Headers", "WWW-Authenticate")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _form(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n).decode() if n else ""
        if "json" in (self.headers.get("Content-Type") or ""):
            try:
                return {k: [v] if not isinstance(v, list) else v for k, v in json.loads(raw or "{}").items()}
            except Exception:
                return {}
        return parse_qs(raw, keep_blank_values=True)

    def _path(self):
        return urlparse(self.path).path.rstrip("/") or "/"

    def do_OPTIONS(self):
        self._send(204)

    def do_GET(self):
        p = self._path()
        base = self._base()
        if p.startswith("/.well-known/oauth-protected-resource"):
            return self._send(200, {"resource": f"{base}/mcp", "authorization_servers": [base],
                                    "bearer_methods_supported": ["header"]})
        if p.startswith("/.well-known/oauth-authorization-server") or p.startswith("/.well-known/openid-configuration"):
            return self._send(200, {
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "registration_endpoint": f"{base}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": ["email"],
            })
        if p == "/authorize":
            q = parse_qs(urlparse(self.path).query)
            g = lambda k: (q.get(k) or [""])[0]
            if g("response_type") != "code" or not _redirect_ok(g("redirect_uri")) or g("code_challenge_method") != "S256" or not g("code_challenge"):
                return self._send(400, "Richiesta di autorizzazione non valida.", "text/plain; charset=utf-8")
            req = seal("req", {"r": g("redirect_uri"), "s": g("state"), "c": g("code_challenge"), "i": g("client_id")}, 1800)
            return self._send(200, PAGE.format(req=req, boxes=_boxes({}), error=""), "text/html; charset=utf-8")
        if p == "/":
            return self._send(200, {"name": "Aruba Studio", "mcp": f"{base}/mcp"})
        if p in ("/mcp", "/api/mcp"):
            return self._send(405, {"error": "Usa POST"})
        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        self._send(405, {"error": "Senza stato"})

    def do_POST(self):
        p = self._path()
        if p == "/register":
            data = self._form()
            uris = data.get("redirect_uris") or []
            if not uris or not all(_redirect_ok(u) for u in uris):
                return self._send(400, {"error": "invalid_redirect_uri"})
            return self._send(201, {
                "client_id": "c_" + secrets.token_urlsafe(16),
                "client_id_issued_at": int(time.time()),
                "redirect_uris": uris,
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "client_name": (data.get("client_name") or ["Claude"])[0],
            })
        if p == "/authorize":
            return self._authorize_post()
        if p == "/token":
            return self._token()
        if p in ("/mcp", "/api/mcp"):
            return self._mcp()
        return self._send(404, {"error": "not found"})

    def _authorize_post(self):
        f = self._form()
        req = unseal("req", (f.get("req") or [""])[0])
        if not req:
            return self._send(400, "La richiesta è scaduta: riprova ad aggiungere il connettore.", "text/plain; charset=utf-8")
        boxes, errors = {}, []
        for i in range(3):
            user = (f.get(f"email{i}") or [""])[0].strip().lower()
            pwd = (f.get(f"password{i}") or [""])[0]
            if not user and not pwd:
                continue
            if not user or not pwd:
                errors.append(f"Casella {i + 1}: inserisci sia email sia password.")
                continue
            err = aruba_tools.check_login(user, pwd)
            if err:
                errors.append(f"{html.escape(user)}: {html.escape(err)}.")
            else:
                boxes[user] = pwd
        if errors or not boxes:
            msg = "<p class='err'>" + "<br>".join(errors or ["Inserisci almeno una casella."]) + "</p>"
            return self._send(200, PAGE.format(req=html.escape((f.get("req") or [""])[0]), boxes=_boxes(f), error=msg), "text/html; charset=utf-8")
        code = seal("code", {"b": boxes, "r": req["r"], "c": req["c"]}, CODE_TTL)
        sep = "&" if "?" in req["r"] else "?"
        loc = req["r"] + sep + urlencode({"code": code, **({"state": req["s"]} if req["s"] else {})})
        return self._send(302, b"", headers={"Location": loc})

    def _token(self):
        f = self._form()
        g = lambda k: (f.get(k) or [""])[0]
        if g("grant_type") == "authorization_code":
            code = unseal("code", g("code"))
            if not code or code["r"] != g("redirect_uri"):
                return self._send(400, {"error": "invalid_grant"})
            verifier = g("code_verifier")
            if _b64(hashlib.sha256(verifier.encode()).digest()) != code["c"]:
                return self._send(400, {"error": "invalid_grant", "error_description": "PKCE"})
            boxes = code["b"]
        elif g("grant_type") == "refresh_token":
            boxes = unseal("refresh", g("refresh_token"))
            if not boxes:
                return self._send(400, {"error": "invalid_grant"})
        else:
            return self._send(400, {"error": "unsupported_grant_type"})
        return self._send(200, {
            "access_token": seal("access", boxes, ACCESS_TTL),
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "refresh_token": seal("refresh", boxes, REFRESH_TTL),
            "scope": "email",
        })

    def _mcp(self):
        auth = self.headers.get("Authorization") or ""
        boxes = unseal("access", auth[7:].strip()) if auth.lower().startswith("bearer ") else None
        if not boxes:
            meta = f'{self._base()}/.well-known/oauth-protected-resource'
            return self._send(401, {"error": "unauthorized"},
                              headers={"WWW-Authenticate": f'Bearer resource_metadata="{meta}"'})
        aruba_tools.CURRENT.set(boxes)
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            req = json.loads(self.rfile.read(n) if n else b"{}")
        except Exception:
            return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON non valido"}})
        if isinstance(req, list):
            out = [r for r in (aruba_tools.rpc(x) for x in req) if r]
        else:
            out = aruba_tools.rpc(req)
        return self._send(200, out) if out else self._send(202)
