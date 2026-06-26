"""Unit tests for ``database/runner.py`` confirmation prompt + DSN
redaction. These don't need Docker — they exercise the pure-function
helpers and argparse plumbing directly.

The full integration suite at ``tests/integration/`` tests the runner's
DB behavior end-to-end; this file targets the operator-facing safety
gate added in the follow-up PR.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Make ``database.runner`` importable. Walk up to the repo root via the
# database/migrations/ ancestor — robust against tree restructuring.
def _find_repo_root() -> Path:
    p = Path(__file__).resolve()
    while p.parent != p:
        if (p / "database" / "migrations").is_dir():
            return p
        p = p.parent
    raise RuntimeError("Could not locate repo root (no database/migrations/ ancestor)")


_REPO_ROOT = _find_repo_root()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from database import runner  # noqa: E402


# ---------------------------------------------------------------------------
# _redact_dsn — strip the password but leave everything else readable
# ---------------------------------------------------------------------------


def test_redact_dsn_replaces_password() -> None:
    dsn = "postgresql://user:supersecret@host.railway.app:5432/railway"
    redacted = runner._redact_dsn(dsn)
    assert redacted == "postgresql://user:***@host.railway.app:5432/railway"


def test_redact_dsn_handles_no_password() -> None:
    """A DSN without a `:password@` segment should pass through unchanged."""
    dsn = "postgresql://localhost:5432/db"
    assert runner._redact_dsn(dsn) == dsn


def test_redact_dsn_handles_complex_password() -> None:
    """Passwords with URL-encoded special chars (but no `@`) should still
    be redacted cleanly."""
    dsn = "postgresql://admin:p%40ss%21word@host:5432/db"
    redacted = runner._redact_dsn(dsn)
    assert "p%40ss" not in redacted
    assert "***" in redacted
    assert "admin" in redacted
    assert "host:5432/db" in redacted


def test_redact_dsn_does_not_leak_password_in_logs() -> None:
    """The contract: never include the literal password in the redacted
    string. Defense-in-depth assertion for the "show DSN in prompt"
    code path."""
    dsn = "postgres://u:my_uniq_secret_4242@h/d"
    assert "my_uniq_secret_4242" not in runner._redact_dsn(dsn)


# ---------------------------------------------------------------------------
# _confirm — yes flag, TTY check, y/N parsing
# ---------------------------------------------------------------------------


def test_confirm_returns_true_when_yes_flag_set() -> None:
    """`--yes` bypasses the prompt entirely. No TTY check, no stdin read."""
    assert runner._confirm("apply migrations", "postgres://u:p@h/d", yes=True) is True


def test_confirm_returns_false_when_not_tty_and_no_yes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """In a non-TTY (pipe / cron / CI), refuse without prompting.
    Prevents silent mutations when the operator didn't pass --yes."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = runner._confirm("apply migrations", "postgres://u:p@h/d", yes=False)
    assert rc is False
    captured = capsys.readouterr()
    assert "TTY" in captured.err or "tty" in captured.err.lower()


def test_confirm_accepts_lowercase_y(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert runner._confirm("X", "postgres://u:p@h/d", yes=False) is True


def test_confirm_accepts_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "YES")
    assert runner._confirm("X", "postgres://u:p@h/d", yes=False) is True


def test_confirm_rejects_n(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert runner._confirm("X", "postgres://u:p@h/d", yes=False) is False


def test_confirm_rejects_empty_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty (just Enter) is treated as N. The prompt says [y/N] — N
    is the default."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert runner._confirm("X", "postgres://u:p@h/d", yes=False) is False


def test_confirm_rejects_anything_other_than_y_or_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "go")
    assert runner._confirm("X", "postgres://u:p@h/d", yes=False) is False


def test_confirm_handles_keyboard_interrupt_as_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_prompt: str) -> str:
        raise KeyboardInterrupt
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", _raise)
    assert runner._confirm("X", "postgres://u:p@h/d", yes=False) is False


