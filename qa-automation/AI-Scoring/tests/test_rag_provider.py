"""P1 — Pulpo provider against a mocked MCP transport (PulpoConnection
§4.1): handshake, session echo, JSON + SSE response parsing, neutral
shape mapping at the boundary, order-aligned batches, factory gating."""

from __future__ import annotations

import json

import httpx
import pytest

from backend.services.rag import factory
from backend.services.rag.provider import RagProviderError
from backend.services.rag.pulpo import PulpoProvider, _parse_rpc_response

URL = "https://pulpo.test/api/mcp"

_SEARCH_PAYLOAD = {
    "batches": [
        {
            "query": "Access & Entry — Smart-lock failure",
            "degraded": False,
            "results": [
                {
                    "id": "doc-1", "title": "Smart Lock Troubleshooting",
                    "excerpt": "When a member reports...", "tags": ["member_support"],
                    "score": 0.81, "score_type": "cosine",
                    "last_verified": "2026-07-01", "open_flag_count": 1,
                },
                {
                    "id": "doc-2", "title": "Lockbox Fallback",
                    "excerpt": "If the smart lock...", "tags": [],
                    "score": 0.44, "score_type": "cosine",
                },
            ],
        },
    ],
}

# Live shape (verified 2026-07-22): get_document nests under "document".
_DOC_PAYLOAD = {
    "document": {
        "id": "doc-1", "title": "Smart Lock Troubleshooting",
        "body": "Step 1...", "tags": ["member_support"],
        "open_flags": [
            {"quote": "reboot the lock", "body": "outdated for gen-3 locks",
             "suggestion": "replace with hub reset", "anchor_status": "anchored"},
        ],
    },
}


def _mcp_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    method = body.get("method")
    if method == "initialize":
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"],
                  "result": {"protocolVersion": "2025-03-26", "capabilities": {}}},
            headers={"Mcp-Session-Id": "sess-42"},
        )
    if method == "notifications/initialized":
        return httpx.Response(202)
    if method == "tools/call":
        # Every post-handshake call must echo the session id.
        assert request.headers.get("Mcp-Session-Id") == "sess-42"
        tool = body["params"]["name"]
        args = body["params"]["arguments"]
        if tool == "search_knowledge_base":
            assert args["rerank"] is False  # §4.2 stance, enforced at transport
            payload = _SEARCH_PAYLOAD
        elif tool == "get_document":
            payload = _DOC_PAYLOAD if args["id"] == "doc-1" else {"error": "not found"}
        else:
            payload = {"ok": True}
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"],
                  "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}},
        )
    return httpx.Response(400)


@pytest.fixture
def provider() -> PulpoProvider:
    p = PulpoProvider(URL, "pk_test")
    p._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_mcp_handler),
        headers={"Authorization": "Bearer pk_test"},
    )
    return p


@pytest.mark.asyncio
async def test_search_maps_neutral_hits(provider: PulpoProvider):
    (hits,) = await provider.search(["Access & Entry — Smart-lock failure"])
    assert [h.id for h in hits] == ["doc-1", "doc-2"]
    top = hits[0]
    assert top.title == "Smart Lock Troubleshooting"
    assert top.score == 0.81
    assert top.score_kind == "cosine"
    assert top.tags == ("member_support",)
    assert top.flag_count == 1
    # Nothing Pulpo-shaped escapes: neutral dataclass, no score_type attr.
    assert not hasattr(top, "score_type")


@pytest.mark.asyncio
async def test_search_order_aligned_with_queries(provider: PulpoProvider):
    """A query Pulpo dropped yields [] in ITS position, not a shifted list."""
    batches = await provider.search(
        ["no-batch-for-this", "Access & Entry — Smart-lock failure"]
    )
    assert batches[0] == []
    assert [h.id for h in batches[1]] == ["doc-1", "doc-2"]


@pytest.mark.asyncio
async def test_get_document_maps_flags(provider: PulpoProvider):
    doc = await provider.get_document("doc-1")
    assert doc is not None
    assert doc.body == "Step 1..."
    (flag,) = doc.flags
    assert flag.quote == "reboot the lock"
    assert flag.note == "outdated for gen-3 locks"
    assert flag.suggestion == "replace with hub reset"


@pytest.mark.asyncio
async def test_handshake_runs_once(provider: PulpoProvider):
    await provider.search(["q"])
    assert provider._initialized and provider._session_id == "sess-42"
    await provider.search(["q"])  # second call: no re-handshake assertion trips


def test_parse_rpc_response_sse():
    """Streamable-HTTP servers may answer POSTs as short SSE streams."""
    resp = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=(
            "event: message\n"
            'data: {"jsonrpc":"2.0","method":"ping"}\n\n'
            'data: {"jsonrpc":"2.0","id":2,"result":{"ok":true}}\n\n'
        ),
    )
    message = _parse_rpc_response(resp)
    assert message == {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}}


def test_parse_rpc_response_garbage_raises():
    resp = httpx.Response(200, headers={"content-type": "text/html"}, text="<html>")
    with pytest.raises(RagProviderError):
        _parse_rpc_response(resp)


@pytest.mark.asyncio
async def test_tool_error_raises_provider_error(provider: PulpoProvider):
    async def failing_post(body):
        return httpx.Response(500, text="boom")
    provider._initialized = True
    provider._post = failing_post  # type: ignore[method-assign]
    with pytest.raises(RagProviderError):
        await provider.search(["q"])


# -- factory gating ---------------------------------------------------------


def test_factory_none_mode(monkeypatch):
    factory.reset_for_tests()
    monkeypatch.setenv("RAG_PROVIDER", "none")
    assert factory.get_rag_provider() is None
    factory.reset_for_tests()


def test_factory_pulpo_without_env_disables(monkeypatch):
    factory.reset_for_tests()
    monkeypatch.delenv("RAG_PROVIDER", raising=False)
    monkeypatch.delenv("PULPO_MCP_URL", raising=False)
    monkeypatch.delenv("PULPO_MCP_TOKEN", raising=False)
    assert factory.get_rag_provider() is None
    factory.reset_for_tests()


def test_factory_pulpo_with_env(monkeypatch):
    factory.reset_for_tests()
    monkeypatch.delenv("RAG_PROVIDER", raising=False)
    monkeypatch.setenv("PULPO_MCP_URL", URL)
    monkeypatch.setenv("PULPO_MCP_TOKEN", "pk_test")
    provider = factory.get_rag_provider()
    assert provider is not None and provider.name == "pulpo"
    assert factory.get_rag_provider() is provider  # singleton
    factory.reset_for_tests()
