import base64
import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet

from app.auth import digest, pkce
from app.config import Settings
from app.delivery import public_addresses, signed_headers, validate_callback
from app.main import create_app
from app.store import Store


class Callbacks:
    def __init__(self):
        self.calls = []
        self.status = 202
        self.verify = True

    async def post(self, url, body, headers):
        data = json.loads(body)
        self.calls.append((url, data, headers, body))
        if data.get("type") == "verification":
            return 200, json.dumps({"challenge": data["challenge"] if self.verify else "wrong"}).encode()
        return self.status, b"{}"


@pytest.fixture
async def env(tmp_path):
    settings = Settings(
        base_url="https://dispatch.example",
        admin_token="a" * 40,
        ingest_token="i" * 40,
        encryption_key=Fernet.generate_key().decode(),
        database=str(tmp_path / "db.sqlite3"),
        destinations={"inbox": "a" * 32, "ideas": "b" * 32},
        notion_token="test-notion",
        notion_timezone="America/Los_Angeles",
        oauth_redirects=["https://chatgpt.com/callback"],
        worker_enabled=False,
    )
    state = {"notion_status": 200, "notion_calls": [], "ai": None}

    def upstream(request):
        if request.url.host == "api.notion.com":
            if request.method == "GET":
                return httpx.Response(200, json={"results": [], "has_more": False})
            state["notion_calls"].append(request)
            if state["notion_status"] == "timeout":
                raise httpx.ReadTimeout("test", request=request)
            return httpx.Response(state["notion_status"], json={})
        return httpx.Response(200, json=state["ai"])

    callbacks = Callbacks()
    app = create_app(settings, httpx.AsyncClient(transport=httpx.MockTransport(upstream)), callbacks)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=settings.base_url
        ) as client:
            yield settings, app, client, callbacks, state


def auth(settings, admin=False):
    return {"Authorization": "Bearer " + (settings.admin_token if admin else settings.ingest_token)}


async def add(env, text, stamp=None):
    s, app, client, _, _ = env
    payload = {"transcription": text}
    if stamp is not None:
        payload["recordedAt"] = stamp
    response = await client.post("/webhooks/index", json=payload, headers=auth(s))
    assert response.status_code == 202
    await app.state.worker.tick()
    return app.state.store.get(response.json()["id"])


def subscription_params(secret=None):
    return {
        "name": "task.created",
        "arguments": {},
        "delivery": {
            "mode": "webhook",
            "url": "https://chatgpt.com/events/test",
            "secret": secret or "whsec_" + base64.b64encode(b"s" * 32).decode(),
        },
    }


async def test_note_real_payload_and_duplicate(env):
    s, app, c, _, state = env
    payload = {
        "transcription": (None, "Note to ideas: 雲端筆記"),
        "recordedAt": (None, "1791234567000"),
        "client": (None, "ring"),
    }
    first = await c.post("/webhooks/index", files=payload, headers=auth(s))
    second = await c.post("/webhooks/index", files=payload, headers=auth(s))
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"] and second.json()["duplicate"]
    await app.state.worker.tick()
    assert app.state.store.get(first.json()["id"])["status"] == "completed"
    assert len(state["notion_calls"]) == 1
    sent = state["notion_calls"][0]
    assert sent.method == "PATCH" and str(sent.url).endswith("b" * 32 + "/children")
    assert (
        json.loads(sent.content)["children"][1]["bulleted_list_item"]["rich_text"][0]["text"]["content"]
        == "雲端筆記"
    )


@pytest.mark.parametrize("status", [500, "timeout", 403])
async def test_ambiguous_note_never_automatically_repeated(env, status):
    _, app, _, _, state = env
    state["notion_status"] = status
    row = await add(env, "remember this")
    assert row["status"] == "needs_review"
    await app.state.worker.tick()
    assert len(state["notion_calls"]) == 1


