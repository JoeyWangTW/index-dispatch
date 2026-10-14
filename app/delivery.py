"""Destination adapters. Callback connections pin a validated public address."""

import asyncio
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import socket
import ssl
import time
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


def signing_key(secret):
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        raise ValueError("Invalid signing secret")
    key = base64.b64decode(secret[6:], validate=True)
    if not 24 <= len(key) <= 64:
        raise ValueError("Invalid signing secret length")
    return key


def signed_headers(secret, event_id, sub_id, body, timestamp=None):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    signed = event_id.encode() + b"." + timestamp.encode() + b"." + body
    signature = base64.b64encode(hmac.digest(signing_key(secret), signed, hashlib.sha256)).decode()
    return {
        "Content-Type": "application/json",
        "webhook-id": event_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": "v1," + signature,
        "X-MCP-Subscription-Id": sub_id,
    }


def validate_callback(url, hosts):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
        or any(ord(c) < 33 for c in url)
    ):
        raise ValueError("Callback must use HTTPS on an explicitly allowed host")
    return parsed


def public_addresses(host):
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Callback resolves to a non-public address")
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        # Resolve exactly once; connect to that sockaddr, with original SNI/certificate verification.
        addresses = public_addresses(self.host)
        family, kind, protocol, _, address = addresses[0]
        raw = socket.socket(family, kind, protocol)
        raw.settimeout(self.timeout)
        try:
            raw.connect(address)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class CallbackSender:
    def __init__(self, hosts):
        self.hosts = hosts

    async def post(self, url, body, headers):
        parsed = validate_callback(url, self.hosts)
        if len(body) > 262144:
            raise ValueError("Event too large")

        def send():
            connection = PinnedHTTPS(parsed.hostname, timeout=10, context=ssl.create_default_context())
            try:
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                connection.request("POST", path, body=body, headers=headers)
                response = connection.getresponse()
                # No redirects; bound the amount of callback response data retained.
                return response.status, response.read(8192)
            finally:
                connection.close()

        return await asyncio.to_thread(send)


class Notion:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client

    async def append(self, request):
        page = self.settings.destinations[request["destination"]]
        url = f"https://api.notion.com/v1/blocks/{page}/children"
        headers = {
            "Authorization": f"Bearer {self.settings.notion_token}",
            "Notion-Version": "2022-06-28",
        }
        recorded = request.get("recorded_at")
        timestamp = recorded / 1000 if recorded is not None else request["created"]
        day = datetime.fromtimestamp(timestamp, ZoneInfo(self.settings.notion_timezone)).date().isoformat()
        # Read the page on every delivery so restarts and manual edits cannot stale a cache.
        blocks, cursor, seen = [], None, set()
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            response = await self.client.get(url, headers=headers, params=params, timeout=20)
            if not response.is_success:
                return response
            data = response.json()
            blocks.extend(data["results"])
            if not data["has_more"]:
                break
            cursor = data["next_cursor"]
            if not cursor or cursor in seen:
                raise ValueError("Invalid Notion pagination")
            seen.add(cursor)

        after = None
        for i, block in enumerate(blocks):
            if block.get("type") != "heading_2":
                continue
            heading = "".join(
                part.get("plain_text", part.get("text", {}).get("content", ""))
                for part in block["heading_2"]["rich_text"]
            )
            if heading == day:
                after = block["id"]
                for following in blocks[i + 1 :]:
                    if following.get("type", "").startswith("heading_"):
                        break
                    after = following["id"]
                break

        def rich_text(text):
            return [
                {"type": "text", "text": {"content": text[i : i + 1800]}} for i in range(0, len(text), 1800)
            ]

        # One bullet per recording, even for long notes. IDs remain in the dashboard.
        children = [
            {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": rich_text(request["body"])},
            }
        ]
        payload = {"children": children}
        if after:
            # Insert before the next date's heading, including for delayed recordings.
            payload["after"] = after
        else:
            children.insert(
                0,
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {"rich_text": rich_text(day)},
                },
            )
        # A single write creates both a missing heading and its first bullet.
        return await self.client.patch(url, headers=headers, json=payload, timeout=20)


def serialize(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
