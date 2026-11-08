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


def test_postgres_legacy_match_result_settles_wager_with_row_lock(pgdb):
    tc.seed_initial_teams(pgdb)
    tc.initialise_bracket(pgdb)
    match=tc.get_match(pgdb,"R1-L1")
    ta=tc.team(pgdb,match["team_a_id"])
    tb=tc.team(pgdb,match["team_b_id"])
    legacy_a=pgdb.execute("INSERT INTO teams(name,p1,p2) VALUES(?,?,?)",(ta["name"],111,112)).lastrowid
    legacy_b=pgdb.execute("INSERT INTO teams(name,p1,p2) VALUES(?,?,?)",(tb["name"],113,114)).lastrowid
    tc.set_legacy_team_id(pgdb,ta["id"],legacy_a)
    tc.set_legacy_team_id(pgdb,tb["id"],legacy_b)
    legacy_match=pgdb.execute(
        "INSERT INTO matches(round,team_a,team_b,status) VALUES(?,?,?,'open')",
        ("Round 1",legacy_a,legacy_b),
    ).lastrowid
    tc.set_legacy_match_id(pgdb,"R1-L1",legacy_match)
    pgdb.execute("INSERT INTO players(user_id,balance) VALUES(?,?)",(9001,900))
    pgdb.execute(
        "INSERT INTO bets(match_id,user_id,team_id,amount,settled,payout) VALUES(?,?,?,100,0,0)",
        (legacy_match,9001,legacy_a),
    )
    tc.set_result(pgdb,"R1-L1",ta["id"])
    assert pgdb.execute("SELECT balance FROM players WHERE user_id=?",(9001,)).fetchone()["balance"]==1050
    assert pgdb.execute("SELECT payout FROM bets WHERE match_id=? AND user_id=?",(legacy_match,9001)).fetchone()["payout"]==150
    assert pgdb.execute("SELECT status FROM matches WHERE id=?",(legacy_match,)).fetchone()["status"]=="done"


def test_postgres_can_import_available_legacy_sqlite_file_once(pgdb,tmp_path):
    import sqlite3

    source=tmp_path/"craftcore.db"
    old=sqlite3.connect(source)
    old.executescript("""
        CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE players(user_id INTEGER PRIMARY KEY,balance INTEGER NOT NULL);
        CREATE TABLE teams(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE COLLATE NOCASE,p1 INTEGER NOT NULL,p2 INTEGER NOT NULL);
        CREATE TABLE matches(id INTEGER PRIMARY KEY AUTOINCREMENT,round TEXT NOT NULL,team_a INTEGER NOT NULL,team_b INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'open',winner INTEGER,forfeit_team INTEGER,pred_channel INTEGER,pred_msg INTEGER);
        CREATE TABLE draft(match_id INTEGER NOT NULL,step INTEGER NOT NULL,action TEXT NOT NULL,team_id INTEGER,gamemode TEXT NOT NULL,PRIMARY KEY(match_id,step));
        CREATE TABLE games(match_id INTEGER NOT NULL,game_no INTEGER NOT NULL,gamemode TEXT NOT NULL,score_a INTEGER NOT NULL,score_b INTEGER NOT NULL,PRIMARY KEY(match_id,game_no));
        CREATE TABLE bets(match_id INTEGER NOT NULL,user_id INTEGER NOT NULL,team_id INTEGER NOT NULL,amount INTEGER NOT NULL,settled INTEGER NOT NULL DEFAULT 0,payout INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(match_id,user_id));
    """)
    old.execute("INSERT INTO config VALUES('staff_role','123')")
    old.execute("INSERT INTO players VALUES(555,777)")
    old.execute("INSERT INTO teams(id,name,p1,p2) VALUES(8,'Old Team',101,102)")
    old.execute("INSERT INTO matches(id,round,team_a,team_b) VALUES(5,'Old Round',8,8)")
    old.execute("INSERT INTO bets(match_id,user_id,team_id,amount) VALUES(5,555,8,50)")
    old.commit();old.close()

    imported=pgdb.migrate_sqlite_legacy(str(source))
    assert imported==5
    assert pgdb.execute("SELECT balance FROM players WHERE user_id=?",(555,)).fetchone()["balance"]==777
    assert pgdb.execute("SELECT name FROM teams WHERE id=?",(8,)).fetchone()["name"]=="Old Team"
    assert pgdb.execute("SELECT amount FROM bets WHERE match_id=? AND user_id=?",(5,555)).fetchone()["amount"]==50
    new_id=pgdb.execute("INSERT INTO teams(name,p1,p2) VALUES(?,?,?)",("New Team",201,202)).lastrowid
    assert new_id>8
    assert pgdb.migrate_sqlite_legacy(str(source))==0


def test_postgres_transaction_rollback_and_casefold_uniqueness(pgdb):
    with pytest.raises(Exception):
        with pgdb:
            pgdb.execute("INSERT INTO players(user_id,balance) VALUES(?,?)",(77,100))
            pgdb.execute("INSERT INTO players(user_id,balance) VALUES(?,?)",(77,100))
    assert pgdb.execute("SELECT 1 FROM players WHERE user_id=?",(77,)).fetchone() is None
    tc.seed_initial_teams(pgdb)
    with pytest.raises(ValueError):
        tc.create_team(pgdb,"Extra team","LEVI","AnotherPlayer")