async def test_rate_limited_note_retries_and_long_note_chunks(env):
    _, app, _, _, state = env
    state["notion_status"] = 429
    row = await add(env, "x" * 9000)
    assert row["status"] == "ready" and row["attempts"] == 1
    state["notion_status"] = 200
    app.state.store.update(row["id"], next_attempt=0)
    await app.state.worker.tick()
    assert app.state.store.get(row["id"])["status"] == "completed"
    blocks = json.loads(state["notion_calls"][0].content)["children"]
    parts = blocks[1]["bulleted_list_item"]["rich_text"]
    assert len(blocks) == 2
    assert max(len(p["text"]["content"]) for p in parts) <= 2000
    assert "".join(p["text"]["content"] for p in parts) == "x" * 9000


async def test_dots_waits_verifies_delivers_and_reports(env):
    _, app, _, cb, _ = env
    row = await add(env, "Dots, research train tickets")
    assert row["status"] == "waiting_subscription" and not cb.calls
    sub = await app.state.mcp.call("events/subscribe", subscription_params(), "owner-client")
    await app.state.worker.tick()
    row = app.state.store.get(row["id"])
    assert row["status"] == "delivered" and row["subscription_id"] == sub["id"]
    _, event, headers, body = cb.calls[-1]
    assert event["data"]["text"] == "research train tickets" and event["eventId"] == row["id"]
    signed = f"{row['id']}.{headers['webhook-timestamp']}.".encode() + body
    expected = "v1," + base64.b64encode(hmac.digest(b"s" * 32, signed, hashlib.sha256)).decode()
    assert headers["webhook-signature"] == expected
    params = {
        "name": "report_task_result",
        "arguments": {"task_id": row["id"], "status": "completed", "result": "Done"},
    }
    await app.state.mcp.call("tools/call", params, "owner-client")
    await app.state.mcp.call("tools/call", params, "owner-client")
    assert app.state.store.get(row["id"])["status"] == "completed"
    with pytest.raises(Exception, match="Task not found"):
        await app.state.mcp.call("tools/call", params, "different-client")


async def test_subscription_failure_rotation_and_persistence(env):
    s, app, _, cb, _ = env
    cb.verify = False
    with pytest.raises(Exception, match="Callback verification failed"):
        await app.state.mcp.call("events/subscribe", subscription_params(), "owner")
    assert app.state.store.subscription() is None
    cb.verify = True
    sub = await app.state.mcp.call("events/subscribe", subscription_params(), "owner")
    replacement = "whsec_" + base64.b64encode(b"n" * 32).decode()
    again = await app.state.mcp.call("events/subscribe", subscription_params(replacement), "owner")
    assert sub["id"] == again["id"]
    other = Store(s)
    saved = other.subscription()
    assert (
        saved["secret"] != replacement
        and other.cipher.decrypt(saved["secret"].encode()).decode() == replacement
    )
    other.close()
    await add(env, "dots: test rotation")
    assert cb.calls[-1][2]["webhook-signature"].count("v1,") == 2
    await app.state.mcp.call("events/unsubscribe", subscription_params(), "owner")
    assert app.state.store.subscription() is None


@pytest.mark.parametrize("status,retry", [(503, True), (429, True), (410, False), (413, False)])
async def test_dots_retry_policy_and_stable_event_id(env, status, retry):
    _, app, _, cb, _ = env
    await app.state.mcp.call("events/subscribe", subscription_params(), "owner")
    cb.status = status
    row = await add(env, "dots: test")
    assert row["status"] == ("ready" if retry else "needs_review")
    if retry:
        cb.status = 202
        app.state.store.update(row["id"], next_attempt=0)
        await app.state.worker.tick()
        assert cb.calls[-2][1]["eventId"] == cb.calls[-1][1]["eventId"]


async def test_ingress_permissions_limits_and_key_conflict(env):
    s, _, c, _, _ = env
    assert (await c.post("/webhooks/index", json={"transcription": "hello"})).status_code == 401
    assert (await c.get("/api/requests", headers=auth(s))).status_code == 401
    assert (await c.post("/webhooks/index", content=b"a" * 65537, headers=auth(s))).status_code == 413
    assert (await c.post("/webhooks/index", json={"transcription": ""}, headers=auth(s))).status_code == 400
    assert (
        await c.post("/webhooks/index", json={"transcription": "ok", "recordedAt": -1}, headers=auth(s))
    ).status_code == 400
    headers = {**auth(s), "Idempotency-Key": "same"}
    assert (
        await c.post("/webhooks/index", json={"transcription": "one"}, headers=headers)
    ).status_code == 202
    assert (
        await c.post("/webhooks/index", json={"transcription": "two"}, headers=headers)
    ).status_code == 409


