import asyncio
import contextlib
import json
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .auth import bearer, install_auth, principal
from .config import Settings
from .delivery import CallbackSender, Notion
from .mcp import MCP, RPCError
from .router import Decision, Router, validate
from .store import Store
from .worker import Worker


class Limits:
    def __init__(self, app):
        self.app = app
        self.rates = defaultdict(deque)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["method"] == "POST":
            category = "oauth" if scope["path"].startswith("/oauth") else "other"
            queue, now = self.rates[category], time.monotonic()
            while queue and queue[0] < now - 60:
                queue.popleft()
            if len(queue) >= (30 if category == "oauth" else 120):
                return await JSONResponse({"detail": "Rate limit exceeded"}, 429)(scope, receive, send)
            queue.append(now)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > 65536:
                return await JSONResponse({"detail": "Text-only request limit is 64 KiB"}, 413)(
                    scope, receive, send
                )
            if not message.get("more_body"):
                break

        async def buffered():
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] += [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; frame-ancestors 'none'",
                    ),
                ]
            await send(message)

        await self.app(scope, buffered, secure_send)


def create_app(settings=None, client=None, callbacks=None):
    settings = settings or Settings.from_env()
    store = Store(settings)
    client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
    callbacks = callbacks or CallbackSender(settings.callback_hosts)
    worker = Worker(settings, store, Router(settings, client), Notion(settings, client), callbacks)
    mcp = MCP(settings, store, callbacks)

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        task = asyncio.create_task(worker.run()) if settings.worker_enabled else None
        app.state.worker_task = task
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await client.aclose()
        store.close()

    app = FastAPI(title="Index Dispatch", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(Limits)
    app.state.store, app.state.worker, app.state.mcp = store, worker, mcp
    install_auth(app, settings, store)

    @app.exception_handler(json.JSONDecodeError)
    @app.exception_handler(UnicodeDecodeError)
    async def invalid_json(request, exc):
        return JSONResponse({"detail": "Invalid JSON"}, 400)

    @app.get("/healthz")
    async def health():
        store.db.execute("SELECT 1")
        task = getattr(app.state, "worker_task", None)
        if task and task.done():
            raise HTTPException(503, "Delivery worker stopped")
        return {"status": "ok"}

    @app.get("/")
    async def home():
        return FileResponse(Path(__file__).parent / "static/index.html")

    @app.get("/app.js")
    async def javascript():
        return FileResponse(Path(__file__).parent / "static/app.js", media_type="application/javascript")

    @app.get("/style.css")
    async def stylesheet():
        return FileResponse(Path(__file__).parent / "static/style.css", media_type="text/css")

    async def ingest(request, admin=False):
        bearer(request, settings.admin_token if admin else settings.ingest_token)
        if request.headers.get("content-type", "").startswith("application/json"):
            data = await request.json()
        else:
            form = await request.form(max_files=0, max_fields=5, max_part_size=65536)
            data = dict(form)
        if not isinstance(data, dict):
            raise HTTPException(400, "Expected an object")
        text = data.get("transcription")
        if not isinstance(text, str) or not text.strip() or len(text) > 12000:
            raise HTTPException(400, "transcription must contain 1–12000 characters")
        stamp = data.get("recordedAt")
        try:
            if stamp is not None:
                if (
                    isinstance(stamp, bool)
                    or str(stamp) != str(int(stamp))
                    or not 0 <= int(stamp) <= 4102444800000
                ):
                    raise ValueError()
                stamp = int(stamp)
        except (ValueError, TypeError, OverflowError):
            raise HTTPException(400, "recordedAt must be Unix milliseconds") from None
        key = request.headers.get("idempotency-key")
        if key and len(key) > 200:
            raise HTTPException(400, "Idempotency key too long")
        try:
            row, created = store.add(text.strip(), stamp, "manual" if admin else "ring", key)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return JSONResponse({"id": row["id"], "status": row["status"], "duplicate": not created}, 202)

    @app.post("/webhooks/index")
    async def index(request: Request):
        return await ingest(request)

    @app.post("/api/requests")
    async def manual(request: Request):
        return await ingest(request, admin=True)

    @app.get("/api/requests")
    async def recent(request: Request):
        bearer(request, settings.admin_token)
        sub = store.subscription()
        return {
            "requests": store.recent(),
            "destinations": list(settings.destinations),
            "router": settings.router_mode,
            "dots_connected": bool(sub),
        }

    @app.post("/api/requests/{request_id}/resolve")
    async def resolve(request_id: str, request: Request):
        bearer(request, settings.admin_token)
        row = store.get(request_id)
        if not row:
            raise HTTPException(404, "Not found")
        if row["status"] != "needs_review":
            raise HTTPException(409, "Only requests awaiting review can be resolved")
        data = await request.json()
        try:
            decision = validate(
                Decision(data["route"], data["destination"], row["body"] or row["transcript"]), settings
            )
            if decision.route == "review":
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "Choose note and a configured destination, or dots") from None
        store.update(
            request_id,
            status="ready",
            route=decision.route,
            destination=decision.destination,
            body=decision.body,
            detail="Manually approved",
            attempts=0,
            next_attempt=0,
            subscription_id=None,
        )
        return {"status": "ready"}

    @app.post("/api/dots/disconnect")
    async def disconnect(request: Request):
        bearer(request, settings.admin_token)
        store.db.execute("UPDATE subscriptions SET active=0")
        store.db.execute("DELETE FROM oauth")
        return {"status": "disconnected"}

    @app.post("/mcp")
    async def rpc(request: Request):
        client_id = principal(request, settings, store)
        origin = request.headers.get("origin")
        if origin and origin != settings.base_url:
            raise HTTPException(403, "Origin not allowed")
        try:
            data = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            )
        if (
            not isinstance(data, dict)
            or data.get("jsonrpc") != "2.0"
            or not isinstance(data.get("method"), str)
            or not isinstance(data.get("params", {}), dict)
        ):
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
            )
        if "id" not in data:
            return Response(status_code=202)
        envelope = {"jsonrpc": "2.0", "id": data["id"]}
        try:
            envelope["result"] = await mcp.call(data["method"], data.get("params", {}), client_id)
        except RPCError as exc:
            envelope["error"] = {"code": exc.code, "message": exc.message}
            if exc.data:
                envelope["error"]["data"] = exc.data
        except Exception:
            envelope["error"] = {"code": -32603, "message": "Internal error"}
        return JSONResponse(envelope)

    return app
