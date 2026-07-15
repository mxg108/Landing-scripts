"""Versioned SQL migration runner.

Reads ``database/migrations/NNN_name.sql`` (up) and optional matching
``NNN_name_down.sql`` (down) files. Tracks applied versions in a
``public.schema_migrations`` table with the down SQL snapshotted at apply
time, so rollback survives later deletion of the ``_down.sql`` file.

CLI::

    python -m database.runner status
    python -m database.runner up [--limit N] [--yes]
    python -m database.runner down [--limit N] [--yes]    # default --limit 1
    python -m database.runner bootstrap [--yes]

``DATABASE_URL`` env var carries the connection string. The ``bootstrap``
command registers migrations that were applied via raw psql before this
runner existed (currently 001 and 002 — the mass_notifications schema).

Mutating commands (``up`` / ``down`` / ``bootstrap``) prompt for
confirmation before doing work, showing the redacted target DSN.
``--yes`` (or ``-y``) skips the prompt for scripted / CI use. If stdin
is not a TTY and ``--yes`` wasn't passed, the runner refuses without
prompting — protects against piped or cron contexts silently mutating
the production database.

Naming convention
-----------------
The ``_down`` suffix is reserved by the down-file convention. A migration
file named e.g. ``104_callback_down.sql`` is ambiguous — the parser will
treat it as the down companion of a non-existent ``104_callback.sql`` and
the migration won't be applied. Pick names that don't end in ``_down``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"

# Migrations applied via raw psql before the runner existed. Bootstrap
# registers these as applied without re-running them. Add entries here
# when the prod DB has migrations that the runner can't safely re-apply.
PREEXISTING_VERSIONS: dict[int, str] = {
    1: "mass_notifications_schema",
    2: "add_property_event_columns",
}

_FILENAME_RE = re.compile(r"^(\d+)_(.+?)(_down)?\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up_path: Path
    down_path: Path | None

    @property
    def up_sql(self) -> str:
        return self.up_path.read_text(encoding="utf-8")

    @property
    def down_sql(self) -> str | None:
        if self.down_path is None:
            return None
        return self.down_path.read_text(encoding="utf-8")


def discover_migrations(migrations_dir: Path | None = None) -> list[Migration]:
    """Read the migrations directory and pair up files with `_down` companions.

    Returns migrations sorted by version. Files not matching ``NNN_name.sql``
    or ``NNN_name_down.sql`` are silently skipped — that lets ``.gitkeep``,
    READMEs, etc. coexist in the directory.
    """
    base = migrations_dir or MIGRATIONS_DIR
    if not base.exists():
        return []

    up_files: dict[int, tuple[str, Path]] = {}
    down_files: dict[int, Path] = {}

    for path in sorted(base.iterdir()):
        if path.suffix != ".sql":
            continue
        m = _FILENAME_RE.match(path.name)
        if not m:
            continue
        version = int(m.group(1))
        name = m.group(2)
        is_down = m.group(3) is not None
        if is_down:
            down_files[version] = path
        else:
            up_files[version] = (name, path)

    return [
        Migration(
            version=version,
            name=up_files[version][0],
            up_path=up_files[version][1],
            down_path=down_files.get(version),
        )
        for version in sorted(up_files)
    ]


async def ensure_meta_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public.schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            down_sql TEXT
        )
        """
    )


async def _fetch_applied(conn: asyncpg.Connection) -> dict[int, str]:
    rows = await conn.fetch(
        "SELECT version, name FROM public.schema_migrations ORDER BY version"
    )
    return {r["version"]: r["name"] for r in rows}


async def cmd_status(conn: asyncpg.Connection, migrations_dir: Path | None = None) -> int:
    await ensure_meta_table(conn)
    applied = await _fetch_applied(conn)
    discovered = discover_migrations(migrations_dir)
    by_version = {m.version: m for m in discovered}

    print(f"{'Ver':>4}  {'Name':<60}  {'Status':<10}  File")
    print("-" * 95)

    all_versions = sorted(set(applied) | set(by_version))
    for v in all_versions:
        disk = by_version.get(v)
        disk_name = disk.name if disk else "<missing-from-disk>"
        if v in applied:
            status = "applied"
            if disk and disk.name != applied[v]:
                status = "renamed?"
        else:
            status = "pending"
        file_label = disk.up_path.name if disk else "-"
        print(f"{v:>4}  {disk_name:<60}  {status:<10}  {file_label}")

    pending = sum(1 for m in discovered if m.version not in applied)
    print()
    print(f"Applied: {len(applied)}    Pending: {pending}    Disk total: {len(discovered)}")
    return 0


