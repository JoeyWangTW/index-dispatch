from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.delivery import Notion


def block(kind, text):
    return {
        "id": str(uuid4()),
        "object": "block",
        "type": kind,
        kind: {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def text_of(item):
    return "".join(p["text"]["content"] for p in item[item["type"]]["rich_text"])


class Page:
    def __init__(self, blocks=None):
        self.blocks = blocks or []
        self.reads = 0
        self.writes = 0
        self.read_status = 200

    def respond(self, request):
        if request.method == "GET":
            self.reads += 1
            start = int(request.url.params.get("start_cursor", 0))
            end = start + int(request.url.params["page_size"])
            return httpx.Response(
                self.read_status,
                json={
                    "results": self.blocks[start:end],
                    "has_more": end < len(self.blocks),
                    "next_cursor": str(end) if end < len(self.blocks) else None,
                },
            )
        import json

        self.writes += 1
        payload = json.loads(request.content)
        position = len(self.blocks)
        if "after" in payload:
            position = next(i + 1 for i, b in enumerate(self.blocks) if b["id"] == payload["after"])
        children = [{**b, "id": str(uuid4())} for b in payload["children"]]
        self.blocks[position:position] = children
        return httpx.Response(200, json={"results": children})


async def deliver(page, body, recorded=None, created="2026-10-15T12:00:00+00:00"):
    settings = SimpleNamespace(
        destinations={"ideas": "a" * 32}, notion_token="test", notion_timezone="America/Los_Angeles"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(page.respond)) as client:
        # A fresh adapter each time exercises restarts without relying on an in-memory heading cache.
        return await Notion(settings, client).append(
            {
                "destination": "ideas",
                "body": body,
                "recorded_at": int(datetime.fromisoformat(recorded).timestamp() * 1000) if recorded else None,
                "created": datetime.fromisoformat(created).timestamp(),
            }
        )


async def test_daily_groups_and_delayed_recording():
    page = Page()
    await deliver(page, "first idea", "2026-10-14T18:00:00+00:00")
    await deliver(page, "second idea", "2026-10-15T06:59:00+00:00")
    await deliver(page, "next day", "2026-10-15T07:00:00+00:00")
    await deliver(page, "late upload", "2026-10-14T19:00:00+00:00")
    assert [(b["type"], text_of(b)) for b in page.blocks] == [
        ("heading_2", "2026-10-14"),
        ("bulleted_list_item", "first idea"),
        ("bulleted_list_item", "second idea"),
        ("bulleted_list_item", "late upload"),
        ("heading_2", "2026-10-15"),
        ("bulleted_list_item", "next day"),
    ]


async def test_paginated_heading_keeps_existing_content():
    original = [block("paragraph", f"Existing {i}") for i in range(100)]
    page = Page(original + [block("heading_2", "2026-10-15"), block("bulleted_list_item", "earlier")])
    await deliver(page, "new note")
    assert page.reads == 2 and page.writes == 1
    assert page.blocks[:100] == original
    assert [text_of(b) for b in page.blocks[100:]] == ["2026-10-15", "earlier", "new note"]


@pytest.mark.parametrize(
    "timestamp,day",
    [
        ("2026-01-02T07:59:00+00:00", "2026-01-01"),
        ("2026-01-02T08:00:00+00:00", "2026-01-02"),
        ("2026-07-02T06:59:00+00:00", "2026-07-01"),
        ("2026-07-02T07:00:00+00:00", "2026-07-02"),
    ],
)
async def test_missing_recording_time_uses_receipt_date_with_dst(timestamp, day):
    page = Page()
    await deliver(page, "note", created=timestamp)
    assert text_of(page.blocks[0]) == day


@pytest.mark.parametrize("status", [403, 429, 500])
async def test_failed_lookup_never_writes_duplicate_heading(status):
    page = Page()
    page.read_status = status
    response = await deliver(page, "note")
    assert response.status_code == status
    assert page.writes == 0 and not page.blocks