async def test_unknown_destination_and_classifier_failure_review(env):
    s, app, _, _, state = env
    row = await add(env, "note to missing: save me")
    assert row["status"] == "needs_review" and not state["notion_calls"]
    s.router_mode, s.router_model, s.openai_key = "openai", "test-model", "test-key"
    state["ai"] = {"status": "incomplete", "output": []}
    row = await add(env, "please plan my weekend")
    assert row["status"] == "needs_review" and not state["notion_calls"]
    state["ai"] = {
        "status": "completed",
        "output": [
            {
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "route": "dots",
                                "destination": "dots",
                                "body": "plan my weekend",
                                "reason": "Task",
                            }
                        ),
                    }
                ]
            }
        ],
    }
    row = await add(env, "please plan my weekend")
    assert row["status"] == "waiting_subscription"


async def oauth_flow(env, verifier="v" * 43):
    s, _, c, _, _ = env
    reg = await c.post("/oauth/register", json={"redirect_uris": s.oauth_redirects})
    assert reg.status_code == 201
    client_id = reg.json()["client_id"]
    params = {
        "client_id": client_id,
        "redirect_uri": s.oauth_redirects[0],
        "response_type": "code",
        "code_challenge_method": "S256",
        "code_challenge": pkce(verifier),
        "resource": s.base_url + "/mcp",
        "scope": "dispatch",
        "state": "state-test",
    }
    page = await c.get("/oauth/authorize", params=params)
    assert page.status_code == 200
    handle = re.search('name="handle" value="([^"]+)"', page.text)[1]
    approved = await c.post("/oauth/approve", data={"token": s.admin_token, "handle": handle})
    assert approved.status_code == 303
    query = parse_qs(urlsplit(approved.headers["location"]).query)
    assert query["state"] == ["state-test"]
    return {
        "client_id": client_id,
        "code": query["code"][0],
        "code_verifier": verifier,
        "redirect_uri": s.oauth_redirects[0],
        "resource": s.base_url + "/mcp",
        "grant_type": "authorization_code",
    }


async def test_oauth_pkce_token_rotation_and_revocation(env):
    s, app, c, _, _ = env
    grant = await oauth_flow(env)
    response = await c.post("/oauth/token", data=grant)
    assert response.status_code == 200
    tokens = response.json()
    assert app.state.store.oauth_get("access", digest(tokens["access_token"]))
    assert (await c.post("/oauth/token", data=grant)).status_code == 400
    auth_header = {"Authorization": "Bearer " + tokens["access_token"]}
    result = await c.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "server/discover"}, headers=auth_header
    )
    assert result.json()["result"]["supportedVersions"] == ["2026-07-28"]
    refresh = {
        "grant_type": "refresh_token",
        "client_id": grant["client_id"],
        "refresh_token": tokens["refresh_token"],
        "resource": grant["resource"],
    }
    assert (await c.post("/oauth/token", data=refresh)).status_code == 200
    assert (await c.post("/oauth/token", data=refresh)).status_code == 400
    await c.post("/api/dots/disconnect", json={}, headers=auth(s, True))
    assert (await c.post("/mcp", json={}, headers=auth_header)).status_code == 401


async def test_oauth_wrong_verifier_and_untrusted_redirect(env):
    _, _, c, _, _ = env
    response = await c.post("/oauth/register", json={"redirect_uris": ["https://evil.example/callback"]})
    assert response.status_code == 400
    grant = await oauth_flow(env)
    grant["code_verifier"] = "x" * 43
    assert (await c.post("/oauth/token", data=grant)).status_code == 400


