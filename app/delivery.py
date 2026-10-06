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
from urllib.parse import urlsplit


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
        text = request["body"]
        chunks = [text[i : i + 1800] for i in range(0, len(text), 1800)]
        chunks.append(f"Index Dispatch · {request['id']}")
        return await self.client.patch(
            f"https://api.notion.com/v1/blocks/{page}/children",
            headers={
                "Authorization": f"Bearer {self.settings.notion_token}",
                "Notion-Version": "2022-06-28",
            },
            json={
                "children": [
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
                    }
                    for chunk in chunks
                ]
            },
            timeout=20,
        )


def serialize(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
