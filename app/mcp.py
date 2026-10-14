import asyncio
import hashlib
import json
import secrets
import time

from .delivery import serialize, signed_headers, signing_key, validate_callback
from .worker import iso

EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
TASK_EVENT = {
    "name": "task.created",
    "description": "A voice task for the owner's Dot is ready to execute.",
    "delivery": ["webhook"],
    "inputSchema": EMPTY_SCHEMA,
    "payloadSchema": {
        "type": "object",
        "properties": {"task_id": {"type": "string"}, "text": {"type": "string"}},
        "required": ["task_id", "text"],
        "additionalProperties": False,
    },
}
TOOLS = [
    {
        "name": "get_task",
        "description": "Retrieve a voice task delivered to this connection.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "report_task_result",
        "description": "Record completion or failure of a delivered task. Idempotent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "status": {"type": "string", "enum": ["completed", "failed"]},
                "result": {"type": "string", "maxLength": 4000},
            },
            "required": ["task_id", "status", "result"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
]


class RPCError(Exception):
    def __init__(self, code, message, data=None):
        self.code, self.message, self.data = code, message, data


class MCP:
    def __init__(self, settings, store, callbacks):
        self.settings, self.store, self.callbacks = settings, store, callbacks
        self.lock = asyncio.Lock()

    async def call(self, method, params, client):
        if method == "server/discover":
            return {
                "resultType": "complete",
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {}, "events": {}},
            }
        if method == "initialize":
            return {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "index-dispatch", "version": "0.1.0"},
            }
        if method == "ping":
            return {}
        if method == "events/list":
            return {"events": [TASK_EVENT]}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method in {"events/subscribe", "events/unsubscribe"}:
            async with self.lock:
                return await self.subscription(method, params, client)
        if method == "tools/call":
            return self.tool(params, client)
        raise RPCError(-32601, "Method not found")

    async def subscription(self, method, p, client):
        delivery = p.get("delivery", {})
        if (
            p.get("name") != "task.created"
            or p.get("arguments", {}) != {}
            or not isinstance(delivery, dict)
            or delivery.get("mode") != "webhook"
            or not isinstance(delivery.get("url"), str)
            or p.get("cursor") is not None
        ):
            raise RPCError(-32602, "Invalid event subscription")
        url = delivery["url"]
        identity = serialize([client, url, "task.created", {}])
        sub_id = "sub_" + hashlib.sha256(identity).hexdigest()
        if method == "events/unsubscribe":
            self.store.db.execute(
                "UPDATE subscriptions SET active=0 WHERE id=? AND client_id=?", (sub_id, client)
            )
            return {}
        try:
            validate_callback(url, self.settings.callback_hosts)
            secret = delivery["secret"]
            signing_key(secret)
            ttl = p.get("ttlMs", 86400000)
            if ttl is None:
                ttl = 86400000
            if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 0 < ttl <= 1e12:
                raise ValueError("Invalid lifetime")
        except (ValueError, TypeError, KeyError):
            raise RPCError(-32602, "Invalid callback, secret or lifetime") from None
        current = self.store.subscription()
        if current and current["id"] != sub_id:
            raise RPCError(-32602, "V1 supports one Dot subscription; unsubscribe the existing one first")
        verified = self.store.oauth_get("verified", sub_id)
        if not verified or verified["secret_hash"] != hashlib.sha256(secret.encode()).hexdigest():
            challenge = secrets.token_urlsafe(32)
            body = serialize({"type": "verification", "challenge": challenge})
            headers = signed_headers(secret, "verify_" + secrets.token_hex(16), sub_id, body)
            try:
                status, response = await self.callbacks.post(url, body, headers)
                echo = json.loads(response).get("challenge", "")
                valid = (
                    200 <= status < 300 and isinstance(echo, str) and secrets.compare_digest(echo, challenge)
                )
            except Exception:
                valid = False
            if not valid:
                raise RPCError(-32015, "Callback verification failed", {"reason": "challenge_failed"})
            self.store.oauth_put(
                "verified", sub_id, {"secret_hash": hashlib.sha256(secret.encode()).hexdigest()}, 300
            )
        if current and self.store.cipher.decrypt(current["secret"].encode()).decode() != secret:
            self.store.oauth_put("rotation", sub_id, {"secret": current["secret"]}, 300)
        expires = time.time() + min(86400, max(60, ttl / 1000))
        self.store.save_subscription(sub_id, client, url, secret, expires)
        self.store.db.execute("UPDATE requests SET next_attempt=0 WHERE status='waiting_subscription'")
        return {"id": sub_id, "refreshBefore": iso(expires), "cursor": None, "truncated": False}

    def tool(self, p, client):
        name, args = p.get("name"), p.get("arguments", {})
        if name not in {"get_task", "report_task_result"} or not isinstance(args, dict):
            raise RPCError(-32602, "Unknown tool or invalid arguments")
        row = self.store.get(str(args.get("task_id", "")))
        sub = self.store.db.execute(
            "SELECT client_id FROM subscriptions WHERE id=?", (row["subscription_id"] if row else "",)
        ).fetchone()
        if not row or row["route"] != "dots" or not sub or sub[0] != client:
            raise RPCError(-32602, "Task not found for this connection")
        if name == "report_task_result":
            if (
                args.get("status") not in {"completed", "failed"}
                or not isinstance(args.get("result"), str)
                or len(args["result"]) > 4000
            ):
                raise RPCError(-32602, "Invalid task result")
            if row["status"] in {"completed", "failed"}:
                if row["status"] != args["status"] or row["detail"] != args["result"]:
                    raise RPCError(-32602, "A different final result is already recorded")
            else:
                self.store.update(row["id"], status=args["status"], detail=args["result"])
                row = self.store.get(row["id"])
        result = {"task_id": row["id"], "text": row["body"], "status": row["status"], "result": row["detail"]}
        return {"content": [{"type": "text", "text": json.dumps(result)}], "isError": False}