def test_callback_ssrf_and_signing(monkeypatch):
    for url in [
        "http://chatgpt.com/a",
        "https://chatgpt.com.evil/a",
        "https://user@chatgpt.com/a",
        "https://chatgpt.com:444/a",
    ]:
        with pytest.raises(ValueError):
            validate_callback(url, ["chatgpt.com"])
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="non-public"):
        public_addresses("chatgpt.com")
    secret = "whsec_" + base64.b64encode(b"s" * 32).decode()
    assert signed_headers(secret, "id", "sub", b"body", 1) != signed_headers(secret, "id", "sub", b"body", 2)


async def test_restart_recovery_and_expired_subscription(env):
    _, app, _, _, _ = env
    store = app.state.store
    row, _ = store.add("note")
    store.update(row["id"], status="saving")
    store.recover()
    assert store.get(row["id"])["status"] == "needs_review"
    store.save_subscription("expired", "owner", "https://chatgpt.com/test", "test-secret", time.time() - 1)
    assert store.subscription() is None


async def test_review_can_be_resolved_only_once(env):
    s, app, c, _, _ = env
    row = await add(env, "note to missing: keep this")
    path = f"/api/requests/{row['id']}/resolve"
    assert (
        await c.post(path, json={"route": "note", "destination": "missing"}, headers=auth(s, True))
    ).status_code == 400
    valid = {"route": "note", "destination": "ideas"}
    assert (await c.post(path, json=valid, headers=auth(s, True))).status_code == 200
    assert (await c.post(path, json=valid, headers=auth(s, True))).status_code == 409
    await app.state.worker.tick()
    assert app.state.store.get(row["id"])["status"] == "completed"


async def test_malformed_utf8_is_rejected(env):
    s, _, c, _, _ = env
    response = await c.post(
        "/webhooks/index", content=b"\xff", headers={**auth(s), "Content-Type": "application/json"}
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "choice,confidence,status,destination",
    [
        ("note:ideas", 0.98, "completed", "ideas"),
        ("dots", 0.97, "waiting_subscription", "dots"),
        ("review", 0.99, "needs_review", ""),
        ("note:ideas", 0.4, "needs_review", ""),
        ("note:unknown", 0.99, "needs_review", ""),
        ("dots", True, "needs_review", ""),
        ("dots", "0.99", "needs_review", ""),
        ("dots", 1.1, "needs_review", ""),
    ],
)
async def test_jev_routes_and_rejects_uncertain_or_invalid_choices(
    env, choice, confidence, status, destination
):
    s, app, _, _, state = env
    s.router_mode, s.typesafe_key = "jev", "test-key"
    state["ai"] = {"answers": {"destination": {"type": "choice", "choice": choice, "confidence": confidence}}}
    text = "A travel journal that collects voice entries — 旅遊筆記"
    row = await add(env, text)
    assert row["status"] == status and (row["destination"] or "") == destination
    if status != "needs_review":
        assert row["body"] == text
    else:
        assert not state["notion_calls"]


