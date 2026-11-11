"""Testes do backend com a API da Tencent simulada (não usa rede nem chave real)."""
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import main  # noqa: E402


def ok(data):
    return httpx.Response(200, json={"code": 0, "message": "ok", "requestId": "r1", "data": data})


def fail(code, message):
    return httpx.Response(200, json={"code": code, "message": message, "requestId": "r1", "data": {}})


@pytest.fixture
def client(monkeypatch, tmp_path):
    calls = []
    state = {"palms": {}, "session": None}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.path, body, request.headers.get("authorization")))
        path = request.url.path
        if path == main.PATH_CREATE_TOKEN:
            return ok({"Token": "sut_abc", "ExpiresAt": 1783691400})
        if path == main.PATH_GET_PALM:
            palms = state["palms"].get(body["UserId"])
            return ok(palms) if palms else fail(2080009, "data not found")
        if path == main.PATH_GET_SESSION:
            return ok(state["session"]) if state["session"] else fail(2080024, "compare session expired or not exist")
        if path == main.PATH_DELETE_PALM:
            state["palms"].pop(body["UserId"], None)
            return ok({})
        return httpx.Response(404)

    monkeypatch.setenv("PALM_API_KEY", "ak_test")
    monkeypatch.setattr(main, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(main, "CONSENT_LOG", tmp_path / "consent.jsonl")
    c = TestClient(main.app)
    c.calls, c.state = calls, state
    return c


def test_token_registration_requires_consent(client):
    r = client.post("/api/token", json={"userId": "u1", "purpose": "registration"})
    assert r.status_code == 400
    assert client.calls == []


def test_token_registration_logs_consent_and_hides_api_key(client):
    r = client.post("/api/token", json={"userId": "u1", "purpose": "registration", "consent": True})
    assert r.status_code == 200
    assert r.json()["token"] == "sut_abc"
    assert "ak_test" not in r.text
    path, body, auth = client.calls[0]
    assert path == main.PATH_CREATE_TOKEN and body["UserId"] == "u1" and auth == "Bearer ak_test"
    assert "AppId" not in body
    log = main.CONSENT_LOG.read_text().strip().splitlines()
    assert len(log) == 1 and json.loads(log[0])["userId"] == "u1"


def test_invalid_user_id_rejected(client):
    r = client.post("/api/token", json={"userId": "joão silva", "purpose": "verification"})
    assert r.status_code == 422


def test_get_palm_empty_when_not_registered(client):
    r = client.get("/api/palm/u1")
    assert r.status_code == 200
    assert r.json() == {"userId": "u1", "left": None, "right": None}


def test_get_palm_maps_states(client):
    client.state["palms"]["u1"] = {"LeftPalm": {"PalmState": 1, "PalmDirection": 1, "RegisterTime": 1783600000}}
    r = client.get("/api/palm/u1").json()
    assert r["left"] == {"ativa": True, "altaSimilaridade": False, "registradaEm": 1783600000}
    assert r["right"] is None


def test_verification_confirmed_only_when_user_and_match(client):
    client.state["session"] = {
        "UserId": "u1", "PalmId": "p", "PalmDirection": 1,
        "SearchResult": {"IsMatch": True, "Score": 92, "AlgorithmVersion": "rgb_v3"},
    }
    good = client.post("/api/session/confirm", json={"sessionId": "s", "userId": "u1", "purpose": "verification"}).json()
    assert good["confirmado"] is True and good["score"] == 92 and good["palma"] == "left"

    other = client.post("/api/session/confirm", json={"sessionId": "s", "userId": "u2", "purpose": "verification"}).json()
    assert other["confirmado"] is False

    client.state["session"]["SearchResult"]["IsMatch"] = False
    nomatch = client.post("/api/session/confirm", json={"sessionId": "s", "userId": "u1", "purpose": "verification"}).json()
    assert nomatch["confirmado"] is False


def test_recognition_returns_user_from_platform_without_user_id(client):
    client.state["session"] = {
        "UserId": "u7", "PalmId": "p", "PalmDirection": 2,
        "SearchResult": {"IsMatch": True, "Score": 95, "AlgorithmVersion": "rgb_v3"},
    }
    r = client.post("/api/session/confirm", json={"sessionId": "s", "purpose": "recognition"}).json()
    assert r["confirmado"] is True and r["userId"] == "u7" and r["palma"] == "right"

    client.state["session"]["SearchResult"]["IsMatch"] = False
    r = client.post("/api/session/confirm", json={"sessionId": "s", "purpose": "recognition"}).json()
    assert r["confirmado"] is False and r["userId"] is None


def test_verification_still_requires_user_id(client):
    r = client.post("/api/session/confirm", json={"sessionId": "s", "purpose": "verification"})
    assert r.status_code == 422


def test_registration_confirm_returns_palms(client):
    client.state["session"] = {"UserId": "u1", "PalmId": "p", "PalmDirection": 2, "RegisterResult": {}}
    client.state["palms"]["u1"] = {"RightPalm": {"PalmState": 1, "PalmDirection": 2, "RegisterTime": 1}}
    r = client.post("/api/session/confirm", json={"sessionId": "s", "userId": "u1", "purpose": "registration"}).json()
    assert r["confirmado"] is True and r["palmas"]["right"]["ativa"] is True


def test_expired_session_surfaces_palm_error(client):
    r = client.post("/api/session/confirm", json={"sessionId": "s", "userId": "u1", "purpose": "verification"})
    assert r.status_code == 502 and r.json()["codigo"] == 2080024


def test_delete_sends_direction_list(client):
    r = client.request("DELETE", "/api/palm/u1", json={"directions": ["right", "left"]})
    assert r.status_code == 200
    path, body, _ = client.calls[-1]
    assert path == main.PATH_DELETE_PALM and body == {"UserId": "u1", "PalmDirectionList": [1, 2]}


def test_missing_api_key(client, monkeypatch):
    monkeypatch.delenv("PALM_API_KEY")
    r = client.get("/api/palm/u1")
    assert r.status_code == 503