async def cmd_up(
    conn: asyncpg.Connection,
    *,
    limit: int | None = None,
    migrations_dir: Path | None = None,
) -> int:
    await ensure_meta_table(conn)
    applied = await _fetch_applied(conn)
    discovered = discover_migrations(migrations_dir)
    pending = [m for m in discovered if m.version not in applied]

    if not pending:
        print("No pending migrations.")
        return 0

    # Out-of-order guard. If any pending migration has a version lower than
    # the highest already-applied version, refuse — the operator must either
    # roll back to a state where that pending version comes next, or accept
    # that the lower version is a no-op that should be removed from disk.
    if applied:
        max_applied = max(applied)
        out_of_order = [m for m in pending if m.version <= max_applied]
        if out_of_order:
            print(
                f"ERROR: pending migrations with version <= {max_applied} "
                f"(already applied):",
                file=sys.stderr,
            )
            for m in out_of_order:
                print(f"  {m.version:03d}_{m.name}", file=sys.stderr)
            return 2

    if limit is not None:
        pending = pending[:limit]

    for m in pending:
        print(f"Applying {m.version:03d}_{m.name} ...")
        async with conn.transaction():
            await conn.execute(m.up_sql)
            await conn.execute(
                "INSERT INTO public.schema_migrations (version, name, down_sql) "
                "VALUES ($1, $2, $3)",
                m.version,
                m.name,
                m.down_sql,
            )
        print("  applied.")

    print(f"\nApplied {len(pending)} migration(s).")
    return 0


async def cmd_down(conn: asyncpg.Connection, *, limit: int = 1) -> int:
    await ensure_meta_table(conn)
    rows = await conn.fetch(
        "SELECT version, name, down_sql FROM public.schema_migrations "
        "ORDER BY version DESC LIMIT $1",
        limit,
    )
    if not rows:
        print("No applied migrations to roll back.")
        return 0

    for r in rows:
        if r["down_sql"] is None:
            print(
                f"ERROR: migration {r['version']:03d}_{r['name']} has no down_sql "
                f"(irreversible). Aborting.",
                file=sys.stderr,
            )
            return 2
        print(f"Rolling back {r['version']:03d}_{r['name']} ...")
        async with conn.transaction():
            await conn.execute(r["down_sql"])
            await conn.execute(
                "DELETE FROM public.schema_migrations WHERE version = $1",
                r["version"],
            )
        print("  rolled back.")

    print(f"\nRolled back {len(rows)} migration(s).")
    return 0


async def cmd_bootstrap(conn: asyncpg.Connection) -> int:
    """Register PREEXISTING_VERSIONS as applied without running them.

    Use this once after deploying the runner against a DB that already has
    older migrations applied (e.g. 001/002 applied via raw psql before the
    runner existed). Idempotent.
    """
    await ensure_meta_table(conn)
    applied = await _fetch_applied(conn)

    registered = []
    for version, name in PREEXISTING_VERSIONS.items():
        if version in applied:
            continue
        await conn.execute(
            "INSERT INTO public.schema_migrations (version, name, down_sql) "
            "VALUES ($1, $2, NULL)",
            version,
            name,
        )
        registered.append((version, name))

    if not registered:
        print("Bootstrap: nothing to do (all pre-existing migrations registered).")
        return 0

    print(f"Bootstrap: registered {len(registered)} pre-existing migration(s):")
    for v, n in registered:
        print(f"  {v:03d}_{n}")
    return 0


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


# Matches the password portion of `postgresql://user:pass@host/db` etc.
_DSN_PASSWORD_RE = re.compile(r"(://[^:/@]+:)[^@]*(@)")


def _redact_dsn(dsn: str) -> str:
    """Replace the password in postgres://user:pass@host/db with ``***``.

    Leaves user, host, port, and database visible so the operator can
    verify they're targeting the right database before confirming a
    mutating command.
    """
    return _DSN_PASSWORD_RE.sub(r"\1***\2", dsn)


