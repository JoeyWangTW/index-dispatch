import asyncio
import time
from datetime import UTC, datetime

from .delivery import serialize, signed_headers


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


class Worker:
    def __init__(self, settings, store, router, notion, callbacks):
        self.settings, self.store = settings, store
        self.router, self.notion, self.callbacks = router, notion, callbacks

    async def run(self):
        while True:
            await self.tick()
            await asyncio.sleep(1)

    async def tick(self):
        for request in self.store.pending():
            try:
                await self.process(request)
            except Exception:
                # Never include upstream response bodies or credentials in status/log output.
                self.store.update(request["id"], status="needs_review", detail="Unexpected delivery error")

    def retry(self, request, detail):
        attempts = request["attempts"] + 1
        self.store.update(
            request["id"],
            status="ready" if attempts < 8 else "needs_review",
            attempts=attempts,
            next_attempt=time.time() + min(3600, 5 * 2**attempts),
            detail=detail,
        )

    async def process(self, request):
        rid = request["id"]
        if request["status"] == "queued":
            self.store.update(rid, status="routing")
            try:
                decision = await self.router.decide(request["transcript"])
            except Exception:
                self.store.update(rid, status="needs_review", detail="Routing failed; choose a destination")
                return
            self.store.update(
                rid,
                status="needs_review" if decision.route == "review" else "ready",
                route=decision.route,
                destination=decision.destination,
                body=decision.body,
                detail=decision.reason,
            )
            request = self.store.get(rid)
            if request["status"] == "needs_review":
                return
        if request["route"] == "note":
            if not self.settings.notion_token:
                self.store.update(rid, status="needs_review", detail="Set NOTION_TOKEN before sending notes")
                return
            self.store.update(rid, status="saving")
            try:
                response = await self.notion.append(request)
            except Exception:
                self.store.update(
                    rid,
                    status="needs_review",
                    detail="Notion outcome unknown. Check the page before retrying.",
                )
                return
            if 200 <= response.status_code < 300:
                self.store.update(rid, status="completed", detail="Saved to Notion")
            elif response.status_code == 429:
                self.retry(request, "Notion rate limited; retry scheduled")
            else:
                self.store.update(
                    rid,
                    status="needs_review",
                    detail=f"Notion HTTP {response.status_code}. Check page/access before retrying.",
                )
            return
        sub = self.store.subscription()
        if not sub:
            self.store.update(
                rid,
                status="waiting_subscription",
                next_attempt=time.time() + 10,
                detail="Waiting for a Dot to subscribe",
            )
            return
        if request["subscription_id"] and request["subscription_id"] != sub["id"]:
            self.store.update(
                rid, status="needs_review", detail="Destination subscription changed; review task"
            )
            return
        self.store.update(rid, status="delivering", subscription_id=sub["id"])
        event = {
            "eventId": rid,
            "name": "task.created",
            "timestamp": iso(request["created"]),
            "data": {"task_id": rid, "text": request["body"]},
            "cursor": None,
        }
        body = serialize(event)
        secret = self.store.cipher.decrypt(sub["secret"].encode()).decode()
        headers = signed_headers(secret, rid, sub["id"], body)
        old = self.store.oauth_get("rotation", sub["id"])
        if old:
            previous = self.store.cipher.decrypt(old["secret"].encode()).decode()
            headers["webhook-signature"] += (
                " "
                + signed_headers(previous, rid, sub["id"], body, int(headers["webhook-timestamp"]))[
                    "webhook-signature"
                ]
            )
        try:
            status, _ = await self.callbacks.post(sub["url"], body, headers)
        except Exception:
            self.retry(request, "Callback unavailable; retry scheduled")
            return
        # A fast agent may have reported completion while this HTTP request was in flight.
        if self.store.get(rid)["status"] in {"completed", "failed"}:
            return
        if 200 <= status < 300:
            self.store.update(rid, status="delivered", detail="Received by ChatGPT; awaiting task result")
        elif status == 429 or status >= 500:
            self.retry(request, f"Callback HTTP {status}; retry scheduled")
        else:
            if status == 410:
                self.store.db.execute("UPDATE subscriptions SET active=0 WHERE id=?", (sub["id"],))
            self.store.update(
                rid, status="needs_review", detail=f"Callback HTTP {status}; no automatic retry"
            )
