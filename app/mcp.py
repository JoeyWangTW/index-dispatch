import asyncio
import hashlib
import json
import logging
import secrets
import time
from urllib.parse import urlsplit

from .delivery import serialize, signed_headers, signing_key, validate_callback
from .worker import iso

logger = logging.getLogger("uvicorn.error")

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
        "name": "list_pending_tasks",
        "description": (
            "Recover unfinished voice tasks assigned to this connection when an event payload or task ID "
            "is missing, or reconcile a batch. Reading does not acknowledge or claim work. Check each "
            "task's latest state and prior chat results before acting; never blindly repeat old work. "
            "Follow next_after_task_id until null."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
                "after_task_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_task",
        "description": (
            "Retrieve a voice task delivered to this connection. For clients with a cached tool catalog, "
            "task_id='pending' lists unfinished tasks, and 'pending:<next_after_task_id>' reads the next "
            "page. These read-only selectors have the same ownership checks as list_pending_tasks."
        ),
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
        "description": (
            "Acknowledge a voice task with in_progress before working, report needs_input when waiting "
            "for the user, then completed after replying or failed if unable to finish. Include a short "
            "progress/result summary. Repeating the same report is idempotent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "status": {"type": "string", "enum": ["in_progress", "needs_input", "completed", "failed"]},
                "result": {"type": "string", "minLength": 1, "maxLength": 4000},
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
        except (ValueError, TypeError):
            # Only the hostname is diagnostic; callback paths and queries may contain secrets.
            try:
                host = urlsplit(url).hostname or "missing"
                host = host if len(host) <= 253 and all(c.isalnum() or c in ".-" for c in host) else "invalid"
            except ValueError:
                host = "invalid"
            logger.warning("MCP subscription rejected: callback host=%s", host)
            raise RPCError(-32602, "Invalid callback URL") from None
        try:
            secret = delivery["secret"]
            signing_key(secret)
        except (ValueError, TypeError, KeyError):
            logger.warning("MCP subscription rejected: signing secret format")
            raise RPCError(-32602, "Invalid signing secret") from None
        ttl = p.get("ttlMs", 86400000)
        if ttl is None:
            ttl = 86400000
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 0 < ttl <= 1e12:
            logger.warning("MCP subscription rejected: lifetime")
            raise RPCError(-32602, "Invalid subscription lifetime")
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
        if name not in {"list_pending_tasks", "get_task", "report_task_result"} or not isinstance(args, dict):
            raise RPCError(-32602, "Unknown tool or invalid arguments")
        if name == "list_pending_tasks":
            return self.pending_tasks(args, client)
        # Keep recovery usable while clients cache the original get_task input schema.
        # Real task IDs are UUIDs; these reserved selectors cannot collide with them.
        task_id = args.get("task_id")
        if name == "get_task" and isinstance(task_id, str):
            if task_id == "pending":
                return self.pending_tasks({}, client)
            if task_id.startswith("pending:"):
                return self.pending_tasks({"after_task_id": task_id.removeprefix("pending:")}, client)
        row = self.store.get(str(args.get("task_id", "")))
        sub = self.store.db.execute(
            "SELECT client_id FROM subscriptions WHERE id=?", (row["subscription_id"] if row else "",)
        ).fetchone()
        if not row or row["route"] != "dots" or not sub or sub[0] != client:
            raise RPCError(-32602, "Task not found for this connection")
        if name == "report_task_result":
            if (
                args.get("status") not in {"in_progress", "needs_input", "completed", "failed"}
                or not isinstance(args.get("result"), str)
                or not args["result"].strip()
                or len(args["result"]) > 4000
            ):
                raise RPCError(-32602, "Invalid task result")
            if row["status"] in {"completed", "failed"}:
                if args["status"] in {"completed", "failed"} and (
                    row["status"] != args["status"] or row["detail"] != args["result"]
                ):
                    raise RPCError(-32602, "A different final result is already recorded")
            elif row["status"] != args["status"] or row["detail"] != args["result"]:
                now = time.time()
                self.store.update(
                    row["id"],
                    status=args["status"],
                    detail=args["result"],
                    acknowledged_at=row["acknowledged_at"] or now,
                    progress_at=now,
                    finished_at=now if args["status"] in {"completed", "failed"} else None,
                )
                row = self.store.get(row["id"])
        return self.response(self.task_data(row))

    @staticmethod
    def response(result):
        return {"content": [{"type": "text", "text": json.dumps(result)}], "isError": False}

    @staticmethod
    def task_data(row):
        result = {
            "task_id": row["id"],
            "text": row["body"],
            "status": row["status"],
            "result": row["detail"],
            "created_at": row["created"],
        }
        result.update(
            {key: row[key] for key in ("delivered_at", "acknowledged_at", "progress_at", "finished_at")}
        )
        return result

    def pending_tasks(self, args, client):
        limit, after = args.get("limit", 10), args.get("after_task_id")
        if (
            args.keys() - {"limit", "after_task_id"}
            or type(limit) is not int
            or not 1 <= limit <= 20
            or (after is not None and (not isinstance(after, str) or not after))
        ):
            raise RPCError(-32602, "Invalid pending-task query")
        # Keyset pagination survives an earlier page being completed between reads.
        position = (-1, "")
        if after is not None:
            anchor = self.store.db.execute(
                """SELECT r.created, r.id FROM requests r
                   JOIN subscriptions s ON s.id=r.subscription_id
                   WHERE r.id=? AND r.route='dots' AND s.client_id=?""",
                (after, client),
            ).fetchone()
            if not anchor:
                raise RPCError(-32602, "Task not found for this connection")
            position = tuple(anchor)
        rows = self.store.db.execute(
            """SELECT r.* FROM requests r JOIN subscriptions s ON s.id=r.subscription_id
               WHERE s.client_id=? AND r.route='dots'
                 AND r.status IN ('delivering','delivered','in_progress','needs_input')
                 AND (r.created,r.id) > (?,?)
               ORDER BY r.created,r.id LIMIT ?""",
            (client, *position, limit + 1),
        ).fetchall()
        return self.response(
            {
                "tasks": [self.task_data(row) for row in rows[:limit]],
                "next_after_task_id": rows[limit - 1]["id"] if len(rows) > limit else None,
            }
        )