def test_confirm_handles_eof_as_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_prompt: str) -> str:
        raise EOFError
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", _raise)
    assert runner._confirm("X", "postgres://u:p@h/d", yes=False) is False


def test_confirm_prompt_shows_redacted_dsn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The whole point — operators see the target DB but not the password."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    runner._confirm("apply migrations",
                    "postgresql://u:supersecret@prod.railway.app:5432/db",
                    yes=False)
    captured = capsys.readouterr()
    assert "***" in captured.err
    assert "prod.railway.app" in captured.err
    assert "supersecret" not in captured.err


# ---------------------------------------------------------------------------
# argparse — --yes / -y are accepted on every mutating subcommand
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["up", "--yes"],
        ["up", "-y"],
        ["up", "--limit", "2", "-y"],
        ["down", "--yes"],
        ["down", "-y"],
        ["down", "--limit", "3", "--yes"],
        ["bootstrap", "--yes"],
        ["bootstrap", "-y"],
    ],
)
def test_parser_accepts_yes_on_mutating_subcommands(argv: list[str]) -> None:
    parser = runner._make_parser()
    args = parser.parse_args(argv)
    assert getattr(args, "yes", False) is True


def test_parser_yes_defaults_false_when_unspecified() -> None:
    parser = runner._make_parser()
    args = parser.parse_args(["up"])
    assert getattr(args, "yes", False) is False


def test_status_does_not_carry_yes_flag() -> None:
    """``status`` is read-only — no confirmation needed, no --yes flag.
    Passing --yes to status should be a parse error."""
    parser = runner._make_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["status", "--yes"])


# ---------------------------------------------------------------------------
# _action_label — per-subcommand label generation
#
# Regression layer for the bug shipped in the initial confirmation-prompt
# PR: a dict literal in main_async eagerly evaluated args.limit for every
# command, but the bootstrap subparser doesn't declare --limit. Computing
# the label per command (if/elif) instead of via a dict keeps each branch
# isolated. These tests instantiate args through the actual parser so we
# catch any future drift between subparser flags and the label function.
# ---------------------------------------------------------------------------


def test_action_label_bootstrap_does_not_reference_limit() -> None:
    """The regression. Bootstrap's args namespace has no `limit`; touching
    it raises AttributeError. The label function MUST work with bare
    bootstrap args."""
    parser = runner._make_parser()
    args = parser.parse_args(["bootstrap", "--yes"])
    label = runner._action_label(args)
    assert "register" in label
    # Both pre-existing versions are mentioned in the label so the
    # operator sees exactly what's about to be marked applied.
    assert "001" in label
    assert "002" in label


def test_action_label_up_without_limit() -> None:
    parser = runner._make_parser()
    args = parser.parse_args(["up"])
    assert runner._action_label(args) == "apply ALL pending migrations"


def test_action_label_up_with_limit() -> None:
    parser = runner._make_parser()
    args = parser.parse_args(["up", "--limit", "2"])
    label = runner._action_label(args)
    assert "limit=2" in label


def test_action_label_down_default_limit() -> None:
    """`down` defaults --limit to 1. The label should reflect that."""
    parser = runner._make_parser()
    args = parser.parse_args(["down"])
    label = runner._action_label(args)
    assert "1 most-recent migration" in label


def test_action_label_down_with_limit() -> None:
    parser = runner._make_parser()
    args = parser.parse_args(["down", "--limit", "3"])
    label = runner._action_label(args)
    assert "roll back the 3 most-recent" in label


def test_action_label_rejects_status() -> None:
    """`status` is read-only; the label function is only valid for
    mutating commands. Defensive — caller (main_async) gates by command,
    but tests of this invariant document the contract."""
    import argparse
    args = argparse.Namespace(command="status")
    with pytest.raises(ValueError, match="unexpected mutating command"):
        runner._action_label(args)
