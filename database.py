"""Small SQLite-compatible database facade with optional Render PostgreSQL storage.

Local development keeps using SQLite. On Render, DATABASE_URL selects PostgreSQL.
The facade preserves the existing execute/fetchone/commit/rollback API so bot
commands can share one code path while new deployments use durable remote data.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("craftcore.database")

SERIAL_TABLES = {
    "teams", "matches", "cc_tournament_teams", "cc_match_history",
    "cc_audit_log", "cc_coin_transactions", "cc_match_disputes",
    "cc_announcements", "cc_giveaway_draws",
}
LEGACY_TABLES = ("config", "players", "teams", "matches", "draft", "games", "bets")


class HybridRow(dict):
    """Mapping rows that also accept SQLite-style positional indexing."""
    def __init__(self, values: dict[str, Any]):
        super().__init__(values)
        self._values = tuple(values.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class Cursor:
    def __init__(self, cursor=None, lastrowid=None, special_rows=None, rowcount=None):
        self._cursor = cursor
        self._lastrowid = lastrowid
        self._special_rows = list(special_rows or [])
        self._rowcount = rowcount if rowcount is not None else (
            cursor.rowcount if cursor is not None else len(self._special_rows)
        )

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def rowcount(self):
        return self._rowcount

    def fetchone(self):
        if self._special_rows:
            return self._special_rows.pop(0)
        row = self._cursor.fetchone() if self._cursor is not None else None
        return HybridRow(row) if row is not None and not isinstance(row, HybridRow) else row

    def fetchall(self):
        if self._special_rows:
            rows, self._special_rows = self._special_rows, []
            return rows
        rows = self._cursor.fetchall() if self._cursor is not None else []
        return [HybridRow(row) if row is not None and not isinstance(row, HybridRow) else row for row in rows]

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                break
            yield row


class PostgresConnection:
    is_postgres = True

    def __init__(self, dsn: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "DATABASE_URL is set but psycopg is not installed. Install psycopg[binary]."
            ) from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self._dsn = dsn
        self._connection = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=15, autocommit=True)
        self._last_rowcount = -1
        self._closed = False

    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, value):
        # Kept for compatibility with the existing bot.py initialization.
        return None

    def _normalise(self, sql: str) -> tuple[str, bool, str | None]:
        statement = sql.strip()
        upper = statement.upper()

        if upper.startswith("PRAGMA FOREIGN_KEYS"):
            return "", False, None
        table_info = re.fullmatch(r"PRAGMA\s+TABLE_INFO\((\w+)\)", statement, re.I)
        if table_info:
            return (
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name=%s ORDER BY ordinal_position",
                False,
                table_info.group(1).lower(),
            )
        if "FROM SQLITE_MASTER" in upper:
            master = re.search(
                r"SELECT\s+1\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'\s+AND\s+name\s*=\s*(\?|['\"][^'\"]+['\"])",
                statement,
                flags=re.I,
            )
            if master:
                statement = (
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema=current_schema() AND table_name=" + master.group(1)
                )

        if upper.startswith("SELECT CHANGES()"):
            return "__CRAFTCORE_CHANGES__", False, None

        statement = re.sub(
            r"(\b[\w.]+\b)\s*=\s*\?\s+COLLATE\s+NOCASE",
            r"LOWER(\1)=LOWER(?)",
            statement,
            flags=re.I,
        )
        statement = re.sub(
            r"(\b[\w.]+\b)\s+LIKE\s+\?\s+COLLATE\s+NOCASE",
            r"LOWER(\1) LIKE LOWER(?)",
            statement,
            flags=re.I,
        )
        statement = re.sub(
            r"(\b[\w.]+\b)\s+COLLATE\s+NOCASE",
            r"LOWER(\1)",
            statement,
            flags=re.I,
        )
        statement = re.sub(
            r"datetime\(\s*'now'\s*\)",
            "TO_CHAR(CURRENT_TIMESTAMP, 'YYYY-MM-DD HH24:MI:SS')",
            statement,
            flags=re.I,
        )

        ignore = bool(re.match(r"INSERT\s+OR\s+IGNORE\s+INTO\s+", statement, re.I))
        if ignore:
            statement = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", statement, count=1, flags=re.I)

        if upper.startswith("CREATE TABLE") or upper.startswith("ALTER TABLE"):
            statement = re.sub(
                r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
                "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
                statement,
                flags=re.I,
            )
            statement = re.sub(r"\bINTEGER\b", "BIGINT", statement, flags=re.I)
            statement = re.sub(r"\s+AUTOINCREMENT\b", "", statement, flags=re.I)
            statement = re.sub(r"\s+COLLATE\s+NOCASE\b", "", statement, flags=re.I)

        # The pre-existing bot deliberately issues BEGIN IMMEDIATE for wallet
        # and result transactions. PostgreSQL uses BEGIN; callers lock rows
        # explicitly where concurrent balance writes are possible.
        statement = re.sub(r"^\s*BEGIN\s+IMMEDIATE\s*;?\s*$", "BEGIN", statement, flags=re.I)
        statement = statement.replace("?", "%s")
        return statement, ignore, None

    def execute(self, sql: str, parameters: Iterable[Any] = ()):
        statement, ignore, pragma_parameter = self._normalise(sql)
        if statement == "":
            return Cursor(special_rows=[], rowcount=0)
        if statement == "__CRAFTCORE_CHANGES__":
            return Cursor(special_rows=[HybridRow({"changes": self._last_rowcount})], rowcount=1)
        params = tuple(parameters or ())
        if pragma_parameter is not None:
            params = (pragma_parameter,)
        if statement == "BEGIN":
            # psycopg starts a transaction on the first statement by default.
            # Avoid issuing a nested BEGIN if a preceding read already opened it.
            from psycopg.pq import TransactionStatus
            if self._connection.info.transaction_status != TransactionStatus.IDLE:
                return Cursor(special_rows=[], rowcount=0)

        table_match = re.match(r"\s*INSERT\s+INTO\s+([a-zA-Z_]\w*)", statement, re.I)
        table_name = table_match.group(1).lower() if table_match else None
        add_returning = (
            table_name in SERIAL_TABLES
            and not re.search(r"\bRETURNING\b", statement, re.I)
            and not re.search(r"\bON\s+CONFLICT\b", statement, re.I)
        )
        if ignore and not re.search(r"\bON\s+CONFLICT\b", statement, re.I):
            statement = statement.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        if add_returning:
            statement = statement.rstrip().rstrip(";") + " RETURNING id"

        cursor = self._connection.cursor()
        try:
            cursor.execute(statement, params)
            self._last_rowcount = cursor.rowcount
            lastrowid = None
            if add_returning:
                row = cursor.fetchone()
                if row:
                    lastrowid = row["id"]
                return Cursor(cursor=None, lastrowid=lastrowid, rowcount=cursor.rowcount)
            return Cursor(cursor=cursor, rowcount=cursor.rowcount)
        except self._psycopg.IntegrityError as exc:
            cursor.close()
            raise sqlite3.IntegrityError(str(exc)) from exc
        except self._psycopg.DatabaseError as exc:
            cursor.close()
            raise sqlite3.OperationalError(str(exc)) from exc

    def executescript(self, script: str):
        try:
            for statement in script.split(";"):
                if statement.strip():
                    self.execute(statement)
            self.commit()
        except Exception:
            self.rollback()
            raise

    def executemany(self, sql: str, seq_of_parameters):
        statement, ignore, pragma_parameter = self._normalise(sql)
        if statement == "":
            return Cursor(special_rows=[], rowcount=0)
        if pragma_parameter is not None or statement == "__CRAFTCORE_CHANGES__":
            raise sqlite3.OperationalError("executemany is not supported for this database command.")
        if ignore and not re.search(r"\bON\s+CONFLICT\b", statement, re.I):
            statement = statement.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        cursor = self._connection.cursor()
        try:
            cursor.executemany(statement, [tuple(row) for row in seq_of_parameters])
            self._last_rowcount = cursor.rowcount
            return Cursor(cursor=cursor, rowcount=cursor.rowcount)
        except self._psycopg.IntegrityError as exc:
            cursor.close()
            raise sqlite3.IntegrityError(str(exc)) from exc
        except self._psycopg.DatabaseError as exc:
            cursor.close()
            raise sqlite3.OperationalError(str(exc)) from exc

    def ensure_casefold_indexes(self):
        statements = (
            "CREATE UNIQUE INDEX IF NOT EXISTS teams_name_ci_idx ON teams (LOWER(name))",
            "CREATE UNIQUE INDEX IF NOT EXISTS cc_teams_name_ci_idx ON cc_tournament_teams (LOWER(name))",
            "CREATE UNIQUE INDEX IF NOT EXISTS cc_players_username_ci_idx ON cc_tournament_players (LOWER(username))",
            "CREATE UNIQUE INDEX IF NOT EXISTS cc_links_username_ci_idx ON cc_discord_links (LOWER(username))",
            "CREATE UNIQUE INDEX IF NOT EXISTS cc_champion_claim_username_ci_idx ON cc_champion_prize_claims (LOWER(username))",
        )
        for statement in statements:
            self.execute(statement)
        self.commit()

    def migrate_sqlite_legacy(self, source_path: str):
        """Copy available legacy SQLite economy/game records once into a new PG DB."""
        source = Path(source_path)
        if not source.is_file() or source.resolve() == Path(":memory:"):
            return 0
        self.execute("""
            CREATE TABLE IF NOT EXISTS cc_sqlite_migrations(
                migration_key TEXT PRIMARY KEY,
                imported_at TEXT NOT NULL,
                imported_rows BIGINT NOT NULL
            )
        """)
        if self.execute("SELECT 1 FROM cc_sqlite_migrations WHERE migration_key=?", ("legacy-sqlite-v1",)).fetchone():
            self.commit()
            return 0

        old = sqlite3.connect(str(source))
        old.row_factory = sqlite3.Row
        total = 0
        try:
            with old:
                for table in LEGACY_TABLES:
                    exists = old.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone()
                    if not exists:
                        continue
                    old_columns = [r["name"] for r in old.execute(f"PRAGMA table_info({table})")]
                    new_columns = [
                        r["column_name"] for r in self.execute(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema=current_schema() AND table_name=?", (table,)
                        ).fetchall()
                    ]
                    columns = [c for c in old_columns if c in new_columns]
                    if not columns:
                        continue
                    names = ",".join(f'"{c}"' for c in columns)
                    placeholders = ",".join(["%s"] * len(columns))
                    sql = f'INSERT INTO "{table}" ({names}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'
                    for row in old.execute(f'SELECT {names} FROM "{table}"'):
                        self._connection.execute(sql, tuple(row[c] for c in columns))
                        total += 1
            for table in ("teams", "matches"):
                self._connection.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}','id'), "
                    f"GREATEST(COALESCE((SELECT MAX(id) FROM {table}),0)+1,1), false)"
                )
            self._connection.execute(
                "INSERT INTO cc_sqlite_migrations(migration_key,imported_at,imported_rows) "
                "VALUES('legacy-sqlite-v1',TO_CHAR(CURRENT_TIMESTAMP,'YYYY-MM-DD HH24:MI:SS'),%s)",
                (total,),
            )
            self.commit()
            log.info("Imported %s rows from the existing SQLite bot database.", total)
            return total
        except Exception:
            self.rollback()
            raise
        finally:
            old.close()

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()
        self._closed = True

    def __enter__(self):
        from psycopg.pq import TransactionStatus
        if self._connection.info.transaction_status == TransactionStatus.IDLE:
            self._connection.execute("BEGIN")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


def connect_database(database_url: str | None, sqlite_path: str):
    if database_url:
        log.info("Connecting to the configured PostgreSQL database.")
        connection = PostgresConnection(database_url)
        return connection
    if os.getenv("REQUIRE_DATABASE_URL", "").lower() in ("1", "true", "yes"):
        raise RuntimeError(
            "DATABASE_URL is required for this Render deployment. Configure the Render Postgres connection before starting the bot."
        )
    log.warning("DATABASE_URL is not set; using local SQLite at %s.", sqlite_path)
    connection = sqlite3.connect(sqlite_path)
    connection.row_factory = sqlite3.Row
    return connection
