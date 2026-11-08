"""Single-tournament Craftcore 2v2 data layer. Legacy bot tables are never altered."""
import csv
import io
import json
import sqlite3
from datetime import datetime, timezone

NAME = "CRAFTCORE 2v2 TOURNAMENT"
MODES = ("Axe PvP", "Spear Mace", "Mace", "Cart PvP (HT ONLY)", "Diamond SMP (Legacy)", "Diamond Pot", "UHC", "Sword", "SMP")
ORDER = (("ban","a"),("ban","b"),("pick","a"),("pick","b"),("pick","a"),("pick","b"),("ban","a"),("ban","b"),("decider",None))
INITIAL = (
("Team Levi","levi","glova"),("Team Yugo","Yugo","Sandwich"),("Team Asharam","Asharam","Aevia"),("Team grape214","grape214","borius"),
("Team Kawaii","Kawaii Gal","Kawaii Boi"),("Team Firepro","Firepro","Dpie"),("Team motives","motives","frostivity"),("Team Terabyte","Terabyte","Founder Cell"),
("Team Itsuki","Itsuki","Tyrx"),("Team Hasdjd","Hasdjd1234","MaskedCube"),("Team KKalataa","KKalataa","Prask0"),("Team Vyrqz","Vyrqz_","Parasolsl"),
("Team karlstun","karlstun","Notkareem124"),("Team jambaza","jambaza","HollowInfinite"),("Team jelka","jelka","vejsilie"),("Team riuchantou","riuchantou","conqueror"))
DEFS = (
("R1-L1","Round 1","Left · Match 1","Team Levi","Team Yugo",None,None),
("R1-L2","Round 1","Left · Match 2","Team Asharam","Team grape214",None,None),
("R1-L3","Round 1","Left · Match 3","Team Kawaii","Team Firepro",None,None),
("R1-L4","Round 1","Left · Match 4","Team motives","Team Terabyte",None,None),
("R1-R1","Round 1","Right · Match 1","Team Itsuki","Team Hasdjd",None,None),
("R1-R2","Round 1","Right · Match 2","Team KKalataa","Team Vyrqz",None,None),
("R1-R3","Round 1","Right · Match 3","Team karlstun","Team jambaza",None,None),
("R1-R4","Round 1","Right · Match 4","Team jelka","Team riuchantou",None,None),
("QF-L1","Quarterfinals","Left · Quarterfinal 1",None,None,"R1-L1","R1-L2"),
("QF-L2","Quarterfinals","Left · Quarterfinal 2",None,None,"R1-L3","R1-L4"),
("QF-R1","Quarterfinals","Right · Quarterfinal 1",None,None,"R1-R1","R1-R2"),
("QF-R2","Quarterfinals","Right · Quarterfinal 2",None,None,"R1-R3","R1-R4"),
("SF-L","Semifinals","Left · Semifinal",None,None,"QF-L1","QF-L2"),
("SF-R","Semifinals","Right · Semifinal",None,None,"QF-R1","QF-R2"),
("GF","Grand Final","Grand Final",None,None,"SF-L","SF-R"))
KEYS = {d[0] for d in DEFS}
class DownstreamResultsExist(ValueError):
    def __init__(self, keys):
        self.match_keys = keys
        super().__init__("Completed downstream matches need confirmation before they are cleared.")

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def clean(value, label, limit):
    if not isinstance(value,str): raise ValueError(f"{label} must be text.")
    value=value.strip()
    if not value or len(value)>limit or any(ord(c)<32 for c in value): raise ValueError(f"{label} must contain 1–{limit} printable characters.")
    return value

def audit(db, action, details, actor=None):
    db.execute("INSERT INTO cc_audit_log(actor_id,action,details,created_at) VALUES(?,?,?,?)",(actor,action,json.dumps(details,ensure_ascii=False,sort_keys=True),now()))

