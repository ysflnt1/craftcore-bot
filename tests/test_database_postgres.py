import os
import uuid

import pytest

from database import PostgresConnection
import tournament_core as tc


@pytest.fixture
def pgdb():
    dsn=os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is not configured.")
    db=PostgresConnection(dsn)
    schema="craftcore_test_"+uuid.uuid4().hex[:12]
    db.execute(f'CREATE SCHEMA "{schema}"')
    db.execute(f'SET search_path TO "{schema}"')
    db.executescript("""
        CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE players(user_id INTEGER PRIMARY KEY,balance INTEGER NOT NULL);
        CREATE TABLE teams(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            p1 INTEGER NOT NULL,p2 INTEGER NOT NULL
        );
        CREATE TABLE matches(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round TEXT NOT NULL,team_a INTEGER NOT NULL,team_b INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',winner INTEGER,forfeit_team INTEGER,
            pred_channel INTEGER,pred_msg INTEGER
        );
        CREATE TABLE draft(
            match_id INTEGER NOT NULL,step INTEGER NOT NULL,action TEXT NOT NULL,
            team_id INTEGER,gamemode TEXT NOT NULL,PRIMARY KEY(match_id,step)
        );
        CREATE TABLE games(
            match_id INTEGER NOT NULL,game_no INTEGER NOT NULL,gamemode TEXT NOT NULL,
            score_a INTEGER NOT NULL,score_b INTEGER NOT NULL,PRIMARY KEY(match_id,game_no)
        );
        CREATE TABLE bets(
            match_id INTEGER NOT NULL,user_id INTEGER NOT NULL,team_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,settled INTEGER NOT NULL DEFAULT 0,
            payout INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(match_id,user_id)
        );
    """)
    tc.ensure_schema(db)
    db.ensure_casefold_indexes()
    try:
        yield db
    finally:
        try:
            db.rollback()
            db.execute("SET search_path TO public")
            db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            db.close()


def test_postgres_sqlite_compatibility_and_generated_ids(pgdb):
    pgdb.execute("INSERT INTO players(user_id,balance) VALUES(?,?)",(123,100))
    pgdb.execute("INSERT OR IGNORE INTO players(user_id,balance) VALUES(?,?)",(123,999))
    row=pgdb.execute("SELECT * FROM players WHERE user_id=?",(123,)).fetchone()
    assert row["balance"]==100
    assert row[0]==123
    assert pgdb.execute("SELECT changes()").fetchone()[0] in (0,1)

    cursor=pgdb.execute("INSERT INTO teams(name,p1,p2) VALUES(?,?,?)",("Team A",1,2))
    assert cursor.lastrowid is not None
    assert pgdb.execute("SELECT name FROM teams WHERE name=? COLLATE NOCASE",("team a",)).fetchone()["name"]=="Team A"
    columns={r["name"] for r in pgdb.execute("PRAGMA table_info(teams)").fetchall()}
    assert {"id","name","p1","p2"} <= columns
    assert pgdb.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='players'").fetchone()


def test_postgres_tournament_schema_roster_bracket_and_draft(pgdb):
    assert tc.seed_initial_teams(pgdb)==16
    assert tc.initialise_bracket(pgdb)
    assert len(tc.teams(pgdb))==16
    assert len(tc.bracket(pgdb))==15
    first=tc.get_match(pgdb,"R1-L1")
    assert first["status"]=="ready"
    state=None
    for mode in tc.MODES:
        state=tc.draft_add(pgdb,"R1-L1",mode,42)
    assert state["complete"]
    winner=tc.get_match(pgdb,"R1-L1")["team_a_id"]
    tc.set_result(pgdb,"R1-L1",winner,3,1)
    assert tc.get_match(pgdb,"QF-L1")["team_a_id"]==winner
    assert pgdb.execute("SELECT COUNT(*) AS n FROM cc_draft_selections WHERE match_key=?",("R1-L1",)).fetchone()["n"]==9


def test_postgres_transaction_rollback_and_casefold_uniqueness(pgdb):
    with pytest.raises(Exception):
        with pgdb:
            pgdb.execute("INSERT INTO players(user_id,balance) VALUES(?,?)",(77,100))
            pgdb.execute("INSERT INTO players(user_id,balance) VALUES(?,?)",(77,100))
    assert pgdb.execute("SELECT 1 FROM players WHERE user_id=?",(77,)).fetchone() is None
    tc.seed_initial_teams(pgdb)
    with pytest.raises(ValueError):
        tc.create_team(pgdb,"Extra team","LEVI","AnotherPlayer")
