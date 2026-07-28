"""ScorecardActions S7 — frontend surface checkpoints (§9).

Static assertions over the shipped HTML. These pin the DESIGN
boundaries, not the styling: the override doorway exists on the
scorecard page ONLY (§4.3 — an approval-flow action, not archaeology),
the datapoint page carries the lifecycle actions with role-gated
delete rendering, and both manual doorways surface the §4.3a
acknowledgment text.
"""

from __future__ import annotations

from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
SCORECARD = (FRONTEND / "scorecard.html").read_text()
DATAPOINT = (FRONTEND / "datapoint.html").read_text()

ACK_PHRASE = "manually modified by a human"


class TestOverrideSurfaceBoundary:
    def test_scorecard_has_override_control(self):
        assert "override-form" in SCORECARD
        assert "/override" in SCORECARD

    def test_datapoint_page_has_no_override_control(self):
        """§4.3: scorecard surface ONLY. The word may appear in prose;
        the control and its endpoint must not."""
        assert "override-form" not in DATAPOINT
        assert "/override" not in DATAPOINT
        assert "submitOverride" not in DATAPOINT


class TestAcknowledgmentProtocolSurface:
    def test_scorecard_carries_ack_text(self):
        assert ACK_PHRASE in SCORECARD

    def test_scorecard_resolution_approve_sends_ack(self):
        assert "payload.acknowledged = resolvingReview" in SCORECARD

    def test_scorecard_unlock_to_edit_uses_ack(self):
        assert "unlockForEdit" in SCORECARD
        assert "/datapoints/" in SCORECARD  # re-approval posts to the edit surface


class TestDatapointActionBar:
    def test_lifecycle_actions_present(self):
        assert "rescoreEval" in DATAPOINT
        assert "deleteEval" in DATAPOINT
        assert "editInEditorLink" in DATAPOINT

    def test_delete_is_role_gated_via_whoami(self):
        """S7 checkpoint: role-gated button rendering — delete renders
        only after /whoami says privileged (routes still enforce)."""
        assert "/whoami" in DATAPOINT
        assert "privileged" in DATAPOINT
        # Hidden until the whoami check flips it.
        assert 'id="deleteBtn"' in DATAPOINT and "display:none" in DATAPOINT

    def test_scorecard_delete_is_role_gated_too(self):
        assert "/whoami" in SCORECARD
        assert "privileged" in SCORECARD

    def test_delete_requires_typed_agent_name(self):
        """§4.4 UI: type-the-agent-name confirm on both surfaces."""
        assert "Type the agent name" in DATAPOINT
        assert "Type the agent name" in SCORECARD


class TestGasDisclaimerTemplate:
    GAS = Path(__file__).resolve().parent.parent.parent  # qa-automation/

    def test_shared_base_renders_all_causes(self):
        renderer = (self.GAS / "src" / "HtmlRenderer.js").read_text()
        for cause in ("rescore_manual", "rescore_auto", "override",
                      "review_resolution", "edit_finalized"):
            assert cause in renderer, f"disclaimer cause {cause} missing"

    def test_dopost_threads_disclaimer(self):
        main = (self.GAS / "src" / "Main.js").read_text()
        assert "payload.disclaimer" in main
        assert "_processHistoryRow(entry, disclaimer)" in main