async def test_jev_request_contract_and_explicit_command_bypass(env):
    from app.router import Router

    s, _, _, _, _ = env
    s.router_mode, s.typesafe_key = "jev", "test-key"
    s.destinations["dots"] = "c" * 32
    s.destination_descriptions = {"ideas": "Product ideas to save for later"}
    calls = []

    def upstream(request):
        calls.append(request)
        assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
        assert request.headers["Authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload["state"] == "Remember my idea"
        assert payload["model"] == "jev-latest"
        criteria = payload["questions"]["destination"]["criteria"]
        assert set(criteria) == {"note:inbox", "note:ideas", "note:dots", "dots", "review"}
        assert criteria["note:ideas"] == s.destination_descriptions["ideas"]
        return httpx.Response(
            200,
            json={
                "answers": {
                    "destination": {
                        "type": "choice",
                        "choice": "note:dots",
                        "confidence": 0.9,
                    }
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        router = Router(s, client)
        assert (await router.decide("Dots, research trains")).route == "dots"
        assert (await router.decide("Note to ideas: a travel journal")).route == "note"
        assert (await router.decide("Note to missing: retain this")).route == "review"
        assert not calls
        result = await router.decide("Remember my idea")
        assert result.route == "note" and result.destination == "dots"
        assert len(calls) == 1


@pytest.mark.parametrize(
    "text,body",
    [
        (
            "Hey Hiro, check eBay and Alibaba delivery before my trip",
            "check eBay and Alibaba delivery before my trip",
        ),
        ("Hi, Kiro: check my email and calendar", "check my email and calendar"),
        ("Tell Kiro to find my receipt", "to find my receipt"),
        ("Dots, research trains", "research trains"),
    ],
)
async def test_agent_aliases_route_without_classifier(env, text, body):
    s, _, _, _, _ = env
    s.router_mode, s.typesafe_key = "jev", "test-key"
    # No classifier response is configured: success requires the explicit route.
    row = await add(env, text)
    assert row["route"] == "dots" and row["status"] == "waiting_subscription"
    assert row["body"] == body and row["transcript"] == text


async def test_agent_aliases_are_configurable_and_do_not_match_name_fragments(env):
    from app.router import Router

    s, _, _, _, _ = env
    s.dots_agent_aliases = ["Ada"]

    def unexpected_call(request):
        pytest.fail("Rules routing must not call a classifier")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_call)) as client:
        router = Router(s, client)
        assert (await router.decide("Hey Ada, find my meeting")).route == "dots"
        for text in ["Adapt the design later", "Hiroshima is on my travel list", "Kiro is my assistant"]:
            assert (await router.decide(text)).route == "note"
        assert (await router.decide("Note to inbox: ask Ada to check email later")).route == "note"
        assert (await router.decide("Hey Ada")).route == "review"


@pytest.mark.parametrize("choice,confidence", [("review", 0.99), ("dots", 0.4)])
async def test_jev_review_reason_distinguishes_choice_from_low_confidence(env, choice, confidence):
    s, _, _, _, state = env
    s.router_mode, s.typesafe_key = "jev", "test-key"
    state["ai"] = {"answers": {"destination": {"type": "choice", "choice": choice, "confidence": confidence}}}
    row = await add(env, "Check my order")
    assert row["status"] == "needs_review"
    assert f"Jev selected {choice} (confidence {confidence:.2f})" in row["detail"]
    assert ("below threshold 0.75" in row["detail"]) == (confidence < 0.75)


@pytest.mark.parametrize("failure", ["timeout", 401, 429, 503, "malformed"])
async def test_jev_failures_preserve_request_for_review(env, failure):
    from app.router import Router

    s, app, _, _, state = env
    s.router_mode, s.typesafe_key = "jev", "test-key"

    def upstream(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("private upstream details", request=request)
        if failure == "malformed":
            return httpx.Response(200, json={"answers": {}})
        return httpx.Response(failure, text="private upstream details")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        app.state.worker.router = Router(s, client)
        row = await add(env, "Please plan my trip")
    assert row["status"] == "needs_review"
    assert row["transcript"] == "Please plan my trip"
    assert "private" not in row["detail"]
    assert not state["notion_calls"]


def test_jev_settings_from_env_and_validation(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "a" * 40)
    monkeypatch.setenv("INDEX_TOKEN", "i" * 40)
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ROUTER_MODE", "jev")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("JEV_API_KEY", "test-alias")
    assert Settings.from_env().typesafe_key == "test-alias"
    monkeypatch.setenv("DOTS_AGENT_ALIASES", "Ada, Adah")
    assert Settings.from_env().dots_agent_aliases == ["Ada", "Adah"]
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-canonical")
    assert Settings.from_env().typesafe_key == "test-canonical"
    for invalid in ["nan", "inf", "-0.1", "1.1"]:
        monkeypatch.setenv("JEV_MIN_CONFIDENCE", invalid)
        with pytest.raises(ValueError, match="JEV_MIN_CONFIDENCE"):
            Settings.from_env()
    monkeypatch.setenv("JEV_MIN_CONFIDENCE", "0.75")
    monkeypatch.setenv("NOTE_DESTINATION_DESCRIPTIONS", '{"missing":"A description"}')
    with pytest.raises(ValueError, match="NOTE_DESTINATION_DESCRIPTIONS"):
        Settings.from_env()
    monkeypatch.delenv("NOTE_DESTINATION_DESCRIPTIONS")
    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.delenv("JEV_API_KEY")
    with pytest.raises(ValueError, match="Jev routing requires"):
        Settings.from_env()


async def test_mcp_diagnostics_exclude_params_and_credentials(env, caplog):
    import logging

    _, app, c, _, _ = env
    token = "diagnostic-access-token"
    app.state.store.oauth_put("client", "diagnostic-client", {"redirects": []}, 60)
    app.state.store.oauth_put(
        "access",
        digest(token),
        {"client_id": "diagnostic-client", "resource": "https://dispatch.example/mcp"},
        60,
    )
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    for method, params in [
        ("events/list", {}),
        ("events/subscribe", {"secret": "private-signing-secret", "text": "private-voice-note"}),
        ("private-method-name", {}),
    ]:
        r = await c.post(
            "/mcp",
            headers={"Authorization": "Bearer " + token},
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        assert r.status_code == 200
    assert "MCP operation=events/list succeeded" in caplog.text
    assert "MCP operation=events/subscribe rpc_error=-32602" in caplog.text
    assert "MCP operation=unknown rpc_error=-32601" in caplog.text
    for private in [token, "private-signing-secret", "private-voice-note", "private-method-name"]:
        assert private not in caplog.text


async def test_subscription_rejection_diagnostics_are_specific_and_private(env, caplog):
    _, app, _, _, _ = env
    params = subscription_params()
    params["delivery"]["url"] = "https://callbacks.example.com/private-path?token=private-query"
    with pytest.raises(Exception, match="Invalid callback URL"):
        await app.state.mcp.call("events/subscribe", params, "owner")
    assert "callback host=callbacks.example.com" in caplog.text
    assert "private-path" not in caplog.text and "private-query" not in caplog.text
    caplog.clear()
    params = subscription_params("private-secret")
    with pytest.raises(Exception, match="Invalid signing secret"):
        await app.state.mcp.call("events/subscribe", params, "owner")
    assert "signing secret format" in caplog.text and "private-secret" not in caplog.text
    params = subscription_params()
    params["ttlMs"] = -1
    with pytest.raises(Exception, match="Invalid subscription lifetime"):
        await app.state.mcp.call("events/subscribe", params, "owner")


async def report(app, task_id, status, result, owner="owner"):
    return await app.state.mcp.call(
        "tools/call",
        {
            "name": "report_task_result",
            "arguments": {"task_id": task_id, "status": status, "result": result},
        },
        owner,
    )


async def test_acknowledgment_lifecycle_and_deadlines(env, monkeypatch):
    s, app, client, cb, _ = env
    await app.state.mcp.call("events/subscribe", subscription_params(), "owner")
    row = await add(env, "Dots, check my calendar")
    store, rid = app.state.store, row["id"]
    sent = row["delivered_at"]
    assert sent is not None and row["acknowledged_at"] is None
    assert store.view(row, sent + 299)["display_status"] == "awaiting_ack"
    assert store.view(row, sent + 300)["display_status"] == "no_ack"
    get = await app.state.mcp.call("tools/call", {"name": "get_task", "arguments": {"task_id": rid}}, "owner")
    assert json.loads(get["content"][0]["text"])["acknowledged_at"] is None
    # Reading is not an acknowledgment. Explicit progress is required.
    monkeypatch.setattr(time, "time", lambda: sent + 310)
    response = await client.get("/api/requests", headers=auth(s, admin=True))
    assert response.json()["requests"][0]["attention"] is True
    await app.state.worker.tick()
    assert len(cb.calls) == 2  # Verification and original delivery only; no timeout resend.
    await report(app, rid, "in_progress", "Checking calendar")
    started = store.get(rid)
    assert started["acknowledged_at"] == sent + 310
    assert store.view(started)["display_status"] == "in_progress"
    assert store.view(started, sent + 2110)["display_status"] == "stalled"
    await report(app, rid, "in_progress", "Checking calendar")
    assert store.get(rid) == started
    monkeypatch.setattr(time, "time", lambda: sent + 400)
    await report(app, rid, "needs_input", "Which day?")
    waiting = store.get(rid)
    assert store.view(waiting, sent + 99999)["display_status"] == "needs_input"
    assert store.view(waiting)["attention"] is True
    monkeypatch.setattr(time, "time", lambda: sent + 500)
    await report(app, rid, "in_progress", "Checking Friday")
    await report(app, rid, "completed", "Replied with available times")
    finished = store.get(rid)
    assert finished["finished_at"] == sent + 500
    assert finished["acknowledged_at"] == sent + 310
    assert not store.view(finished)["attention"]
    await report(app, rid, "completed", "Replied with available times")
    await report(app, rid, "in_progress", "Late duplicate acknowledgment")
    assert store.get(rid) == finished
    with pytest.raises(Exception, match="different final result"):
        await report(app, rid, "failed", "Conflicting final outcome")
    reopened = Store(s)
    reopened.recover()
    assert reopened.get(rid) == finished
    reopened.close()


async def pending_tasks(app, owner="owner", **arguments):
    result = await app.state.mcp.call(
        "tools/call", {"name": "list_pending_tasks", "arguments": arguments}, owner
    )
    return json.loads(result["content"][0]["text"])


async def test_missing_event_payload_recovers_without_resend_or_read_ack(env):
    _, app, _, callbacks, _ = env
    await app.state.mcp.call("events/subscribe", subscription_params(), "owner")
    row = await add(env, "Dots, calculate 17 plus 26")
    # The agent was woken without an event ID. Discover it through the durable inbox.
    recovered = await pending_tasks(app)
    assert [task["task_id"] for task in recovered["tasks"]] == [row["id"]]
    assert recovered["tasks"][0]["text"] == "calculate 17 plus 26"
    assert recovered["tasks"][0]["acknowledged_at"] is None
    assert app.state.store.get(row["id"]) == row
    await report(app, row["id"], "in_progress", "Calculating")
    await report(app, row["id"], "completed", "43")
    assert (await pending_tasks(app)) == {"tasks": [], "next_after_task_id": None}
    await app.state.worker.tick()
    assert len(callbacks.calls) == 2  # Subscription verification and one event.


async def test_pending_tasks_owner_filters_and_pagination_after_completion(env):
    _, app, _, _, _ = env
    await app.state.mcp.call("events/subscribe", subscription_params(), "owner")
    store = app.state.store
    rows = [await add(env, f"Dots, request {i}") for i in range(4)]
    await report(app, rows[1]["id"], "in_progress", "Working")
    await report(app, rows[2]["id"], "needs_input", "Which language?")
    store.update(rows[3]["id"], status="delivering")
    # A different OAuth client, unassigned tasks, notes, and final results stay private/absent.
    store.save_subscription(
        "other-sub", "other", "https://chatgpt.com/events/other", "test", time.time() + 60
    )
    for status in ["queued", "ready", "waiting_subscription", "needs_review", "completed", "failed"]:
        excluded, _ = store.add(f"Dots, excluded {status}")
        store.update(excluded["id"], status=status, route="dots", subscription_id=rows[0]["subscription_id"])
    other, _ = store.add("Dots, private other task")
    store.update(other["id"], subscription_id="other-sub", route="dots", status="delivered")
    note, _ = store.add("Note to inbox: private note")
    store.update(note["id"], subscription_id=rows[0]["subscription_id"], route="note", status="delivered")
    first = await pending_tasks(app, limit=2)
    assert [t["task_id"] for t in first["tasks"]] == [r["id"] for r in rows[:2]]
    assert first["next_after_task_id"] == rows[1]["id"]
    for row in rows[:2]:
        await report(app, row["id"], "completed", "Done")
    second = await pending_tasks(app, limit=2, after_task_id=first["next_after_task_id"])
    assert [t["task_id"] for t in second["tasks"]] == [r["id"] for r in rows[2:]]
    assert [t["status"] for t in second["tasks"]] == ["needs_input", "delivering"]
    assert second["next_after_task_id"] is None
    assert (await pending_tasks(app, "stranger"))["tasks"] == []
    assert [t["task_id"] for t in (await pending_tasks(app, "other"))["tasks"]] == [other["id"]]
    with pytest.raises(Exception, match="Task not found for this connection"):
        await pending_tasks(app, after_task_id=other["id"])


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": 0},
        {"limit": 21},
        {"limit": True},
        {"limit": "2"},
        {"limit": 1.5},
        {"after_task_id": ""},
        {"after_task_id": 42},
        {"unexpected": True},
    ],
)
async def test_pending_tasks_rejects_invalid_queries(env, arguments):
    with pytest.raises(Exception, match="Invalid pending-task query"):
        await pending_tasks(env[1], **arguments)


@pytest.mark.parametrize("outcome", [200, 503, "timeout"])
@pytest.mark.parametrize("agent_status", ["in_progress", "needs_input", "completed", "failed"])
async def test_agent_report_wins_webhook_race(env, outcome, agent_status):
    _, app, _, cb, _ = env
    await app.state.mcp.call("events/subscribe", subscription_params(), "owner")

    async def early_ack(url, body, headers):
        task_id = json.loads(body)["data"]["task_id"]
        await report(app, task_id, agent_status, "Agent update")
        if outcome == "timeout":
            raise TimeoutError()
        return outcome, b""

    cb.post = early_ack
    row = await add(env, "Dots, check a shipment")
    assert row["status"] == agent_status
    assert row["acknowledged_at"] is not None
    assert row["attempts"] == 0
    assert not app.state.store.pending()


async def test_progress_reports_require_owner_and_nonempty_summary(env):
    _, app, _, _, _ = env
    await app.state.mcp.call("events/subscribe", subscription_params(), "owner")
    row = await add(env, "Dots, find a receipt")
    with pytest.raises(Exception, match="Task not found"):
        await report(app, row["id"], "in_progress", "Working", owner="other")
    for state, summary in [("started", "Working"), ("in_progress", " "), ("needs_input", "x" * 4001)]:
        with pytest.raises(Exception, match="Invalid task result"):
            await report(app, row["id"], state, summary)
    assert app.state.store.get(row["id"])["acknowledged_at"] is None


async def test_old_database_migrates_without_inventing_acknowledgments(env):
    s, app, _, _, _ = env
    row = await add(env, "Dots, legacy request")
    store = app.state.store
    store.update(row["id"], status="delivered", detail="Received by ChatGPT; awaiting task result")
    for column in ("delivered_at", "acknowledged_at", "progress_at", "finished_at"):
        store.db.execute(f"ALTER TABLE requests DROP COLUMN {column}")
    reopened = Store(s)
    legacy = reopened.get(row["id"])
    assert legacy["delivered_at"] is None and legacy["acknowledged_at"] is None
    assert reopened.view(legacy)["display_status"] == "unconfirmed"
    assert reopened.view(legacy)["attention"] is True
    assert not reopened.pending()
    reopened.close()


async def test_cached_get_task_schema_can_recover_and_page_pending_tasks(env):
    _, app, _, callbacks, _ = env
    await app.state.mcp.call("events/subscribe", subscription_params(), "owner")
    rows = [await add(env, f"Dots, cached catalog test {i}") for i in range(11)]
    before = [app.state.store.get(r["id"]) for r in rows]

    async def lookup(selector, owner="owner"):
        result = await app.state.mcp.call(
            "tools/call", {"name": "get_task", "arguments": {"task_id": selector}}, owner
        )
        return json.loads(result["content"][0]["text"])

    first = await lookup("pending")
    assert first == await pending_tasks(app)
    assert len(first["tasks"]) == 10
    await report(app, first["next_after_task_id"], "completed", "Done")
    second = await lookup("pending:" + first["next_after_task_id"])
    assert [task["task_id"] for task in second["tasks"]] == [rows[-1]["id"]]
    assert second["next_after_task_id"] is None
    assert await lookup("pending", "stranger") == {"tasks": [], "next_after_task_id": None}
    with pytest.raises(Exception, match="Task not found for this connection"):
        await lookup("pending:" + rows[0]["id"], "stranger")
    with pytest.raises(Exception, match="Invalid pending-task query"):
        await lookup("pending:")
    with pytest.raises(Exception, match="Task not found for this connection"):
        await report(app, "pending", "completed", "Cannot mutate a selector")
    assert [app.state.store.get(r["id"]) for r in rows[:9]] == before[:9]
    assert len(callbacks.calls) == 12  # Reads did not resend any events.
