"""Single-owner OAuth authorization code + S256 PKCE, with rotating opaque tokens."""

import base64
import hashlib
import html
import re
import secrets
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def pkce(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def bearer(request, expected):
    actual = request.headers.get("authorization", "")
    if not secrets.compare_digest(actual.encode(), ("Bearer " + expected).encode()):
        raise HTTPException(401, "Invalid bearer token")


def install_auth(app, settings, store):
    base = settings.base_url
    resource = base + "/mcp"

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    async def resource_metadata():
        return {"resource": resource, "authorization_servers": [base], "scopes_supported": ["dispatch"]}

    @app.get("/.well-known/oauth-authorization-server")
    async def server_metadata():
        return {
            "issuer": base,
            "authorization_endpoint": base + "/oauth/authorize",
            "token_endpoint": base + "/oauth/token",
            "registration_endpoint": base + "/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["dispatch"],
        }

    @app.post("/oauth/register")
    async def register(request: Request):
        data = await request.json()
        redirects = data.get("redirect_uris") if isinstance(data, dict) else None
        if (
            not isinstance(redirects, list)
            or not 1 <= len(redirects) <= 4
            or any(r not in settings.oauth_redirects for r in redirects)
            or data.get("token_endpoint_auth_method", "none") != "none"
        ):
            return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
        count = store.db.execute("SELECT count(*) FROM oauth WHERE kind='client'").fetchone()[0]
        if count >= 100:
            raise HTTPException(429, "Client registration limit reached")
        client_id = secrets.token_urlsafe(24)
        store.oauth_put("client", client_id, {"redirects": redirects}, lifetime=365 * 86400)
        return JSONResponse(
            {
                "client_id": client_id,
                "redirect_uris": redirects,
                "token_endpoint_auth_method": "none",
                "response_types": ["code"],
                "grant_types": ["authorization_code", "refresh_token"],
            },
            status_code=201,
        )

    @app.get("/oauth/authorize")
    async def authorize(request: Request):
        p = dict(request.query_params)
        client = store.oauth_get("client", p.get("client_id", ""))
        if (
            not client
            or p.get("redirect_uri") not in client["redirects"]
            or p.get("redirect_uri") not in settings.oauth_redirects
            or p.get("response_type") != "code"
            or p.get("code_challenge_method") != "S256"
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", p.get("code_challenge", ""))
            or p.get("resource") != resource
            or p.get("scope", "dispatch") != "dispatch"
        ):
            raise HTTPException(400, "Invalid authorization request")
        handle = secrets.token_urlsafe(32)
        store.oauth_put("approval", digest(handle), p, lifetime=300)
        return HTMLResponse(f"""<!doctype html><html lang="en"><meta charset="utf-8">
          <meta name="viewport" content="width=device-width"><title>Connect Index Dispatch</title>
          <h1>Connect your Dot</h1><p>This grants access to your Dots task queue and task results.</p>
          <p>Redirect: {html.escape(p["redirect_uri"])}</p>
          <form method="post" action="/oauth/approve">
          <input type="hidden" name="handle" value="{handle}">
          <label>Index Dispatch admin token <input type="password" name="token" required autocomplete="off"></label>
          <button type="submit">Authorize connection</button></form></html>""")

    @app.post("/oauth/approve")
    async def approve(request: Request):
        form = await request.form()
        if not secrets.compare_digest(str(form.get("token", "")).encode(), settings.admin_token.encode()):
            raise HTTPException(401, "Invalid admin token")
        p = store.oauth_get("approval", digest(str(form.get("handle", ""))), consume=True)
        if not p:
            raise HTTPException(400, "Approval expired or already used")
        code = secrets.token_urlsafe(32)
        store.oauth_put("code", digest(code), p, lifetime=60)
        query = urlencode({"code": code, "state": p.get("state", "")})
        return RedirectResponse(p["redirect_uri"] + ("&" if "?" in p["redirect_uri"] else "?") + query, 303)

    @app.post("/oauth/token")
    async def token(request: Request):
        p = dict(await request.form())
        error = JSONResponse({"error": "invalid_grant"}, status_code=400)
        if p.get("grant_type") == "authorization_code":
            grant = store.oauth_get("code", digest(p.get("code", "")), consume=True)
            verifier = p.get("code_verifier", "")
            if (
                not grant
                or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)
                or grant["redirect_uri"] != p.get("redirect_uri")
                or not secrets.compare_digest(grant["code_challenge"], pkce(verifier))
            ):
                return error
        elif p.get("grant_type") == "refresh_token":
            grant = store.oauth_get("refresh", digest(p.get("refresh_token", "")), consume=True)
        else:
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        if (
            not grant
            or grant["client_id"] != p.get("client_id")
            or p.get("resource") != resource
            or not store.oauth_get("client", grant["client_id"])
        ):
            return error
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        principal = {"client_id": grant["client_id"], "resource": resource}
        store.oauth_put("access", digest(access), principal, 3600)
        store.oauth_put("refresh", digest(refresh), principal, 30 * 86400)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": refresh,
            "scope": "dispatch",
        }


def principal(request, settings, store):
    auth = request.headers.get("authorization", "")
    record = store.oauth_get("access", digest(auth[7:])) if auth.startswith("Bearer ") else None
    if (
        not record
        or record["resource"] != settings.base_url + "/mcp"
        or not store.oauth_get("client", record["client_id"])
    ):
        raise HTTPException(
            401,
            "Connect using OAuth",
            headers={
                "WWW-Authenticate": f'Bearer resource_metadata="{settings.base_url}/.well-known/oauth-protected-resource"'
            },
        )
    return record["client_id"]
