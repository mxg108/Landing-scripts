"""/score route tests — extended behavior from PR 3 (LookupToScore.md).

Uses FastAPI's TestClient with monkeypatching of the imports inside
``backend.routes.scoring`` so no real I/O (Dialpad, Sheets, Gemini)
runs. The route's branching is what we're after — auth, idempotency,
audio fallback, audit-row write, 422/503 mapping.

The background task ``run()`` itself is never awaited in these tests;
they assert the route's *synchronous* observable behavior (response,
job-store entry, audit row appended).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.middleware import auth
from backend.middleware.auth import KeyIdentity
from backend.routes import scoring as scoring_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEAM_TOKEN = "team-ms-tok"
PRIV_TOKEN = "priv-tok"


@pytest.fixture
def team_key_identity() -> KeyIdentity:
    return KeyIdentity(role="team", team_id="member_support")


@pytest.fixture
def priv_key_identity() -> KeyIdentity:
    return KeyIdentity(role="privileged", team_id=None)


@pytest.fixture
def client(monkeypatch, team_key_identity, priv_key_identity):
    """FastAPI TestClient with auth and side-effects stubbed.

    Each test gets a fresh ``_jobs`` dict and an empty
    ``append_score_audit_row`` capture list at ``client.audit_rows``.
    """
    # Auth: register both tokens. require_team_access / require_api_key
    # both consult _KEY_MAP via secrets.compare_digest, so dict identity
    # doesn't matter — just the contents.
    monkeypatch.setattr(auth, "_KEY_MAP", {
        TEAM_TOKEN: team_key_identity,
        PRIV_TOKEN: priv_key_identity,
    })

    # Reset module-level job + semaphore state so tests don't leak.
    scoring_module._jobs.clear()
    scoring_module._key_semaphores.clear()

    # Capture audit appends.
    captured: list[dict] = []

    def fake_append(**kwargs):
        captured.append(kwargs)
        return 1

    monkeypatch.setattr(scoring_module, "append_score_audit_row", fake_append)

    # Stub Mails-roster check — declare luis@ in member_support.
    async def fake_email_in_team_mails(email, team_id):
        if not email:
            return False
        return (
            email.lower() == "luis@landing.com"
            and team_id == "member_support"
        )

    async def fake_agent_name_for_email(email, team_id):
        if email and email.lower() == "luis@landing.com":
            return "Luis Rubio"
        return None

    async def fake_agent_email_for_name(name, team_id):
        if name in ("Luis Rubio", "luis"):
            return "luis@landing.com"
        return None

    monkeypatch.setattr(scoring_module, "email_in_team_mails", fake_email_in_team_mails)
    monkeypatch.setattr(scoring_module, "agent_name_for_email", fake_agent_name_for_email)
    monkeypatch.setattr(scoring_module, "agent_email_for_name", fake_agent_email_for_name)

    # Stub Dialpad metadata helpers so the success path doesn't hit the API.
    async def fake_get_transcript(call_id):
        return {"transcript": "(stub)"}

    async def fake_get_call_details(call_id):
        return {"call_id": call_id, "_flagged_long_call": False}

    monkeypatch.setattr(scoring_module, "get_transcript", fake_get_transcript)
    monkeypatch.setattr(scoring_module, "get_call_details", fake_get_call_details)

    # Stub the FastAPI app build so we don't need .env. Build a minimal
    # app that just mounts the scoring router exactly the way main.py does.
    from fastapi import FastAPI
    from backend.middleware.auth import AUTH_DEPENDENCY, TEAM_AUTH_DEPENDENCY

    app = FastAPI()
    app.include_router(
        scoring_module.router,
        prefix="/api/{team_id}",
        dependencies=TEAM_AUTH_DEPENDENCY,
    )
    app.include_router(
        scoring_module.router,
        prefix="/api",
        dependencies=AUTH_DEPENDENCY,
    )

    tc = TestClient(app)
    tc.audit_rows = captured  # attach for assertions
    return tc


# ---------------------------------------------------------------------------
# Audio-fallback path
# ---------------------------------------------------------------------------

def test_score_endpoint_without_audio_fetches_recording(client, monkeypatch):
    """Omitting audio_file triggers download_recording(call_id)."""
    called = {}

    async def fake_download(call_id):
        called["call_id"] = call_id
        return b"FAKERECORDINGBYTES"

    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-abc",
            "agent_email": "luis@landing.com",
            "manager_email": "ana@landing.com",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    assert called == {"call_id": "call-abc"}


def test_score_endpoint_with_audio_skips_download(client, monkeypatch):
    """Supplying audio_file means download_recording must NOT be called."""
    called = {"hit": False}

    async def fake_download(call_id):
        called["hit"] = True
        return b""

    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-abc",
            "agent_email": "luis@landing.com",
            "manager_email": "ana@landing.com",
        },
        files={"audio_file": ("call.mp3", b"REALBYTES", "audio/mpeg")},
    )
    assert resp.status_code == 200, resp.text
    assert called["hit"] is False


def test_score_endpoint_no_recording_returns_422(client, monkeypatch):
    async def fake_download(call_id):
        from backend.services.dialpad_client import NoRecordingAvailable
        raise NoRecordingAvailable("none")

    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-aged-out",
            "agent_email": "luis@landing.com",
            "manager_email": "ana@landing.com",
        },
    )
    assert resp.status_code == 422
    assert "no recording" in resp.json()["detail"].lower()
    # Denial audit row should mention the reason.
    assert any(r["notes"] == "no_recording" for r in client.audit_rows)


def test_score_endpoint_rate_limited_returns_503(client, monkeypatch):
    async def fake_download(call_id):
        from backend.services.dialpad_client import DialpadRateLimited
        raise DialpadRateLimited("429")

    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-throttled",
            "agent_email": "luis@landing.com",
            "manager_email": "ana@landing.com",
        },
    )
    assert resp.status_code == 503
    assert any(r["notes"] == "rate_limited" for r in client.audit_rows)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_score_endpoint_idempotent_on_in_flight_job(client):
    """A POST while a prior job is 'scoring' returns the existing job_id and
    does NOT write a second audit row.

    Pre-seeds the job store so the test doesn't race the background task
    (which TestClient awaits before returning the response).
    """
    # job_id = f"{call_id}_{agent_name}".replace(" ", "_")
    # agent_name resolves from email "luis@landing.com" → "Luis Rubio"
    expected_job_id = "call-double-click_Luis_Rubio"
    scoring_module._jobs[
        scoring_module._job_key("member_support", expected_job_id)
    ] = {"status": "scoring", "call_id": "call-double-click"}

    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-double-click",
            "agent_email": "luis@landing.com",
            "manager_email": "ana@landing.com",
        },
        files={"audio_file": ("c.mp3", b"BYTES", "audio/mpeg")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"job_id": expected_job_id, "status": "scoring"}
    # No audit row written — neither scored nor denied.
    assert client.audit_rows == []


# ---------------------------------------------------------------------------
# Audit row write
# ---------------------------------------------------------------------------

def test_score_endpoint_writes_audit_row_on_success(client, monkeypatch):
    async def fake_download(call_id):
        return b"BYTES"

    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-xyz",
            "agent_email": "luis@landing.com",
            "manager_email": "ana@landing.com",
        },
    )
    assert resp.status_code == 200
    assert len(client.audit_rows) == 1
    row = client.audit_rows[0]
    assert row["action"] == "scored"
    assert row["api_key_role"] == "team"
    assert row["evaluator_email"] == "ana@landing.com"
    assert row["agent_email"] == "luis@landing.com"
    assert row["agent_name"] == "Luis Rubio"  # resolved from email
    assert row["call_id"] == "call-xyz"
    assert row["target_team"] == "member_support"


def test_score_endpoint_writes_denied_audit_on_unrostered_team_key(client, monkeypatch):
    """Team key trying to score an agent outside their roster → 403 + denied row."""
    async def fake_download(call_id):
        return b"BYTES"

    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-foo",
            "agent_email": "stranger@landing.com",  # not in stubbed roster
            "manager_email": "ana@landing.com",
        },
    )
    assert resp.status_code == 403
    assert len(client.audit_rows) == 1
    assert client.audit_rows[0]["action"] == "denied"
    assert client.audit_rows[0]["api_key_role"] == "team"


def test_score_endpoint_privileged_bypasses_roster(client, monkeypatch):
    """Privileged key on unrostered agent → success, audit shows privileged role."""
    async def fake_download(call_id):
        return b"BYTES"

    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    resp = client.post(
        "/api/sales/score",
        headers={"Authorization": f"Bearer {PRIV_TOKEN}"},
        data={
            "call_id": "call-priv",
            "agent_email": "contractor@external.com",
            "agent_name": "External Contractor",
            "manager_email": "hr@landing.com",
        },
    )
    assert resp.status_code == 200, resp.text
    assert client.audit_rows[0]["api_key_role"] == "privileged"
    assert client.audit_rows[0]["target_team"] == "sales"


# ---------------------------------------------------------------------------
# 400 validation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Approve writes the audit row
# ---------------------------------------------------------------------------

def test_approve_writes_audit_row(client, monkeypatch):
    """Engine approval appends a Score_Audit row with action="approved"."""
    # Pre-seed a completed job with the metadata the approve handler reads.
    job_id = "call-approve_Luis_Rubio"
    key = scoring_module._job_key("member_support", job_id)
    scoring_module._jobs[key] = {
        "status": "complete",
        "call_id": "call-approve",
        "agent_email": "luis@landing.com",
        "agent_name": "Luis Rubio",
        "manager_email": "ana@landing.com",
        "sheets_row": 99,
        "evaluation_id": 77,
        "scorecard": {
            "manager_email": "ana@landing.com",
            "agent_name": "Luis Rubio",
            "sections": [],
            "call_summary": "",
            "key_strengths": "",
            "opportunities": "",
        },
    }

    # Stub the engine path (DB transition + projection + Apps Script) so
    # the test never reaches Postgres or gspread.
    monkeypatch.setattr(
        scoring_module.eval_store, "missing_manual_scores",
        lambda config, sections: [],
    )

    async def fake_record_approval(config, **kw):
        return 77
    monkeypatch.setattr(scoring_module, "record_approval", fake_record_approval)

    class _Detail:
        overall_score = 85.0

    async def fake_stamp(evaluation_id, config, evaluator_email):
        return _Detail()
    monkeypatch.setattr(scoring_module.eval_store, "stamp_and_finalize", fake_stamp)

    async def fake_pool():
        return object()
    monkeypatch.setattr(scoring_module.eval_store, "get_pool", fake_pool)

    async def fake_project(pool, evaluation_id, config, include_history=True):
        return 555
    monkeypatch.setattr(scoring_module, "project_evaluation", fake_project)
    monkeypatch.setattr(
        scoring_module, "trigger_apps_script",
        lambda row, team_id, disclaimer=None: {"status": "ok"},
    )

    resp = client.post(
        f"/api/member_support/score/{job_id}/approve",
        headers={
            "Authorization": f"Bearer {TEAM_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "sections": [],
            "key_strengths": "x",
            "opportunities": "y",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "finalized"
    assert resp.json()["overall_score"] == 85.0
    approved = [r for r in client.audit_rows if r["action"] == "approved"]
    assert len(approved) == 1
    row = approved[0]
    assert row["api_key_role"] == "team"
    assert row["evaluator_email"] == "ana@landing.com"
    assert row["agent_email"] == "luis@landing.com"
    assert row["agent_name"] == "Luis Rubio"
    assert row["call_id"] == "call-approve"
    assert row["target_team"] == "member_support"
    assert row["result_row"] == 555
    assert row["notes"] == "engine-scored"


def test_score_endpoint_legacy_name_only_resolves_email_for_roster_check(client, monkeypatch):
    """Legacy upload flow sends agent_name only (no agent_email). The
    backend should resolve email from name via Mails so the team-key
    roster check has something to compare against — otherwise the
    /score page would always 403."""
    async def fake_download(call_id):
        return b"BYTES"

    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-legacy",
            "agent_name": "Luis Rubio",  # name only, no email
            "manager_email": "ana@landing.com",
        },
        files={"audio_file": ("c.mp3", b"BYTES", "audio/mpeg")},
    )
    assert resp.status_code == 200, resp.text
    # Audit row should carry the resolved email, not blank.
    scored = [r for r in client.audit_rows if r["action"] == "scored"]
    assert len(scored) == 1
    assert scored[0]["agent_email"] == "luis@landing.com"
    assert scored[0]["agent_name"] == "Luis Rubio"


def test_score_endpoint_legacy_name_only_unrostered_team_key_rejected(client, monkeypatch):
    """If Mails can't resolve the name → no email → team key still 403s,
    just like the post-PR-28 behavior intends."""
    async def fake_download(call_id):
        return b"BYTES"

    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-legacy-miss",
            "agent_name": "Not In Roster",  # not in stubbed Mails
            "manager_email": "ana@landing.com",
        },
        files={"audio_file": ("c.mp3", b"BYTES", "audio/mpeg")},
    )
    assert resp.status_code == 403
    denied = [r for r in client.audit_rows if r["action"] == "denied"]
    assert len(denied) == 1


def test_score_endpoint_rejects_missing_agent_identity(client):
    resp = client.post(
        "/api/member_support/score",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_id": "call-bare",
            "manager_email": "ana@landing.com",
        },
    )
    assert resp.status_code == 400
    assert "agent_email" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# /score/batch — parity with /score for audit / idempotency / semaphore
# ---------------------------------------------------------------------------

def _batch_files(*pairs):
    """Build httpx files-tuples for multiple audio uploads.

    httpx's TestClient repeats the ``audio_files`` field when given a
    list of 2-tuples (field, filetuple), matching FastAPI's expectation.
    """
    return [("audio_files", (name, body, "audio/mpeg")) for name, body in pairs]


def test_batch_writes_one_audit_row_per_call(client):
    resp = client.post(
        "/api/member_support/score/batch",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_ids": "call-a,call-b,call-c",
            "agent_name": "Luis Rubio",
            "manager_email": "ana@landing.com",
        },
        files=_batch_files(
            ("a.mp3", b"AAA"),
            ("b.mp3", b"BBB"),
            ("c.mp3", b"CCC"),
        ),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 3
    assert len(body["job_ids"]) == 3

    scored = [r for r in client.audit_rows if r["action"] == "scored"]
    assert len(scored) == 3
    assert all(r["api_key_role"] == "team" for r in scored)
    assert all(r["agent_name"] == "Luis Rubio" for r in scored)
    assert all(r["target_team"] == "member_support" for r in scored)
    assert all(r["notes"] == "batch_upload" for r in scored)
    assert sorted(r["call_id"] for r in scored) == ["call-a", "call-b", "call-c"]


def test_batch_idempotent_on_in_flight_row(client):
    """A row whose job is already pending/scoring is returned unchanged
    — no duplicate audit row, no second background task scheduled."""
    expected_job_id = "call-existing_Luis_Rubio"
    scoring_module._jobs[
        scoring_module._job_key("member_support", expected_job_id)
    ] = {"status": "scoring", "call_id": "call-existing"}

    resp = client.post(
        "/api/member_support/score/batch",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_ids": "call-existing,call-new",
            "agent_name": "Luis Rubio",
            "manager_email": "ana@landing.com",
        },
        files=_batch_files(
            ("a.mp3", b"AAA"),
            ("b.mp3", b"BBB"),
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    # Both job_ids returned (idempotent caller still gets a stable handle).
    assert body["job_ids"] == [expected_job_id, "call-new_Luis_Rubio"]
    # Only ONE audit row — the new call. The in-flight one is skipped.
    scored = [r for r in client.audit_rows if r["action"] == "scored"]
    assert len(scored) == 1
    assert scored[0]["call_id"] == "call-new"


def test_batch_mismatch_returns_400(client):
    resp = client.post(
        "/api/member_support/score/batch",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        data={
            "call_ids": "call-a,call-b",
            "agent_name": "Luis Rubio",
            "manager_email": "ana@landing.com",
        },
        files=_batch_files(("a.mp3", b"AAA")),  # 1 file, 2 ids
    )
    assert resp.status_code == 400
    assert "mismatch" in resp.json()["detail"].lower()
    # No audit rows for a malformed request.
    assert client.audit_rows == []


def test_batch_privileged_role_recorded_in_audit(client):
    resp = client.post(
        "/api/sales/score/batch",
        headers={"Authorization": f"Bearer {PRIV_TOKEN}"},
        data={
            "call_ids": "call-priv-batch",
            "agent_name": "External Contractor",
            "manager_email": "hr@landing.com",
        },
        files=_batch_files(("c.mp3", b"BYTES")),
    )
    assert resp.status_code == 200, resp.text
    assert len(client.audit_rows) == 1
    row = client.audit_rows[0]
    assert row["api_key_role"] == "privileged"
    assert row["target_team"] == "sales"
    assert row["notes"] == "batch_upload"


# ---------------------------------------------------------------------------
# S1 — approve falls back to DB resolution (ScorecardActionsDesign §3)
# ---------------------------------------------------------------------------

def _eval_ref(**overrides):
    from backend.services.eval_store import EvalRef
    defaults = dict(
        id=2377,
        team_id="member_support",
        state="draft",
        source="ai",
        scoring_status="flagged_human_review",
        agent_name_raw="Luis Rubio",
        agent_email="luis@landing.com",
        evaluator_email=None,
        dialpad_call_id="5035229460504576",
        dialpad_entry_point_call_id="6105002063634432",
        dialpad_link="https://dialpad.com/callhistory/callreview/6105002063634432",
        call_summary="Billing question",
        key_strengths="Good tone",
        opportunities="Faster holds",
        overall_score=None,
        model="gemini-2.5-flash",
        sections=[],
        agent_id=7,
        duration_ms=180000.0,
    )
    defaults.update(overrides)
    return EvalRef(**defaults)


def _stub_engine_path(monkeypatch, captured_approvals):
    """Same engine stubs as test_approve_writes_audit_row, capturing the
    record_approval kwargs so the fallback context can be asserted."""
    monkeypatch.setattr(
        scoring_module.eval_store, "missing_manual_scores",
        lambda config, sections: [],
    )

    async def fake_record_approval(config, **kw):
        captured_approvals.append(kw)
        return kw.get("evaluation_id")
    monkeypatch.setattr(scoring_module, "record_approval", fake_record_approval)

    class _Detail:
        overall_score = 72.0

    async def fake_stamp(evaluation_id, config, evaluator_email):
        return _Detail()
    monkeypatch.setattr(scoring_module.eval_store, "stamp_and_finalize", fake_stamp)

    async def fake_pool():
        return object()
    monkeypatch.setattr(scoring_module.eval_store, "get_pool", fake_pool)

    async def fake_project(pool, evaluation_id, config, include_history=True):
        return 321
    monkeypatch.setattr(scoring_module, "project_evaluation", fake_project)
    monkeypatch.setattr(
        scoring_module, "trigger_apps_script",
        lambda row, team_id, disclaimer=None: {"status": "ok"},
    )


def _resolve_stub(monkeypatch, ref):
    calls = []

    async def fake_resolve(team_id, job_id):
        calls.append((team_id, job_id))
        return ref
    monkeypatch.setattr(scoring_module.eval_store, "resolve_evaluation", fake_resolve)
    return calls


def test_approve_restart_simulation_falls_back_to_db(client, monkeypatch):
    """Empty _jobs (process restarted) — approve reconstructs the context
    from qa.evaluations, finalizes, and caches the restored job."""
    approvals: list[dict] = []
    _stub_engine_path(monkeypatch, approvals)
    resolve_calls = _resolve_stub(monkeypatch, _eval_ref())

    job_id = "5035229460504576_Luis_Rubio"
    resp = client.post(
        f"/api/member_support/score/{job_id}/approve",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        json={
            "sections": [],
            "key_strengths": "x",
            "opportunities": "y",
            "evaluator_email": "ana@landing.com",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "finalized"
    assert resolve_calls == [("member_support", job_id)]

    # The approval ran against the resolved row, flagged-review resolution
    # included (scoring_status came back flagged_human_review).
    assert len(approvals) == 1
    assert approvals[0]["evaluation_id"] == 2377
    assert approvals[0]["evaluator_email"] == "ana@landing.com"
    assert approvals[0]["resolving_review"] is True

    # Restored job is cached like a live one and now shows approved.
    key = scoring_module._job_key("member_support", job_id)
    assert scoring_module._jobs[key]["status"] == "approved"
    assert scoring_module._jobs[key]["restored_from_db"] is True

    approved = [r for r in client.audit_rows if r["action"] == "approved"]
    assert len(approved) == 1
    assert approved[0]["evaluator_email"] == "ana@landing.com"
    assert approved[0]["agent_name"] == "Luis Rubio"


def test_approve_fallback_finalized_row_is_read_only(client, monkeypatch):
    """A finalized eval resolved from the DB keeps the 409 wall — the
    edit-of-finalized doorway (ack protocol) is slice S6, not S1."""
    approvals: list[dict] = []
    _stub_engine_path(monkeypatch, approvals)
    _resolve_stub(
        monkeypatch,
        _eval_ref(state="finalized", scoring_status="complete", overall_score=88.0),
    )

    resp = client.post(
        "/api/member_support/score/5035229460504576_Luis_Rubio/approve",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        json={
            "sections": [], "key_strengths": "x", "opportunities": "y",
            "evaluator_email": "ana@landing.com",
        },
    )
    assert resp.status_code == 409
    assert "read-only" in resp.json()["detail"]
    assert approvals == []


def test_approve_fallback_no_row_is_404(client, monkeypatch):
    _resolve_stub(monkeypatch, None)
    resp = client.post(
        "/api/member_support/score/999_Nobody/approve",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        json={"sections": [], "key_strengths": "x", "opportunities": "y"},
    )
    assert resp.status_code == 404


def test_approve_fallback_without_any_evaluator_is_422(client, monkeypatch):
    """Restored draft has no evaluator identity and the payload sends none:
    422 rather than approving as nobody ('' would pass the NOT-NULL CHECK)."""
    approvals: list[dict] = []
    _stub_engine_path(monkeypatch, approvals)
    _resolve_stub(monkeypatch, _eval_ref(evaluator_email=None))

    resp = client.post(
        "/api/member_support/score/5035229460504576_Luis_Rubio/approve",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        json={"sections": [], "key_strengths": "x", "opportunities": "y"},
    )
    assert resp.status_code == 422
    assert "evaluator_email" in resp.json()["detail"]
    assert approvals == []
    # Job stays approvable after the rejection.
    key = scoring_module._job_key(
        "member_support", "5035229460504576_Luis_Rubio"
    )
    assert scoring_module._jobs[key]["status"] == "complete"


def test_approve_fallback_row_evaluator_backfills_payload(client, monkeypatch):
    """No payload evaluator, but the row carries one (e.g. re-approving a
    draft that had an evaluator stamped) — the row identity is used."""
    approvals: list[dict] = []
    _stub_engine_path(monkeypatch, approvals)
    _resolve_stub(
        monkeypatch,
        _eval_ref(evaluator_email="lead@landing.com", scoring_status="complete"),
    )

    resp = client.post(
        "/api/member_support/score/5035229460504576_Luis_Rubio/approve",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        json={"sections": [], "key_strengths": "x", "opportunities": "y"},
    )
    assert resp.status_code == 200, resp.text
    assert approvals[0]["evaluator_email"] == "lead@landing.com"
    assert approvals[0]["resolving_review"] is False


# ---------------------------------------------------------------------------
# S3 — DELETE /score/{job_id} (ScorecardActionsDesign §4.4)
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager  # noqa: E402


class _FakeDeleteConn:
    def __init__(self, coachings=(), eval_row=None, sections=(), point=None):
        self.coachings = list(coachings)
        self.eval_row = eval_row
        self.sections = list(sections)
        self.point = point
        self.deletes: list[tuple[str, tuple]] = []
        self.in_transaction_deletes: list[str] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch(self, query, *args):
        if "coaching_evaluations" in query:
            return self.coachings
        if "evaluation_sections" in query:
            return self.sections
        raise AssertionError(f"unexpected fetch: {query}")

    async def fetchrow(self, query, *args):
        if "FROM qa.evaluations" in query:
            return self.eval_row
        if "agent_stat_points" in query:
            return self.point
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def execute(self, query, *args):
        assert query.startswith("DELETE")
        self.deletes.append((query, args))
        return "DELETE 1"


class _FakeDeletePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _wire_delete(monkeypatch, ref, conn):
    async def fake_resolve(team_id, job_id):
        return ref
    monkeypatch.setattr(scoring_module.eval_store, "resolve_evaluation", fake_resolve)

    async def fake_pool():
        return _FakeDeletePool(conn)
    monkeypatch.setattr(scoring_module.eval_store, "get_pool", fake_pool)

    rebuilds: list[int] = []

    async def fake_rebuild(c, agent_id, config):
        rebuilds.append(agent_id)
        return 3
    monkeypatch.setattr(scoring_module, "rebuild_agent_series", fake_rebuild)

    tombstones: list[str] = []

    async def fake_tombstone(link, config):
        tombstones.append(link)
        return 42
    monkeypatch.setattr(scoring_module, "tombstone_evaluation", fake_tombstone)
    return rebuilds, tombstones


def test_delete_requires_privileged_key(client, monkeypatch):
    resolve_calls = []

    async def fake_resolve(team_id, job_id):
        resolve_calls.append(job_id)
        return None
    monkeypatch.setattr(scoring_module.eval_store, "resolve_evaluation", fake_resolve)

    resp = client.delete(
        "/api/member_support/score/5035229460504576_Luis_Rubio",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
    )
    assert resp.status_code == 403
    assert resolve_calls == []  # denied before touching the DB
    denied = [r for r in client.audit_rows if r["action"] == "denied"]
    assert len(denied) == 1
    assert denied[0]["notes"] == "delete_requires_privileged_key"
    assert denied[0]["call_id"] == "5035229460504576"
    assert denied[0]["api_key_role"] == "team"


def test_delete_unknown_eval_404(client, monkeypatch):
    async def fake_resolve(team_id, job_id):
        return None
    monkeypatch.setattr(scoring_module.eval_store, "resolve_evaluation", fake_resolve)
    resp = client.delete(
        "/api/member_support/score/999_Nobody",
        headers={"Authorization": f"Bearer {PRIV_TOKEN}"},
    )
    assert resp.status_code == 404


def test_delete_refuses_when_coaching_rows_exist(client, monkeypatch):
    conn = _FakeDeleteConn(coachings=[{"coaching_id": 9, "linked_at": None}])
    _wire_delete(monkeypatch, _eval_ref(state="finalized"), conn)

    resp = client.delete(
        "/api/member_support/score/5035229460504576_Luis_Rubio",
        headers={"Authorization": f"Bearer {PRIV_TOKEN}"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["coaching_ids"] == [9]
    assert conn.deletes == []  # nothing touched


def test_delete_happy_path(client, monkeypatch):
    eval_row = {
        "id": 2377, "agent_id": 7, "team_id": "member_support",
        "overall_score": 0.0, "dialpad_link": "https://dialpad.test/call/X",
    }
    point = {"id": 1404, "evaluation_id": 2377, "ewma": 43.6}
    conn = _FakeDeleteConn(
        eval_row=eval_row,
        sections=[{"section_id": "greeting", "numeric_score": 5}],
        point=point,
    )
    ref = _eval_ref(state="finalized", overall_score=0.0)
    rebuilds, tombstones = _wire_delete(monkeypatch, ref, conn)

    # Pre-seed a cached job to verify eviction.
    key = scoring_module._job_key("member_support", "5035229460504576_Luis_Rubio")
    scoring_module._jobs[key] = {"status": "complete"}

    resp = client.delete(
        "/api/member_support/score/5035229460504576_Luis_Rubio",
        headers={"Authorization": f"Bearer {PRIV_TOKEN}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "deleted"
    assert body["evaluation_id"] == 2377
    assert body["tombstoned_history_row"] == 42
    # Snapshot is the caller's backup.
    assert body["snapshot"]["evaluation"]["id"] == 2377
    assert body["snapshot"]["sections"][0]["section_id"] == "greeting"
    assert body["snapshot"]["stat_point"]["id"] == 1404

    # Delete order: stat point first (NO ACTION FK), then the eval row.
    assert "agent_stat_points" in conn.deletes[0][0]
    assert "FROM qa.evaluations" in conn.deletes[1][0]
    assert rebuilds == [7]
    assert tombstones == [ref.dialpad_link]

    orphaned = [r for r in client.audit_rows if r["action"] == "evaluation_orphaned"]
    assert len(orphaned) == 1
    assert orphaned[0]["result_row"] == 42
    assert "old_score=0.0" in orphaned[0]["notes"]
    assert orphaned[0]["agent_name"] == "Luis Rubio"

    assert key not in scoring_module._jobs  # ghost evicted


def test_delete_skips_rebuild_for_agentless_eval(client, monkeypatch):
    """agent_id NULL (departed agent, no stat point) — delete proceeds,
    no series rebuild."""
    conn = _FakeDeleteConn(
        eval_row={"id": 5, "agent_id": None, "dialpad_link": ""},
        sections=[], point=None,
    )
    rebuilds, _ = _wire_delete(monkeypatch, _eval_ref(id=5), conn)

    resp = client.delete(
        "/api/member_support/score/5035229460504576_Luis_Rubio",
        headers={"Authorization": f"Bearer {PRIV_TOKEN}"},
    )
    assert resp.status_code == 200, resp.text
    assert rebuilds == []
    assert resp.json()["snapshot"]["stat_point"] is None


# ---------------------------------------------------------------------------
# S4 — POST /score/{job_id}/rescore (ScorecardActionsDesign §4.2, manual)
# ---------------------------------------------------------------------------

class _NullConn:
    @asynccontextmanager
    async def transaction(self):
        yield


class _NullPool:
    def __init__(self):
        self.conn = _NullConn()

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class _FakeScorecard:
    """Just enough of ScorecardWithMeta for the post-Stage-1 flow."""

    def __init__(self, manager_email):
        self.sections = []
        self.manager_email = manager_email
        self.agent_name = "Luis Rubio"
        self.dialpad_link = (
            "https://dialpad.com/callhistory/callreview/6105002063634432"
        )
        self.call_summary = "Billing question"
        self.key_strengths = "Good tone"
        self.opportunities = "Faster holds"
        self.model = "gemini-2.5-flash"

    def model_dump(self):
        return {
            "sections": self.sections,
            "manager_email": self.manager_email,
            "dialpad_link": self.dialpad_link,
            "call_summary": self.call_summary,
            "key_strengths": self.key_strengths,
            "opportunities": self.opportunities,
            "agent_name": self.agent_name,
            "model": self.model,
        }


def _stub_rescore_pipeline(monkeypatch, *, flagged=False, draft_row_id=2377,
                           engine_score=88.0):
    """Stub the whole rescore background pipeline, recording operation
    order in caps['ops'] — the §4.2 order proof (draft → rebuild → stamp)
    rides on it."""
    caps = {"score_calls": [], "ops": [], "rebuilds": [], "dispatches": [],
            "approvals": []}

    async def fake_download(call_id):
        caps["download"] = call_id
        return b"AUDIO"
    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    async def fake_score_call(**kw):
        caps["score_calls"].append(kw)
        return _FakeScorecard(kw["manager_email"])
    monkeypatch.setattr(scoring_module, "score_call", fake_score_call)

    async def fake_record_draft(scorecard, config, strict=False):
        caps["ops"].append("draft")
        return draft_row_id
    monkeypatch.setattr(
        scoring_module, "record_draft_evaluation", fake_record_draft)

    async def fake_rebuild(conn, agent_id, config):
        caps["ops"].append("rebuild")
        caps["rebuilds"].append(agent_id)
        return 3
    monkeypatch.setattr(scoring_module, "rebuild_agent_series", fake_rebuild)

    monkeypatch.setattr(
        scoring_module.eval_store, "_active_formula", lambda team_id: None)
    monkeypatch.setattr(
        scoring_module.eval_store, "human_review_trigger_fired",
        lambda formula, sections: flagged)
    monkeypatch.setattr(
        scoring_module.eval_store, "requires_analyst_review",
        lambda config: False)

    class _Detail:
        overall_score = engine_score

    async def fake_stamp(evaluation_id, config, evaluator_email):
        caps["ops"].append("stamp")
        return _Detail()
    monkeypatch.setattr(
        scoring_module.eval_store, "stamp_and_finalize", fake_stamp)

    pool = _NullPool()

    async def fake_pool():
        return pool
    monkeypatch.setattr(scoring_module.eval_store, "get_pool", fake_pool)

    async def fake_project(pool_, evaluation_id, config, include_history=True):
        caps["ops"].append("project")
        return 321
    monkeypatch.setattr(scoring_module, "project_evaluation", fake_project)

    def fake_trigger(row, team_id, disclaimer=None):
        caps["dispatches"].append({"row": row, "disclaimer": disclaimer})
        return {"status": "ok"}
    monkeypatch.setattr(scoring_module, "trigger_apps_script", fake_trigger)

    # Approve-path stubs so the flagged → re-approve round-trip works.
    monkeypatch.setattr(
        scoring_module.eval_store, "missing_manual_scores",
        lambda config, sections: [])

    async def fake_record_approval(config, **kw):
        caps["approvals"].append(kw)
        return kw.get("evaluation_id")
    monkeypatch.setattr(scoring_module, "record_approval", fake_record_approval)

    return caps


_RESCORE_URL = "/api/member_support/score/5035229460504576_Luis_Rubio/rescore"


def test_rescore_in_flight_409(client, monkeypatch):
    """§4.2: refuse to race a pending/scoring job — before any DB probe."""
    resolve_calls = _resolve_stub(monkeypatch, _eval_ref())
    key = scoring_module._job_key(
        "member_support", "5035229460504576_Luis_Rubio")
    scoring_module._jobs[key] = {"status": "scoring"}

    resp = client.post(
        _RESCORE_URL,
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        json={"evaluator_email": "ana@landing.com"},
    )
    assert resp.status_code == 409
    assert "scoring" in resp.json()["detail"]
    assert resolve_calls == []
    assert client.audit_rows == []


def test_rescore_unknown_eval_404(client, monkeypatch):
    _resolve_stub(monkeypatch, None)
    resp = client.post(
        _RESCORE_URL,
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        json={"evaluator_email": "ana@landing.com"},
    )
    assert resp.status_code == 404
    assert client.audit_rows == []


def test_rescore_team_key_unrostered_403_denied_audit(client, monkeypatch):
    """§7: team keys are roster-scoped for rescore, same as /score."""
    _resolve_stub(monkeypatch, _eval_ref(agent_email="ghost@landing.com"))
    resp = client.post(
        _RESCORE_URL,
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        json={"evaluator_email": "ana@landing.com"},
    )
    assert resp.status_code == 403
    denied = [r for r in client.audit_rows if r["action"] == "denied"]
    assert len(denied) == 1
    assert denied[0]["notes"] == "http_403"
    assert denied[0]["evaluator_email"] == "ana@landing.com"
    assert denied[0]["agent_name"] == "Luis Rubio"


def test_rescore_no_recording_422(client, monkeypatch):
    _resolve_stub(monkeypatch, _eval_ref())

    async def fake_download(call_id):
        raise scoring_module.NoRecordingAvailable("gone")
    monkeypatch.setattr(scoring_module, "download_recording", fake_download)

    resp = client.post(
        _RESCORE_URL,
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        json={"evaluator_email": "ana@landing.com"},
    )
    assert resp.status_code == 422
    denied = [r for r in client.audit_rows if r["action"] == "denied"]
    assert len(denied) == 1
    assert denied[0]["notes"] == "no_recording"
    rescored = [r for r in client.audit_rows if r["action"] == "rescored"]
    assert rescored == []


def test_rescore_clean_pass_auto_finalizes_with_disclaimer(client, monkeypatch):
    """Full REPLACE round-trip on a clean fresh pass: same row id, series
    rebuilt while the row is draft (§4.2.3 — after the upsert, before
    finalize), auto-finalize, disclaimer email by default."""
    caps = _stub_rescore_pipeline(monkeypatch)
    _resolve_stub(
        monkeypatch,
        _eval_ref(state="finalized", scoring_status="complete",
                  overall_score=62.5),
    )

    resp = client.post(
        _RESCORE_URL,
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        json={"evaluator_email": "ana@landing.com"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["evaluation_id"] == 2377
    assert body["superseded_score"] == 62.5

    # Audit receipt with the superseded score AND model stamp (the
    # P3+ cross-model traceability hook).
    rescored = [r for r in client.audit_rows if r["action"] == "rescored"]
    assert len(rescored) == 1
    assert rescored[0]["notes"] == "manual: 62.5; superseded_model=gemini-2.5-flash"
    assert rescored[0]["evaluator_email"] == "ana@landing.com"
    assert rescored[0]["call_id"] == "5035229460504576"

    # Fresh pass ran with the row's own inputs.
    assert caps["download"] == "5035229460504576"
    assert len(caps["score_calls"]) == 1
    assert caps["score_calls"][0]["duration_ms"] == 180000.0
    assert caps["score_calls"][0]["manager_email"] == "ana@landing.com"

    # §4.2 order proof: upsert → rebuild (row is draft) → finalize.
    assert caps["ops"] == ["draft", "rebuild", "stamp", "project"]
    assert caps["rebuilds"] == [7]

    # Disclaimer email dispatched by default.
    assert caps["dispatches"] == [{"row": 321, "disclaimer": "rescore_manual"}]

    key = scoring_module._job_key(
        "member_support", "5035229460504576_Luis_Rubio")
    job = scoring_module._jobs[key]
    assert job["status"] == "complete"
    assert job["state"] == "finalized"
    assert job["overall_score"] == 88.0
    assert job["rescore"]["superseded_score"] == 62.5
    assert job["email_dispatch"] == {"status": "ok"}


def test_rescore_suppress_email_opts_out(client, monkeypatch):
    caps = _stub_rescore_pipeline(monkeypatch)
    _resolve_stub(monkeypatch, _eval_ref(state="finalized", overall_score=62.5))

    resp = client.post(
        _RESCORE_URL,
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        json={"evaluator_email": "ana@landing.com", "suppress_email": True},
    )
    assert resp.status_code == 200, resp.text
    assert caps["dispatches"] == []
    key = scoring_module._job_key(
        "member_support", "5035229460504576_Luis_Rubio")
    job = scoring_module._jobs[key]
    assert job["status"] == "complete"
    assert job["email_dispatch"]["status"] == "suppressed"


def test_rescore_row_id_mismatch_errors_job(client, monkeypatch):
    """REPLACE contract guard: if the upsert lands anywhere but the
    resolved row, the job errors loudly before touching the stat chain."""
    caps = _stub_rescore_pipeline(monkeypatch, draft_row_id=9999)
    _resolve_stub(monkeypatch, _eval_ref(state="finalized", overall_score=62.5))

    resp = client.post(
        _RESCORE_URL,
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        json={"evaluator_email": "ana@landing.com"},
    )
    assert resp.status_code == 200  # scheduling succeeded; the job errored
    key = scoring_module._job_key(
        "member_support", "5035229460504576_Luis_Rubio")
    job = scoring_module._jobs[key]
    assert job["status"] == "error"
    assert "REPLACE contract violated" in job["error"]
    assert "rebuild" not in caps["ops"]
    assert "stamp" not in caps["ops"]


def test_rescore_flagged_pass_holds_draft_then_approve_dispatches_disclaimer(
        client, monkeypatch):
    """The fresh pass fires §3.14 → row holds in draft review; the
    re-approve finalizes with resolving_review and the disclaimer email
    still rides the job's rescore context."""
    caps = _stub_rescore_pipeline(monkeypatch, flagged=True)
    _resolve_stub(monkeypatch, _eval_ref(state="finalized", overall_score=45.0))

    resp = client.post(
        _RESCORE_URL,
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        json={"evaluator_email": "ana@landing.com"},
    )
    assert resp.status_code == 200, resp.text
    key = scoring_module._job_key(
        "member_support", "5035229460504576_Luis_Rubio")
    job = scoring_module._jobs[key]
    assert job["status"] == "complete"
    assert job["state"] == "draft"
    assert job["scoring_status"] == "flagged_human_review"
    # Stat point already removed while the row sits in draft (§4.2.3).
    assert caps["ops"] == ["draft", "rebuild"]
    assert caps["dispatches"] == []

    resp = client.post(
        "/api/member_support/score/5035229460504576_Luis_Rubio/approve",
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        json={"sections": [], "key_strengths": "x", "opportunities": "y"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "finalized"
    assert caps["approvals"][0]["resolving_review"] is True
    assert caps["approvals"][0]["evaluator_email"] == "ana@landing.com"
    assert caps["ops"] == ["draft", "rebuild", "stamp", "project"]
    assert caps["dispatches"] == [{"row": 321, "disclaimer": "rescore_manual"}]

    actions = [r["action"] for r in client.audit_rows]
    assert actions == ["rescored", "approved"]


def test_rescore_agentless_eval_skips_rebuild(client, monkeypatch):
    """agent_id NULL (departed agent) — fresh pass proceeds, no series
    rebuild, matching the delete route's behavior."""
    caps = _stub_rescore_pipeline(monkeypatch)
    _resolve_stub(
        monkeypatch,
        _eval_ref(state="finalized", overall_score=62.5, agent_id=None),
    )

    resp = client.post(
        _RESCORE_URL,
        headers={"Authorization": f"Bearer {TEAM_TOKEN}"},
        json={"evaluator_email": "ana@landing.com"},
    )
    assert resp.status_code == 200, resp.text
    key = scoring_module._job_key(
        "member_support", "5035229460504576_Luis_Rubio")
    assert scoring_module._jobs[key]["status"] == "complete"
    assert caps["rebuilds"] == []
    assert caps["ops"] == ["draft", "stamp", "project"]


def test_rescore_privileged_key_unrostered_allowed(client, monkeypatch):
    """§7: privileged keys rescore unrostered agents (same as /score)."""
    caps = _stub_rescore_pipeline(monkeypatch)
    _resolve_stub(
        monkeypatch,
        _eval_ref(state="finalized", overall_score=62.5,
                  agent_email="ghost@landing.com"),
    )

    resp = client.post(
        _RESCORE_URL,
        headers={"Authorization": f"Bearer {PRIV_TOKEN}"},
        json={"evaluator_email": "ana@landing.com"},
    )
    assert resp.status_code == 200, resp.text
    rescored = [r for r in client.audit_rows if r["action"] == "rescored"]
    assert len(rescored) == 1
    assert rescored[0]["api_key_role"] == "privileged"