def ensure_schema(db):
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS cc_tournament_teams(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE COLLATE NOCASE,seed INTEGER UNIQUE CHECK(seed IS NULL OR seed BETWEEN 1 AND 16),created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS cc_tournament_players(username TEXT PRIMARY KEY COLLATE NOCASE,team_id INTEGER NOT NULL REFERENCES cc_tournament_teams(id) ON DELETE CASCADE,slot INTEGER NOT NULL CHECK(slot IN (1,2)),UNIQUE(team_id,slot));
    CREATE TABLE IF NOT EXISTS cc_discord_links(username TEXT PRIMARY KEY COLLATE NOCASE,discord_id INTEGER NOT NULL UNIQUE,linked_by INTEGER,linked_at TEXT NOT NULL,FOREIGN KEY(username) REFERENCES cc_tournament_players(username) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS cc_bracket_matches(match_key TEXT PRIMARY KEY,stage TEXT NOT NULL,label TEXT NOT NULL,team_a_id INTEGER REFERENCES cc_tournament_teams(id),team_b_id INTEGER REFERENCES cc_tournament_teams(id),winner_team_id INTEGER REFERENCES cc_tournament_teams(id),source_a TEXT,source_b TEXT,score_a INTEGER,score_b INTEGER,result_type TEXT,referee TEXT,status TEXT NOT NULL DEFAULT 'waiting',updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS cc_match_history(id INTEGER PRIMARY KEY AUTOINCREMENT,match_key TEXT NOT NULL,action TEXT NOT NULL,actor_id INTEGER,old_data TEXT,new_data TEXT,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS cc_draft_selections(match_key TEXT NOT NULL REFERENCES cc_bracket_matches(match_key) ON DELETE CASCADE,step INTEGER NOT NULL CHECK(step BETWEEN 1 AND 9),action TEXT NOT NULL,side TEXT,team_id INTEGER REFERENCES cc_tournament_teams(id),gamemode TEXT NOT NULL,actor_id INTEGER,created_at TEXT NOT NULL,PRIMARY KEY(match_key,step),UNIQUE(match_key,gamemode));
    CREATE TABLE IF NOT EXISTS cc_tournament_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS cc_audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT,actor_id INTEGER,action TEXT NOT NULL,details TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS cc_coin_transactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        delta INTEGER NOT NULL,
        balance_after INTEGER NOT NULL,
        reason TEXT NOT NULL,
        actor_id INTEGER,
        match_id INTEGER,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS cc_wallet_controls(
        user_id INTEGER PRIMARY KEY,
        frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0,1)),
        updated_by INTEGER,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS cc_match_disputes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_key TEXT NOT NULL,
        raised_by INTEGER NOT NULL,
        reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
        previous_legacy_status TEXT,
        resolved_by INTEGER,
        resolution TEXT,
        created_at TEXT NOT NULL,
        resolved_at TEXT
    );
    CREATE INDEX IF NOT EXISTS cc_match_disputes_active_idx ON cc_match_disputes(match_key,status);
    CREATE TABLE IF NOT EXISTS cc_panel_messages(
        panel_key TEXT PRIMARY KEY,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS cc_awards(
        award_key TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        description TEXT,
        awarded_by INTEGER,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS cc_giveaway_draws(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        discord_id INTEGER,
        eligible_count INTEGER NOT NULL,
        drawn_by INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        claim_status TEXT NOT NULL DEFAULT 'unclaimed'
            CHECK(claim_status IN ('unclaimed','claimed','delivered')),
        claimed_by INTEGER,
        claimed_at TEXT,
        delivery_confirmed_by INTEGER,
        delivery_confirmed_at TEXT
    );
    """)
    # Safe additive migrations for columns added after the initial deployment.
    migrations = {
        "cc_tournament_teams": {
            "legacy_team_id": "INTEGER"
        },
        "cc_bracket_matches": {
            "legacy_match_id": "INTEGER",
            "forfeit_team_id": "INTEGER",
            "disqualified_team_id": "INTEGER"
        },
    }
    for table_name, columns in migrations.items():
        present = {row["name"] for row in db.execute(f"PRAGMA table_info({table_name})")}
        for column_name, definition in columns.items():
            if column_name not in present:
                db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
    db.commit()

def teams(db):
    return [dict(r) for r in db.execute("""SELECT t.id,t.name,t.seed,MAX(CASE WHEN p.slot=1 THEN p.username END) player1,MAX(CASE WHEN p.slot=2 THEN p.username END) player2 FROM cc_tournament_teams t LEFT JOIN cc_tournament_players p ON p.team_id=t.id GROUP BY t.id ORDER BY COALESCE(t.seed,99),t.name COLLATE NOCASE""")]
def team(db, key):
    if isinstance(key,int): row=db.execute("SELECT * FROM cc_tournament_teams WHERE id=?",(key,)).fetchone()
    else: row=db.execute("SELECT * FROM cc_tournament_teams WHERE name=? COLLATE NOCASE",(key,)).fetchone()
    if not row:return None
    row=dict(row); p={r["slot"]:r["username"] for r in db.execute("SELECT slot,username FROM cc_tournament_players WHERE team_id=?",(row["id"],))}
    return {**row,"player1":p.get(1),"player2":p.get(2)}
def create_team(db,name,p1,p2,seed=None,actor=None):
    name,p1,p2=clean(name,"Team name",50),clean(p1,"Player 1 username",32),clean(p2,"Player 2 username",32)
    if p1.casefold()==p2.casefold(): raise ValueError("A player cannot occupy both places on a team.")
    if seed is not None and not 1<=int(seed)<=16: raise ValueError("Seed must be between 1 and 16.")
    if db.execute("SELECT COUNT(*) FROM cc_tournament_teams").fetchone()[0]>=16: raise ValueError("This is a fixed 16-team tournament; all 16 places are registered.")
    try:
        with db:
            cur=db.execute("INSERT INTO cc_tournament_teams(name,seed,created_at) VALUES(?,?,?)",(name,seed,now())); tid=cur.lastrowid
            db.executemany("INSERT INTO cc_tournament_players(username,team_id,slot) VALUES(?,?,?)",((p1,tid,1),(p2,tid,2)))
            audit(db,"team_created",{"id":tid,"name":name,"players":[p1,p2],"seed":seed},actor)
        return tid
    except sqlite3.IntegrityError as e:
        msg=str(e).lower()
        if "username" in msg: raise ValueError("One or both Minecraft usernames are already registered.") from e
        if "seed" in msg: raise ValueError(f"Seed {seed} is already assigned.") from e
        if "name" in msg: raise ValueError(f"Team '{name}' already exists.") from e
        raise ValueError(f"Could not create team: {e}") from e
def edit_team(db,tid,name,p1,p2,seed=None,actor=None):
    old=team(db,tid)
    if not old: raise ValueError("Team not found.")
    name,p1,p2=clean(name,"Team name",50),clean(p1,"Player 1 username",32),clean(p2,"Player 2 username",32)
    if p1.casefold()==p2.casefold(): raise ValueError("Player usernames must be different.")
    if seed is not None and not 1<=int(seed)<=16: raise ValueError("Seed must be between 1 and 16.")
    try:
        with db:
            db.execute("UPDATE cc_tournament_teams SET name=?,seed=? WHERE id=?",(name,seed,tid))
            db.execute("DELETE FROM cc_tournament_players WHERE team_id=?",(tid,))
            db.executemany("INSERT INTO cc_tournament_players(username,team_id,slot) VALUES(?,?,?)",((p1,tid,1),(p2,tid,2)))
            audit(db,"team_edited",{"before":old,"after":{"name":name,"player1":p1,"player2":p2,"seed":seed}},actor)
    except sqlite3.IntegrityError as e:
        raise ValueError("Team name, Minecraft username, or seed is already in use.") from e
def delete_team(db,tid,actor=None):
    t=team(db,tid)
    if not t: raise ValueError("Team not found.")
    if db.execute("SELECT 1 FROM cc_bracket_matches WHERE team_a_id=? OR team_b_id=?",(tid,tid)).fetchone(): raise ValueError("This team is part of the fixed bracket and cannot be deleted.")
    with db: db.execute("DELETE FROM cc_tournament_teams WHERE id=?",(tid,)); audit(db,"team_deleted",t,actor)

def seed_initial_teams(db,actor=None):
    current=teams(db)
    present={t["name"].casefold() for t in current}
    missing=sum(1 for n,_,_ in INITIAL if n.casefold() not in present)
    if len(current)+missing>16:
        raise ValueError("Initial seeding would exceed the fixed 16-team tournament; remove unrelated unassigned teams first.")
    added=0
    with db:
        for seed,(name,p1,p2) in enumerate(INITIAL,1):
            old=team(db,name)
            if old:
                if (old["player1"],old["player2"])!=(p1,p2): raise ValueError(f"Existing '{name}' conflicts with the supplied roster; nothing was overwritten.")
                if old["seed"] is None: db.execute("UPDATE cc_tournament_teams SET seed=? WHERE id=?",(seed,old["id"]))
                continue
            if db.execute("SELECT 1 FROM cc_tournament_teams WHERE seed=?",(seed,)).fetchone(): raise ValueError(f"Seed {seed} is already assigned.")
            try:
                cur=db.execute("INSERT INTO cc_tournament_teams(name,seed,created_at) VALUES(?,?,?)",(name,seed,now())); tid=cur.lastrowid
                db.executemany("INSERT INTO cc_tournament_players(username,team_id,slot) VALUES(?,?,?)",((p1,tid,1),(p2,tid,2))); added+=1
            except sqlite3.IntegrityError as e: raise ValueError(f"Could not register '{name}': duplicate team, player, or seed.") from e
        if added:audit(db,"initial_roster_seeded",{"added":added},actor)
    return added

def initialise_bracket(db,actor=None):
    ids={t["name"].casefold():t["id"] for t in teams(db)}
    missing=[n for n,_,_ in INITIAL if n.casefold() not in ids]
    if missing: raise ValueError("Missing initial teams: "+", ".join(missing))
    n=db.execute("SELECT COUNT(*) FROM cc_bracket_matches").fetchone()[0]
    if n:
        if n!=15: raise ValueError("A partial bracket exists; refusing to overwrite.")
        return False
    with db:
        for key,stage,label,a,b,sa,sb in DEFS:
            ta=ids[a.casefold()] if a else None; tb=ids[b.casefold()] if b else None
            db.execute("INSERT INTO cc_bracket_matches(match_key,stage,label,team_a_id,team_b_id,source_a,source_b,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",(key,stage,label,ta,tb,sa,sb,"ready" if ta and tb else "waiting",now()))
        audit(db,"bracket_initialised",{"matches":15},actor)
    return True
def get_match(db,key):
    r=db.execute("SELECT * FROM cc_bracket_matches WHERE match_key=?",(key,)).fetchone()
    return dict(r) if r else None
def name(db,tid):
    r=db.execute("SELECT name FROM cc_tournament_teams WHERE id=?",(tid,)).fetchone()
    return r["name"] if r else "TBD"
def descendants(key):
    found=[]; front=[key]
    while front:
        parent=front.pop(0)
        children=[d[0] for d in DEFS if parent in d[5:7] and d[0] not in found]
        found.extend(children); front.extend(children)
    return found
def refresh(db):
    for key,stage,label,a,b,sa,sb in DEFS:
        m=get_match(db,key)
        ta=(get_match(db,sa)["winner_team_id"] if sa else m["team_a_id"])
        tb=(get_match(db,sb)["winner_team_id"] if sb else m["team_b_id"])
        status="completed" if m["winner_team_id"] is not None else ("ready" if ta is not None and tb is not None else "waiting")
        db.execute("UPDATE cc_bracket_matches SET team_a_id=?,team_b_id=?,status=?,updated_at=? WHERE match_key=?",(ta,tb,status,now(),key))
    gf=get_match(db,"GF")
    if gf["winner_team_id"] is None: db.execute("DELETE FROM cc_tournament_settings WHERE key='champion_team_id'")
    else: db.execute("INSERT INTO cc_tournament_settings(key,value) VALUES('champion_team_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(gf["winner_team_id"]),))
def snapshot(m):
    return {k:m[k] for k in ("winner_team_id","score_a","score_b","result_type","referee","status","forfeit_team_id","disqualified_team_id")}
def set_result(db,key,winner,score_a=None,score_b=None,result_type="normal",referee=None,actor=None,confirm=False,forfeit_team=None,disqualified_team=None):
    if key not in KEYS: raise ValueError("Unknown match ID.")
    m=get_match(db,key)
    if not m: raise ValueError("Bracket is not initialised.")
    if m["team_a_id"] is None or m["team_b_id"] is None: raise ValueError("Both teams must be available before selecting a winner.")
    if (score_a is None)!=(score_b is None) or any(x is not None and (not isinstance(x,int) or x<0) for x in (score_a,score_b)): raise ValueError("Enter both scores as non-negative whole numbers.")
    if db.execute("SELECT 1 FROM cc_match_disputes WHERE match_key=? AND status='open' LIMIT 1",(key,)).fetchone():
        raise ValueError("This match has an open dispute; staff must resolve it before recording a result.")
    if winner is not None and winner not in (m["team_a_id"],m["team_b_id"]): raise ValueError("Winner is not a participant in this match.")
    if winner is None:
        score_a=score_b=None; result_type=None; forfeit_team=None; disqualified_team=None
    elif result_type not in ("normal","forfeit","disqualification","override"): raise ValueError("Invalid result type.")
    if result_type=="forfeit":
        if forfeit_team is None: forfeit_team=m["team_b_id"] if winner==m["team_a_id"] else m["team_a_id"]
        if forfeit_team not in (m["team_a_id"],m["team_b_id"]) or forfeit_team==winner:
            raise ValueError("Forfeiting team must be the losing team in this match.")
    else: forfeit_team=None
    if result_type=="disqualification":
        if disqualified_team is None: disqualified_team=m["team_b_id"] if winner==m["team_a_id"] else m["team_a_id"]
        if disqualified_team not in (m["team_a_id"],m["team_b_id"]) or disqualified_team==winner:
            raise ValueError("Disqualified team must be the losing team in this match.")
    else: disqualified_team=None
    if score_a is not None:
        if score_a==score_b: raise ValueError("Scores cannot be tied.")
        score_winner=m["team_a_id"] if score_a>score_b else m["team_b_id"]
        if result_type=="normal" and score_winner!=winner: raise ValueError("Winner disagrees with the higher score; use an override if intentional.")
    if referee is not None: referee=clean(referee,"Referee",80)
    old=snapshot(m); new={"winner_team_id":winner,"score_a":score_a,"score_b":score_b,"result_type":result_type,"referee":referee,"forfeit_team_id":forfeit_team,"disqualified_team_id":disqualified_team,"status":"completed" if winner is not None else "ready"}
    changed=old["winner_team_id"]!=winner
    done=[k for k in descendants(key) if get_match(db,k)["winner_team_id"] is not None] if changed else []
    if done and not confirm: raise DownstreamResultsExist(done)
    invalid=descendants(key) if changed else []
    with db:
        for child in invalid:
            cm=get_match(db,child); n=db.execute("SELECT COUNT(*) FROM cc_draft_selections WHERE match_key=?",(child,)).fetchone()[0]
            if cm["winner_team_id"] is not None or n:
                db.execute("INSERT INTO cc_match_history(match_key,action,actor_id,old_data,new_data,created_at) VALUES(?,?,?,?,?,?)",(child,"downstream_invalidated",actor,json.dumps(snapshot(cm),sort_keys=True),json.dumps({"upstream":key,"draft_steps_removed":n}),now()))
            if n: db.execute("DELETE FROM cc_draft_selections WHERE match_key=?",(child,))
            if child in done: db.execute("UPDATE cc_bracket_matches SET winner_team_id=NULL,score_a=NULL,score_b=NULL,result_type=NULL,referee=NULL,forfeit_team_id=NULL,disqualified_team_id=NULL,status='waiting',updated_at=? WHERE match_key=?",(now(),child))
        db.execute("UPDATE cc_bracket_matches SET winner_team_id=?,score_a=?,score_b=?,result_type=?,referee=?,forfeit_team_id=?,disqualified_team_id=?,status=?,updated_at=? WHERE match_key=?",(winner,score_a,score_b,result_type,referee,forfeit_team,disqualified_team,new["status"],now(),key))
        db.execute("INSERT INTO cc_match_history(match_key,action,actor_id,old_data,new_data,created_at) VALUES(?,?,?,?,?,?)",(key,"result_set" if winner is not None else "result_cleared",actor,json.dumps(old,sort_keys=True),json.dumps(new,sort_keys=True),now()))
        refresh(db); audit(db,"bracket_result_changed",{"match":key,"before":old,"after":new,"invalidated":done},actor)
    return done
def bracket(db):
    out=[]
    for key,stage,label,*_ in DEFS:
        m=get_match(db,key)
        if not m:continue
        out.append({**m,"team_a_name":name(db,m["team_a_id"]),"team_b_name":name(db,m["team_b_id"]),"winner_name":name(db,m["winner_team_id"]) if m["winner_team_id"] else None})
    return out
def reset_bracket(db,actor=None):
    if db.execute("SELECT COUNT(*) FROM cc_bracket_matches").fetchone()[0]!=15: raise ValueError("Bracket is not initialised.")
    with db:
        for key in KEYS:
            m=get_match(db,key)
            if m["winner_team_id"] is not None: db.execute("INSERT INTO cc_match_history(match_key,action,actor_id,old_data,new_data,created_at) VALUES(?,?,?,?,?,?)",(key,"bracket_reset",actor,json.dumps(snapshot(m),sort_keys=True),"{}",now()))
        db.execute("DELETE FROM cc_draft_selections")
        db.execute("UPDATE cc_bracket_matches SET winner_team_id=NULL,score_a=NULL,score_b=NULL,result_type=NULL,referee=NULL,forfeit_team_id=NULL,disqualified_team_id=NULL,status='waiting',updated_at=?",(now(),))
        refresh(db); audit(db,"bracket_reset",{"teams_and_legacy_economy_preserved":True},actor)
def history(db,key,limit=20):
    if key not in KEYS: raise ValueError("Unknown match ID.")
    return [dict(r) for r in db.execute("SELECT * FROM cc_match_history WHERE match_key=? ORDER BY id DESC LIMIT ?",(key,max(1,min(limit,50))))]
def draft(db,key):
    m=get_match(db,key)
    if not m: raise ValueError("Unknown match or bracket not initialised.")
    rows=[dict(r) for r in db.execute("SELECT * FROM cc_draft_selections WHERE match_key=? ORDER BY step",(key,))]
    used={r["gamemode"] for r in rows}
    return {"match":m,"selections":rows,"next_step":len(rows)+1,"complete":len(rows)==9,"remaining":[x for x in MODES if x not in used]}
def draft_add(db,key,mode,actor=None):
    state=draft(db,key); m=state["match"]
    if m["status"]=="completed": raise ValueError("Match is already completed.")
    if m["team_a_id"] is None or m["team_b_id"] is None: raise ValueError("Both teams must be available before drafting.")
    if state["complete"]: raise ValueError("Draft is complete.")
    if mode not in state["remaining"]: raise ValueError("Gamemode is already selected or invalid.")
    step=state["next_step"]; action,side=ORDER[step-1]; tid=m["team_a_id"] if side=="a" else m["team_b_id"] if side=="b" else None
    with db:
        db.execute("INSERT INTO cc_draft_selections(match_key,step,action,side,team_id,gamemode,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?)",(key,step,action,side,tid,mode,actor,now()))
        audit(db,"draft_selection",{"match":key,"step":step,"action":action,"team_id":tid,"gamemode":mode},actor)
    return draft(db,key)
def draft_undo(db,key,actor=None):
    state=draft(db,key)
    if state["match"]["status"]=="completed": raise ValueError("Completed match draft cannot be edited.")
    if not state["selections"]: raise ValueError("Nothing to undo.")
    last=state["selections"][-1]
    with db: db.execute("DELETE FROM cc_draft_selections WHERE match_key=? AND step=?",(key,last["step"])); audit(db,"draft_undo",{"match":key,"step":last["step"]},actor)
    return last["step"]
def locked(db):
    r=db.execute("SELECT value FROM cc_tournament_settings WHERE key='registration_locked'").fetchone()
    return bool(r and r["value"]=="1")
def set_locked(db,value,actor=None):
    with db: db.execute("INSERT INTO cc_tournament_settings(key,value) VALUES('registration_locked',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",("1" if value else "0",)); audit(db,"registration_lock_changed",{"locked":bool(value)},actor)
def parse_teams(filename,raw):
    content=raw.decode("utf-8-sig")
    if filename.lower().endswith(".csv"): rows=list(csv.DictReader(io.StringIO(content)))
    elif filename.lower().endswith(".json"):
        obj=json.loads(content); rows=obj.get("teams") if isinstance(obj,dict) else obj
    else: raise ValueError("Upload a .csv or .json file.")
    if not isinstance(rows,list) or not rows: raise ValueError("Import file must contain a non-empty list of teams.")
    return rows
def import_teams(db,rows,actor=None):
    parsed=[]; seen_names=set(); seen_players=set(); seen_seeds=set()
    for i,r in enumerate(rows,1):
        try:
            n=clean(str(r.get("name","")),"Team name",50); p1=clean(str(r.get("player1","")),"Player 1",32); p2=clean(str(r.get("player2","")),"Player 2",32)
            seed=int(r["seed"]) if r.get("seed") not in (None,"") else None
            if seed is not None and not 1<=seed<=16: raise ValueError("Seed must be 1–16.")
            if p1.casefold()==p2.casefold(): raise ValueError("Player names must differ.")
            if n.casefold() in seen_names: raise ValueError("Duplicate team name in file.")
            seen_names.add(n.casefold())
            for p in (p1,p2):
                if p.casefold() in seen_players: raise ValueError(f"Duplicate username in file: {p}")
                seen_players.add(p.casefold())
            if seed is not None:
                if seed in seen_seeds: raise ValueError(f"Duplicate seed {seed} in file.")
                seen_seeds.add(seed)
            parsed.append((n,p1,p2,seed))
        except Exception as e: raise ValueError(f"Import row {i}: {e}") from e
    if len(teams(db))+len([x for x in parsed if not team(db,x[0])])>16: raise ValueError("A single tournament supports exactly 16 teams.")
    add=[]
    for n,p1,p2,seed in parsed:
        old=team(db,n)
        if old:
            if (old["player1"],old["player2"])!=(p1,p2): raise ValueError(f"Import conflicts with existing team {n}.")
        else:add.append((n,p1,p2,seed))
    count=0
    try:
        with db:
            for n,p1,p2,seed in add:
                cur=db.execute("INSERT INTO cc_tournament_teams(name,seed,created_at) VALUES(?,?,?)",(n,seed,now())); tid=cur.lastrowid
                db.executemany("INSERT INTO cc_tournament_players(username,team_id,slot) VALUES(?,?,?)",((p1,tid,1),(p2,tid,2))); count+=1
            audit(db,"teams_imported",{"added":count,"already_present":len(parsed)-count},actor)
    except sqlite3.IntegrityError as e: raise ValueError("Import cancelled: team, username, or seed already exists.") from e
    return count
def export_teams(db,fmt="json"):
    rows=teams(db)
    if fmt=="csv":
        out=io.StringIO(newline=""); w=csv.DictWriter(out,fieldnames=("name","player1","player2","seed")); w.writeheader()
        for r in rows:w.writerow({k:r[k] for k in ("name","player1","player2","seed")})
        return out.getvalue()
    return json.dumps({"tournament":NAME,"teams":rows},ensure_ascii=False,indent=2)
def export_bracket(db):
    values=[{k:m[k] for k in ("match_key","winner_team_id","score_a","score_b","result_type","referee")} for m in bracket(db)]
    return json.dumps({"version":1,"tournament":NAME,"teams":teams(db),"matches":values},ensure_ascii=False,indent=2)
def import_bracket(db,raw,actor=None):
    try: obj=json.loads(raw.decode("utf-8-sig") if isinstance(raw,bytes) else raw)
    except Exception as e: raise ValueError("Invalid UTF-8 JSON backup.") from e
    if not isinstance(obj,dict) or obj.get("version")!=1 or obj.get("tournament")!=NAME: raise ValueError("Unsupported backup version or tournament.")
    local={t["id"]:t for t in teams(db)}; supplied=obj.get("teams")
    if not isinstance(supplied,list) or len(supplied)!=len(local): raise ValueError("Backup roster does not match current teams.")
    for t in supplied:
        x=local.get(t.get("id"))
        if not x or any(x[k]!=t.get(k) for k in ("name","player1","player2")): raise ValueError("Backup contains invalid team IDs or identities.")
    items=obj.get("matches")
    if not isinstance(items,list) or len(items)!=15: raise ValueError("Backup must contain all 15 bracket matches.")
    mm={}
    for it in items:
        if not isinstance(it,dict) or it.get("match_key") not in KEYS or it["match_key"] in mm: raise ValueError("Backup contains invalid or duplicate match IDs.")
        mm[it["match_key"]]=it
    if set(mm)!=KEYS: raise ValueError("Backup match IDs do not match the fixed bracket.")
    team_ids={t["name"].casefold():t["id"] for t in local.values()}; winners={k:mm[k].get("winner_team_id") for k in KEYS}
    for key,stage,label,a,b,sa,sb in DEFS:
        ta=team_ids[a.casefold()] if a else winners[sa]; tb=team_ids[b.casefold()] if b else winners[sb]; it=mm[key]; win=it.get("winner_team_id"); x,y=it.get("score_a"),it.get("score_b"); typ=it.get("result_type")
        if win is not None and (ta is None or tb is None or win not in (ta,tb)): raise ValueError(f"Invalid progression/winner in {key}.")
        if (x is None)!=(y is None): raise ValueError(f"Incomplete scores in {key}.")
        if x is not None and (not isinstance(x,int) or not isinstance(y,int) or min(x,y)<0 or x==y): raise ValueError(f"Invalid scores in {key}.")
        if x is not None and typ=="normal" and win!=(ta if x>y else tb): raise ValueError(f"Winner and scores disagree in {key}.")
        if win is not None and typ not in ("normal","forfeit","disqualification","override"): raise ValueError(f"Invalid result type in {key}.")
        if win is None and (x is not None or typ is not None): raise ValueError(f"Result data without winner in {key}.")
    with db:
        for key,*_ in DEFS:
            it=mm[key]; old=get_match(db,key)
            db.execute("INSERT INTO cc_match_history(match_key,action,actor_id,old_data,new_data,created_at) VALUES(?,?,?,?,?,?)",(key,"backup_import",actor,json.dumps(snapshot(old),sort_keys=True),json.dumps(it,sort_keys=True),now()))
            db.execute("UPDATE cc_bracket_matches SET winner_team_id=?,score_a=?,score_b=?,result_type=?,referee=?,status=?,updated_at=? WHERE match_key=?",(it.get("winner_team_id"),it.get("score_a"),it.get("score_b"),it.get("result_type"),it.get("referee"),"completed" if it.get("winner_team_id") is not None else "ready",now(),key))
        refresh(db); audit(db,"bracket_backup_imported",{"matches":15},actor)

def record_coin(db,user_id,delta,reason,actor=None,match_id=None):
    player=db.execute("SELECT balance FROM players WHERE user_id=?",(user_id,)).fetchone()
    if not player: raise ValueError("Wallet does not exist.")
    db.execute("INSERT INTO cc_coin_transactions(user_id,delta,balance_after,reason,actor_id,match_id,created_at) VALUES(?,?,?,?,?,?,?)",
        (user_id,int(delta),int(player["balance"]),reason,actor,match_id,now()))

def coin_history(db,user_id,limit=20):
    return [dict(r) for r in db.execute("SELECT * FROM cc_coin_transactions WHERE user_id=? ORDER BY id DESC LIMIT ?",(user_id,max(1,min(limit,50))) )]

def wallet_frozen(db,user_id):
    r=db.execute("SELECT frozen FROM cc_wallet_controls WHERE user_id=?",(user_id,)).fetchone()
    return bool(r and r["frozen"])

def set_wallet_frozen(db,user_id,frozen,actor=None):
    if not db.execute("SELECT 1 FROM players WHERE user_id=?",(user_id,)).fetchone():
        raise ValueError("Wallet does not exist.")
    with db:
        db.execute("INSERT INTO cc_wallet_controls(user_id,frozen,updated_by,updated_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET frozen=excluded.frozen,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (user_id,1 if frozen else 0,actor,now()))
        audit(db,"wallet_frozen" if frozen else "wallet_unfrozen",{"user_id":user_id},actor)


def set_legacy_team_id(db,team_id,legacy_team_id):
    with db:
        db.execute("UPDATE cc_tournament_teams SET legacy_team_id=? WHERE id=?",(legacy_team_id,team_id))

def set_legacy_match_id(db,match_key,legacy_match_id):
    with db:
        db.execute("UPDATE cc_bracket_matches SET legacy_match_id=? WHERE match_key=?",(legacy_match_id,match_key))

def sync_legacy_result(db,legacy_match_id,legacy_winner_id,forfeit_legacy_id=None,score_a=None,score_b=None,actor=None):
    row=db.execute("SELECT * FROM cc_bracket_matches WHERE legacy_match_id=?",(legacy_match_id,)).fetchone()
    if not row:
        return False
    winner=db.execute("SELECT id FROM cc_tournament_teams WHERE legacy_team_id=?",(legacy_winner_id,)).fetchone()
    if not winner:
        raise ValueError("Legacy winning team is not linked to a tournament team.")
    forfeiter=None
    result_type="normal"
    if forfeit_legacy_id is not None:
        forfeiter=db.execute("SELECT id FROM cc_tournament_teams WHERE legacy_team_id=?",(forfeit_legacy_id,)).fetchone()
        if not forfeiter:
            raise ValueError("Legacy forfeiting team is not linked to a tournament team.")
        result_type="forfeit"
    set_result(db,row["match_key"],winner["id"],score_a,score_b,result_type,
               actor=actor,forfeit_team=forfeiter["id"] if forfeiter else None)
    return True

def open_dispute(db,key,raised_by,reason,previous_legacy_status=None):
    if key not in KEYS or not get_match(db,key):
        raise ValueError("Unknown bracket match.")
    reason=clean(reason,"Dispute reason",500)
    if db.execute("SELECT 1 FROM cc_match_disputes WHERE match_key=? AND status='open'",(key,)).fetchone():
        raise ValueError("This match already has an open dispute.")
    if get_match(db,key)["status"]=="completed":
        raise ValueError("This match has already been settled. A completed result cannot be disputed through this command.")
    with db:
        cur=db.execute("INSERT INTO cc_match_disputes(match_key,raised_by,reason,status,previous_legacy_status,created_at) VALUES(?,?,?,'open',?,?)",(key,raised_by,reason,previous_legacy_status,now()))
        audit(db,"match_dispute_opened",{"match_key":key,"dispute_id":cur.lastrowid,"reason":reason},raised_by)
    return cur.lastrowid

def resolve_dispute(db,key,resolved_by,resolution):
    resolution=clean(resolution,"Resolution",500)
    row=db.execute("SELECT * FROM cc_match_disputes WHERE match_key=? AND status='open' ORDER BY id DESC LIMIT 1",(key,)).fetchone()
    if not row:
        raise ValueError("There is no open dispute for that match.")
    with db:
        db.execute("UPDATE cc_match_disputes SET status='resolved',resolved_by=?,resolution=?,resolved_at=? WHERE id=?",(resolved_by,resolution,now(),row["id"]))
        audit(db,"match_dispute_resolved",{"match_key":key,"dispute_id":row["id"],"resolution":resolution},resolved_by)
    return dict(row)

def dispute_history(db,key):
    if key not in KEYS:
        raise ValueError("Unknown bracket match.")
    return [dict(r) for r in db.execute("SELECT * FROM cc_match_disputes WHERE match_key=? ORDER BY id DESC",(key,))]

def assign_referee(db,key,referee,actor=None):
    if not get_match(db,key):
        raise ValueError("Unknown bracket match.")
    referee=clean(referee,"Referee",80)
    with db:
        old=get_match(db,key)
        db.execute("UPDATE cc_bracket_matches SET referee=?,updated_at=? WHERE match_key=?",(referee,now(),key))
        audit(db,"referee_assigned",{"match_key":key,"before":old["referee"],"after":referee},actor)

def draft_history(db,key,limit=20):
    if key not in KEYS:
        raise ValueError("Unknown match ID.")
    rows=db.execute("SELECT * FROM cc_audit_log WHERE action IN ('draft_selection','draft_undo') ORDER BY id DESC").fetchall()
    out=[]
    for row in rows:
        detail=json.loads(row["details"])
        if detail.get("match")==key or detail.get("match_key")==key:
            out.append({**dict(row),"details_parsed":detail})
            if len(out)>=max(1,min(limit,50)):
                break
    return out