def _confirm(action: str, dsn: str, *, yes: bool) -> bool:
    """Ask the operator to confirm a state-mutating action.

    Returns True iff the operator typed ``y`` / ``yes`` (case-insensitive)
    or ``yes=True`` was passed (i.e. ``--yes`` on the CLI). If stdin is
    not a TTY and ``yes=False``, refuses without prompting so we don't
    silently proceed under cron / CI.
    """
    if yes:
        return True
    print(f"About to: {action}", file=sys.stderr)
    print(f"Target:   {_redact_dsn(dsn)}", file=sys.stderr)
    if not sys.stdin.isatty():
        print(
            "ERROR: stdin is not a TTY; pass --yes (or -y) to confirm.",
            file=sys.stderr,
        )
        return False
    try:
        response = input("Continue? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        return False
    return response in ("y", "yes")


def _make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="database.runner",
        description=(
            "Versioned SQL migration runner. Reads database/migrations/. "
            "DATABASE_URL env var sets the connection string."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show applied and pending migrations.")

    up = sub.add_parser("up", help="Apply pending migrations in order.")
    up.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Apply at most N pending migrations.",
    )
    up.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip the confirmation prompt.",
    )

    down = sub.add_parser("down", help="Roll back the most-recent migration(s).")
    down.add_argument(
        "--limit",
        type=int,
        default=1,
        help="Roll back N migrations (default 1).",
    )
    down.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip the confirmation prompt.",
    )

    boot = sub.add_parser(
        "bootstrap",
        help="Register pre-runner migrations (001, 002) as applied without running them.",
    )
    boot.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip the confirmation prompt.",
    )

    return p


def _action_label(args: argparse.Namespace) -> str:
    """Human-readable action label shown in the confirmation prompt.

    Branches per subcommand so each path only references the args
    attributes its own subparser declared. A dict literal here would
    eagerly evaluate every entry (including ``args.limit`` on the
    ``down`` entry), which crashed bootstrap — that subparser has no
    ``--limit`` flag.
    """
    if args.command == "up":
        return (
            f"apply pending migrations (limit={args.limit})"
            if args.limit is not None else "apply ALL pending migrations"
        )
    if args.command == "down":
        return f"roll back the {args.limit} most-recent migration(s)"
    if args.command == "bootstrap":
        return (
            "register pre-runner migrations as applied "
            f"({', '.join(f'{v:03d}' for v in PREEXISTING_VERSIONS)})"
        )
    raise ValueError(f"unexpected mutating command: {args.command}")


def _resolve_dsn() -> str | None:
    """DATABASE_URL from the environment, falling back to the repo's
    canonical .env (qa-automation/AI-Scoring/.env — the one every other
    script load_dotenv()s). The runner stays shell-driven first: an
    exported DATABASE_URL always wins; the fallback only fills the gap
    when invoked from a fresh shell. python-dotenv also parses values
    that a plain `source` mangles (e.g. unquoted values with spaces)."""
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return dsn
    env_path = REPO_ROOT / "qa-automation" / "AI-Scoring" / ".env"
    try:
        from dotenv import load_dotenv
    except ImportError:
        return None
    if env_path.exists():
        load_dotenv(env_path)
        dsn = os.environ.get("DATABASE_URL")
        if dsn:
            print(f"(DATABASE_URL loaded from {env_path})", file=sys.stderr)
    return dsn


async def main_async(args: argparse.Namespace) -> int:
    dsn = _resolve_dsn()
    if not dsn:
        print(
            "ERROR: DATABASE_URL not set in environment "
            "(and not found in qa-automation/AI-Scoring/.env).",
            file=sys.stderr,
        )
        return 1

    # Mutating commands: show what we're about to do and prompt for y/N.
    if args.command in ("up", "down", "bootstrap"):
        if not _confirm(_action_label(args), dsn,
                        yes=getattr(args, "yes", False)):
            print("Aborted.", file=sys.stderr)
            return 1

    conn = await asyncpg.connect(dsn)
    try:
        if args.command == "status":
            return await cmd_status(conn)
        if args.command == "up":
            return await cmd_up(conn, limit=args.limit)
        if args.command == "down":
            return await cmd_down(conn, limit=args.limit)
        if args.command == "bootstrap":
            return await cmd_bootstrap(conn)
        print(f"Unknown command: {args.command}", file=sys.stderr)
        return 1
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = _make_parser()
    args = parser.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
