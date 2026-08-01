"""Pulpo provider — the ONLY Pulpo-shaped module (PulpoConnection §4.1).

Speaks MCP over Streamable HTTP against `PULPO_MCP_URL`
(https://plasticity.heypulpo.com/api/mcp): an `initialize` handshake,
then `tools/call` JSON-RPC POSTs, Bearer-authenticated. No MCP SDK for
four tool calls — a small client keeps the image lean and the Sandy
port trivial (the TS side swaps this module for the native SDK).

Streamable-HTTP servers may answer a POST either as plain JSON or as a
short SSE stream; `_parse_rpc_response` handles both. A session id
(`Mcp-Session-Id` response header) is captured at initialize and echoed
on every later call; a 404 (expired session) re-initializes once.

Pulpo shapes are mapped into the neutral provider types AT THIS
BOUNDARY — `batches[].results[]`, `score_type`, `open_flags`,
`anchor_status` never escape this file.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx

from backend.services.rag.provider import (
    RagDoc,
    RagFlag,
    RagHit,
    RagProvider,
    RagProviderError,
    RagUpsertResult,
)

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2025-03-26"
_CLIENT_INFO = {"name": "landing-qa-scoring", "version": "1.0"}

CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 8.0

# Pulpo's search_knowledge_base rejects query strings over 500 chars
# (server-side zod cap, observed 2026-07-24). Longer queries — e.g. the
# transcript-head disposition fallback — are clamped here so the limit
# never escapes this boundary.
QUERY_MAX_CHARS = 500


def _parse_rpc_response(resp: httpx.Response) -> Optional[dict]:
    """JSON-RPC response body → dict. Handles both plain JSON and SSE
    (`data:` lines; the last data event carrying a JSON-RPC id wins).
    Returns None for empty/accepted-only bodies (notifications)."""
    content_type = resp.headers.get("content-type", "")
    text = resp.text.strip()
    if not text:
        return None
    if "text/event-stream" in content_type:
        message = None
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                candidate = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and ("result" in candidate or "error" in candidate):
                message = candidate
        return message
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RagProviderError(f"pulpo: non-JSON response ({content_type}): {text[:200]}") from e


class PulpoProvider(RagProvider):
    name = "pulpo"

    def __init__(self, url: str, token: str):
        self._url = url
        self._session_id: Optional[str] = None
        self._initialized = False
        self._next_id = 0
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        )

    # -- transport -----------------------------------------------------

    async def _post(self, body: dict) -> httpx.Response:
        headers = {}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            return await self._client.post(self._url, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise RagProviderError(f"pulpo: transport error: {e}") from e

    async def _initialize(self) -> None:
        self._next_id += 1
        resp = await self._post({
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        })
        if resp.status_code >= 400:
            raise RagProviderError(
                f"pulpo: initialize failed HTTP {resp.status_code}: {resp.text[:200]}"
            )
        message = _parse_rpc_response(resp)
        if message is None or "error" in message:
            raise RagProviderError(f"pulpo: initialize rejected: {message}")
        self._session_id = resp.headers.get("mcp-session-id") or self._session_id
        # Per spec the client acks with an initialized notification.
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    async def _tool_call(self, tool: str, arguments: dict) -> Any:
        """tools/call → the tool's payload (structuredContent preferred,
        else first text content block parsed as JSON, else raw text)."""
        if not self._initialized:
            await self._initialize()
        self._next_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        resp = await self._post(body)
        if resp.status_code == 404 and self._session_id:
            # Session expired server-side — re-handshake once and retry.
            self._initialized, self._session_id = False, None
            await self._initialize()
            resp = await self._post(body)
        if resp.status_code >= 400:
            raise RagProviderError(
                f"pulpo: {tool} failed HTTP {resp.status_code}: {resp.text[:200]}"
            )
        message = _parse_rpc_response(resp)
        if message is None:
            raise RagProviderError(f"pulpo: {tool} returned no response message")
        if "error" in message:
            raise RagProviderError(f"pulpo: {tool} error: {message['error']}")
        result = message.get("result") or {}
        if result.get("isError"):
            raise RagProviderError(f"pulpo: {tool} tool error: {result}")
        if "structuredContent" in result:
            return result["structuredContent"]
        for block in result.get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return result

    async def raw_tool_call(self, tool: str, arguments: dict) -> Any:
        """Raw MCP tool passthrough for OPERATOR tooling (compendium
        snapshot, admin audits) that needs Pulpo fields the neutral
        types deliberately drop (created_at, owner, review cadence…).
        Policy code must keep using the RagProvider surface."""
        return await self._tool_call(tool, arguments)

    # -- shape mapping (Pulpo → neutral) -------------------------------

    @staticmethod
    def _map_hit(raw: dict) -> RagHit:
        return RagHit(
            id=str(raw.get("id", "")),
            title=raw.get("title", "") or "",
            excerpt=raw.get("excerpt", "") or "",
            score=float(raw.get("score") or 0.0),
            score_kind=raw.get("score_type", "") or "",
            tags=tuple(raw.get("tags") or ()),
            updated_at=str(raw.get("last_verified") or raw.get("updated_at") or ""),
            flag_count=int(raw.get("open_flag_count") or 0),
        )

    @staticmethod
    def _map_doc(raw: dict) -> RagDoc:
        flags = tuple(
            RagFlag(
                quote=f.get("quote", "") or "",
                note=f.get("body", "") or "",
                suggestion=f.get("suggestion", "") or "",
            )
            for f in (raw.get("open_flags") or ())
        )
        return RagDoc(
            id=str(raw.get("id", "")),
            title=raw.get("title", "") or "",
            body=raw.get("body", "") or "",
            flags=flags,
            tags=tuple(raw.get("tags") or ()),
            updated_at=str(raw.get("last_verified") or raw.get("updated_at") or ""),
        )

    # -- RagProvider surface -------------------------------------------

    async def search(
        self, queries: list[str], *, limit: int = 5
    ) -> list[list[RagHit]]:
        if not queries:
            return []
        sent = [q[:QUERY_MAX_CHARS] for q in queries]
        # rerank stays False by design (§4.2): Gemini reads full bodies,
        # so Pulpo's precision pass is declined — their own guidance for
        # agents that read the candidates themselves.
        payload = await self._tool_call(
            "search_knowledge_base",
            {"queries": sent, "limit": limit, "rerank": False},
        )
        if not isinstance(payload, dict):
            raise RagProviderError(f"pulpo: unexpected search payload: {payload!r:.200}")
        batches = payload.get("batches") or []
        by_query = {
            b.get("query"): [self._map_hit(r) for r in (b.get("results") or [])]
            for b in batches
        }
        # Order-align with the request (Pulpo echoes the clamped query,
        # so align against `sent`); a query Pulpo dropped yields [].
        return [by_query.get(q, []) for q in sent]

    async def get_document(self, doc_id: str) -> Optional[RagDoc]:
        try:
            payload = await self._tool_call("get_document", {"id": doc_id})
        except RagProviderError as e:
            if "not found" in str(e).lower():
                return None
            raise
        if not isinstance(payload, dict):
            return None
        # Live shape (verified 2026-07-22): the doc nests under "document".
        raw = payload.get("document") if isinstance(payload.get("document"), dict) else payload
        if not raw.get("id"):
            return None
        return self._map_doc(raw)

    @property
    def supports_flagging(self) -> bool:
        return True

    async def flag_document(
        self, doc_id: str, quote: str, *, note: str = "", suggestion: str = ""
    ) -> None:
        args: dict = {"id": doc_id, "quote": quote}
        if note:
            args["body"] = note
        if suggestion:
            args["suggestion"] = suggestion
        await self._tool_call("flag_document", args)

    @property
    def supports_upsert(self) -> bool:
        return True

    async def upsert_document(
        self, *, title: str, body: str, doc_id: Optional[str] = None
    ) -> RagUpsertResult:
        args: dict = {"title": title, "body": body, "private": False}
        if doc_id:
            args["id"] = doc_id
        payload = await self._tool_call("upsert_document", args)
        new_id = str(payload.get("id", "")) if isinstance(payload, dict) else ""
        return RagUpsertResult(id=new_id or (doc_id or ""), created=doc_id is None)

    async def aclose(self) -> None:
        await self._client.aclose()


def build_from_env(token_env: str = "PULPO_MCP_TOKEN") -> Optional[PulpoProvider]:
    """None (with a loud log) when the env isn't configured — retrieval
    is an enhancement, never a boot requirement."""
    url = os.environ.get("PULPO_MCP_URL", "")
    token = os.environ.get(token_env, "")
    if not url or not token:
        logger.warning(
            "pulpo: PULPO_MCP_URL/%s unset — RAG retrieval disabled", token_env
        )
        return None
    return PulpoProvider(url, token)
