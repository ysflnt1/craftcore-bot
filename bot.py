"""
Craftcore 2v2 Tournament Bot
- CraftCoin wagering on match winners (button + modal)
- Ban/Pick snake draft tracking
- Per-game scores, auto match completion, results channel overview
- Staff/admin commands, leaderboard, giveaway eligibility
"""
import logging
import os
import random
import re
import sqlite3
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional, makes commands appear instantly
START_COINS = int(os.getenv("STARTING_COINS", "100"))
WIN_MULT = float(os.getenv("WIN_MULTIPLIER", "1.5"))
DB_PATH = os.getenv("DB_PATH", "craftcore.db")

GAMEMODES = [
    "Axe PvP",
    "Spear Mace",
    "Mace",
    "Cart PvP (HT ONLY)",
    "Diamond SMP (Legacy)",
    "Diamond Pot",
    "UHC",
    "Sword",
    "SMP",
]

# (action, side)  side: "a" / "b" / None
DRAFT_ORDER = [
    ("ban", "a"),
    ("ban", "b"),
    ("pick", "a"),
    ("pick", "b"),
    ("pick", "a"),
    ("pick", "b"),
    ("ban", "a"),
    ("ban", "b"),
    ("decider", None),
]
# game number -> draft step that decides its gamemode
# (4 picks + decider = best of 5, first to WINS_NEEDED game wins)
GAME_STEPS = {1: 3, 2: 4, 3: 5, 4: 6, 5: 9}
WINS_NEEDED = 3

STATUS_TEXT = {
    "open": "🟢 Predictions open",
    "locked": "🔒 Predictions locked",
    "done": "✅ Finished",
    "cancelled": "❌ Cancelled",
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("craftcore")

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS players (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    p1 INTEGER NOT NULL,
    p2 INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round TEXT NOT NULL,
    team_a INTEGER NOT NULL,
    team_b INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    winner INTEGER,
    forfeit_team INTEGER,
    pred_channel INTEGER,
    pred_msg INTEGER
);
CREATE TABLE IF NOT EXISTS draft (
    match_id INTEGER NOT NULL,
    step INTEGER NOT NULL,
    action TEXT NOT NULL,
    team_id INTEGER,
    gamemode TEXT NOT NULL,
    PRIMARY KEY (match_id, step)
);
CREATE TABLE IF NOT EXISTS games (
    match_id INTEGER NOT NULL,
    game_no INTEGER NOT NULL,
    gamemode TEXT NOT NULL,
    score_a INTEGER NOT NULL,
    score_b INTEGER NOT NULL,
    PRIMARY KEY (match_id, game_no)
);
CREATE TABLE IF NOT EXISTS bets (
    match_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    settled INTEGER NOT NULL DEFAULT 0,
    payout INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (match_id, user_id)
);
"""
)
db.commit()


def q(sql, args=()):
    return db.execute(sql, args).fetchall()


def q1(sql, args=()):
    return db.execute(sql, args).fetchone()


def cfg_get(key):
    r = q1("SELECT value FROM config WHERE key=?", (key,))
    return r["value"] if r else None


def cfg_set(key, value):
    db.execute(
        "INSERT INTO config(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    db.commit()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def tn(team_id):
    r = q1("SELECT name FROM teams WHERE id=?", (team_id,))
    return r["name"] if r else "?"


def get_match(mid):
    return q1("SELECT * FROM matches WHERE id=?", (mid,))


def find_team(name):
    return q1("SELECT * FROM teams WHERE name=? COLLATE NOCASE", (name,))


def get_player(uid):
    return q1("SELECT * FROM players WHERE user_id=?", (uid,))


def team_of(uid):
    return q1("SELECT * FROM teams WHERE p1=? OR p2=?", (uid, uid))


def pending_total(uid):
    r = q1("SELECT COALESCE(SUM(amount),0) AS t FROM bets WHERE user_id=? AND settled=0", (uid,))
    return r["t"]


def game_wins(mid):
    a = b = 0
    for g in q("SELECT * FROM games WHERE match_id=?", (mid,)):
        if g["score_a"] > g["score_b"]:
            a += 1
        else:
            b += 1
    return a, b


def game_mode(mid, n):
    r = q1(
        "SELECT gamemode FROM draft WHERE match_id=? AND step=?",
        (mid, GAME_STEPS[n]),
    )
    return r["gamemode"] if r else None


def pool(mid):
    out = {}
    m = get_match(mid)
    for key in ("team_a", "team_b"):
        r = q1(
            "SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM bets "
            "WHERE match_id=? AND team_id=?",
            (mid, m[key]),
        )
        out[key] = (r["c"], r["s"])
    return out


def draft_text(mid, with_next=True):
    m = get_match(mid)
    rows = q("SELECT * FROM draft WHERE match_id=? ORDER BY step", (mid,))
    lines = []
    for r in rows:
        who = tn(r["team_id"]) if r["team_id"] else ""
        if r["action"] == "ban":
            lines.append(f"🚫 **{who}** banned **{r['gamemode']}**")
        elif r["action"] == "pick":
            lines.append(f"✅ **{who}** picked **{r['gamemode']}**")
        else:
            lines.append(f"⚔️ Decider: **{r['gamemode']}**")
    if with_next and len(rows) < len(DRAFT_ORDER):
        action, side = DRAFT_ORDER[len(rows)]
        if side:
            team = tn(m["team_a"] if side == "a" else m["team_b"])
            lines.append(f"➡️ *Next: {team} {action}s*")
        else:
            lines.append("➡️ *Next: decider*")
    return "\n".join(lines) if lines else "*Draft not started*"


def match_embed(mid):
    m = get_match(mid)
    ta, tb = get_match(mid)["team_a"], m["team_b"]
    ra = q1("SELECT * FROM teams WHERE id=?", (ta,))
    rb = q1("SELECT * FROM teams WHERE id=?", (tb,))
    colours = {
        "open": discord.Colour.green(),
        "locked": discord.Colour.orange(),
        "done": discord.Colour.blurple(),
        "cancelled": discord.Colour.red(),
    }
    e = discord.Embed(
        title=f"{m['round']}: {ra['name']} vs {rb['name']}",
        colour=colours.get(m["status"], discord.Colour.greyple()),
    )
    e.add_field(
        name=f"🅰️ {ra['name']}",
        value=f"<@{ra['p1']}> + <@{ra['p2']}>",
        inline=True,
    )
    e.add_field(
        name=f"🅱️ {rb['name']}",
        value=f"<@{rb['p1']}> + <@{rb['p2']}>",
        inline=True,
    )
    e.add_field(name="Status", value=STATUS_TEXT.get(m["status"], m["status"]), inline=False)
    p = pool(mid)
    e.add_field(
        name="CraftCoin predictions",
        value=(
            f"**{ra['name']}**: {p['team_a'][0]} bets · {p['team_a'][1]} coins\n"
            f"**{rb['name']}**: {p['team_b'][0]} bets · {p['team_b'][1]} coins"
        ),
        inline=False,
    )
    e.add_field(name="Draft", value=draft_text(mid)[:1024], inline=False)
    games = q("SELECT * FROM games WHERE match_id=? ORDER BY game_no", (mid,))
    if games:
        e.add_field(
            name="Scores so far",
            value="\n".join(
                f"Game {g['game_no']} ({g['gamemode']}): {g['score_a']} - {g['score_b']}"
                for g in games
            ),
            inline=False,
        )
    e.set_footer(
        text=f"Match #{mid} • Correct prediction pays {WIN_MULT}x your wager"
    )
    return e


def result_embed(mid, stats=None):
    m = get_match(mid)
    a, b = tn(m["team_a"]), tn(m["team_b"])
    ra = q1("SELECT * FROM teams WHERE id=?", (m["team_a"],))
    rb = q1("SELECT * FROM teams WHERE id=?", (m["team_b"],))
    wa, wb = game_wins(mid)
    winner = tn(m["winner"])
    e = discord.Embed(
        title=f"🏆 {m['round']}: {a} vs {b}",
        colour=discord.Colour.gold(),
        description=f"**Winner: {winner}**" + (
            f"  *(opponent forfeited)*" if m["forfeit_team"] else f"  ({wa} - {wb})"
        ),
    )
    e.add_field(name=a, value=f"<@{ra['p1']}> + <@{ra['p2']}>", inline=True)
    e.add_field(name=b, value=f"<@{rb['p1']}> + <@{rb['p2']}>", inline=True)
    e.add_field(name="Draft", value=draft_text(mid, with_next=False)[:1024], inline=False)
    games = q("SELECT * FROM games WHERE match_id=? ORDER BY game_no", (mid,))
    if games:
        lines = []
        for g in games:
            w = a if g["score_a"] > g["score_b"] else b
            lines.append(
                f"**Game {g['game_no']} · {g['gamemode']}**\n"
                f"{a} {g['score_a']} - {g['score_b']} {b} → {w}"
            )
        e.add_field(name="Game results", value="\n".join(lines)[:1024], inline=False)
    if stats is None:
        r = q1(
            "SELECT COUNT(*) AS n, COALESCE(SUM(payout>0),0) AS c, "
            "COALESCE(SUM(payout),0) AS p FROM bets WHERE match_id=?",
            (mid,),
        )
        stats = (r["n"], r["c"], r["p"])
    e.add_field(
        name="CraftCoins",
        value=f"{stats[0]} predictions · {stats[1]} correct · {stats[2]} coins paid out",
        inline=False,
    )
    e.set_footer(text=f"Match #{mid}")
    return e


def bet_view(mid):
    m = get_match(mid)
    v = discord.ui.View(timeout=None)
    v.add_item(BetButton(mid, "a", f"Bet on {tn(m['team_a'])}"[:80]))
    v.add_item(BetButton(mid, "b", f"Bet on {tn(m['team_b'])}"[:80]))
    return v


async def refresh_pred_message(mid):
    m = get_match(mid)
    if not m or not m["pred_msg"]:
        return
    try:
        ch = bot.get_channel(m["pred_channel"]) or await bot.fetch_channel(m["pred_channel"])
        msg = ch.get_partial_message(m["pred_msg"])
        await msg.edit(
            embed=match_embed(mid),
            view=bet_view(mid) if m["status"] == "open" else None,
        )
    except Exception:
        log.exception("Could not refresh prediction message for match %s", mid)


async def post_result(mid, stats=None):
    cid = cfg_get("results_channel")
    if not cid:
        return False
    try:
        ch = bot.get_channel(int(cid)) or await bot.fetch_channel(int(cid))
        await ch.send(embed=result_embed(mid, stats))
        return True
    except Exception:
        log.exception("Could not post result for match %s", mid)
        return False


def settle(mid, winner):
    bets = q("SELECT * FROM bets WHERE match_id=? AND settled=0", (mid,))
    paid = correct = 0
    for b in bets:
        payout = 0
        if b["team_id"] == winner:
            payout = b["amount"] + int(round(b["amount"] * WIN_MULT))
            correct += 1
            paid += payout
            db.execute(
                "UPDATE players SET balance=balance+? WHERE user_id=?",
                (payout, b["user_id"]),
            )
        db.execute(
            "UPDATE bets SET settled=1, payout=? WHERE match_id=? AND user_id=?",
            (payout, mid, b["user_id"]),
        )
    db.commit()
    return len(bets), correct, paid


async def finish_match(mid, winner, forfeit_team=None):
    """Atomically record a match result and settle every outstanding wager once."""
    db.execute("BEGIN IMMEDIATE")
    try:
        match_data = get_match(mid)
        if not match_data or match_data["status"] in ("done", "cancelled"):
            raise ValueError("This match has already been completed or cancelled.")

        bets = db.execute(
            "SELECT * FROM bets WHERE match_id=? AND settled=0", (mid,)
        ).fetchall()
        paid = correct = 0
        for bet in bets:
            payout = 0
            if bet["team_id"] == winner:
                payout = bet["amount"] + int(round(bet["amount"] * WIN_MULT))
                correct += 1
                paid += payout
                db.execute(
                    "UPDATE players SET balance=balance+? WHERE user_id=?",
                    (payout, bet["user_id"]),
                )
            db.execute(
                "UPDATE bets SET settled=1,payout=? WHERE match_id=? AND user_id=? AND settled=0",
                (payout, mid, bet["user_id"]),
            )
            balance = db.execute(
                "SELECT balance FROM players WHERE user_id=?", (bet["user_id"],)
            ).fetchone()
            if balance:
                db.execute(
                    """INSERT INTO cc_coin_transactions
                       (user_id,delta,balance_after,reason,match_id,created_at)
                       VALUES(?,?,?,?,?,datetime('now'))""",
                    (bet["user_id"], payout, balance["balance"],
                     "bet_won_payout" if payout else "bet_lost", mid),
                )
        db.execute(
            "UPDATE matches SET status='done',winner=?,forfeit_team=? WHERE id=?",
            (winner, forfeit_team, mid),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    stats = (len(bets), correct, paid)
    posted = await post_result(mid, stats)
    await refresh_pred_message(mid)
    return stats, posted


def place_bet(uid, mid, team_id, amount):
    m = get_match(mid)
    if not m or m["status"] != "open":
        return False, "Predictions for this match are closed."
    p = get_player(uid)
    if not p:
        return False, "You're not a registered participant, so you can't place wagers."
    old = q1("SELECT * FROM bets WHERE match_id=? AND user_id=?", (mid, uid))
    available = p["balance"] + (old["amount"] if old else 0)
    if amount <= 0:
        return False, "Wager must be at least 1 CraftCoin."
    if amount > available:
        return False, f"You only have **{available}** CraftCoins available."
    if old:
        db.execute("UPDATE players SET balance=balance+? WHERE user_id=?", (old["amount"], uid))
    db.execute("UPDATE players SET balance=balance-? WHERE user_id=?", (amount, uid))
    db.execute(
        "INSERT INTO bets(match_id,user_id,team_id,amount) VALUES(?,?,?,?) "
        "ON CONFLICT(match_id,user_id) DO UPDATE SET team_id=excluded.team_id, amount=excluded.amount",
        (mid, uid, team_id, amount),
    )
    db.commit()
    note = " (replaced your previous wager)" if old else ""
    return True, (
        f"Wagered **{amount}** CraftCoins on **{tn(team_id)}**{note}. "
        f"If correct you get your stake back plus **{int(round(amount * WIN_MULT))}**."
    )


# --------------------------------------------------------------------------
# Bot + persistent betting buttons
# --------------------------------------------------------------------------
class CraftBot(commands.Bot):
    async def setup_hook(self):
        self.add_dynamic_items(BetButton)
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = CraftBot(command_prefix="!unused!", intents=discord.Intents.default())


class BetModal(discord.ui.Modal):
    def __init__(self, match_id: int, side: str):
        m = get_match(match_id)
        self.match_id = match_id
        self.team_id = m["team_a"] if side == "a" else m["team_b"]
        super().__init__(title=f"Bet on {tn(self.team_id)}"[:45])
        self.amount = discord.ui.TextInput(
            label="Wager (CraftCoins)", placeholder="e.g. 25", max_length=7
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.amount.value.strip()
        if not raw.isdigit():
            return await interaction.response.send_message(
                "Please enter a whole number.", ephemeral=True
            )
        ok, msg = place_bet(interaction.user.id, self.match_id, self.team_id, int(raw))
        await interaction.response.send_message(("✅ " if ok else "⚠️ ") + msg, ephemeral=True)
        if ok:
            await refresh_pred_message(self.match_id)


class BetButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"bet:(?P<match>\d+):(?P<side>[ab])",
):
    def __init__(self, match_id: int, side: str, label: str = "Bet"):
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary if side == "a" else discord.ButtonStyle.danger,
                custom_id=f"bet:{match_id}:{side}",
            )
        )
        self.match_id = match_id
        self.side = side

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["match"]), match["side"])

    async def callback(self, interaction: discord.Interaction):
        m = get_match(self.match_id)
        if not m or m["status"] != "open":
            return await interaction.response.send_message(
                "Predictions for this match are closed.", ephemeral=True
            )
        p = get_player(interaction.user.id)
        if not p:
            return await interaction.response.send_message(
                "Only registered participants can place wagers.", ephemeral=True
            )
        await interaction.response.send_modal(BetModal(self.match_id, self.side))


# --------------------------------------------------------------------------
# Permissions / errors
# --------------------------------------------------------------------------
def is_staff(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if user.guild_permissions.administrator or user.guild_permissions.manage_guild:
        return True
    rid = cfg_get("staff_role")
    if rid and any(r.id == int(rid) for r in getattr(user, "roles", [])):
        return True
    raise app_commands.CheckFailure("staff only")


staff_only = app_commands.check(is_staff)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        msg = "🚫 Only tournament staff can use this command."
    else:
        log.exception("Command error", exc_info=error)
        msg = "Something went wrong running that command. Check the bot console."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


async def team_ac(interaction: discord.Interaction, current: str):
    rows = q("SELECT name FROM teams WHERE name LIKE ? LIMIT 25", (f"%{current}%",))
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]


GAMEMODE_CHOICES = [app_commands.Choice(name=g, value=g) for g in GAMEMODES]

# --------------------------------------------------------------------------
# /setup
# --------------------------------------------------------------------------
@bot.tree.command(name="setup", description="Staff: set results/predictions channels and staff role")
@app_commands.describe(
    results_channel="Where final match results are posted",
    predictions_channel="Where prediction (wager) posts go",
    staff_role="Optional role that can use staff commands",
)
@app_commands.guild_only()
@staff_only
async def setup_cmd(
    interaction: discord.Interaction,
    results_channel: discord.TextChannel,
    predictions_channel: discord.TextChannel,
    staff_role: Optional[discord.Role] = None,
):
    cfg_set("results_channel", results_channel.id)
    cfg_set("predictions_channel", predictions_channel.id)
    if staff_role:
        cfg_set("staff_role", staff_role.id)
    await interaction.response.send_message(
        f"✅ Results → {results_channel.mention}\n"
        f"✅ Predictions → {predictions_channel.mention}"
        + (f"\n✅ Staff role → {staff_role.mention}" if staff_role else ""),
        ephemeral=True,
    )


# --------------------------------------------------------------------------
# /participant
# --------------------------------------------------------------------------
participant = app_commands.Group(name="participant", description="Manage participants", guild_only=True)
bot.tree.add_command(participant)


@participant.command(name="add", description="Staff: register a participant with starting CraftCoins")
@staff_only
async def participant_add(interaction: discord.Interaction, user: discord.Member):
    if get_player(user.id):
        return await interaction.response.send_message(
            f"{user.mention} is already registered.", ephemeral=True
        )
    db.execute("INSERT INTO players(user_id,balance) VALUES(?,?)", (user.id, START_COINS))
    db.commit()
    await interaction.response.send_message(
        f"✅ Registered {user.mention} with **{START_COINS}** CraftCoins.", ephemeral=True
    )


@participant.command(name="remove", description="Staff: remove a participant (also deletes unsettled bets)")
@staff_only
async def participant_remove(interaction: discord.Interaction, user: discord.Member):
    if team_of(user.id):
        return await interaction.response.send_message(
            "They're on a team. Delete the team first.", ephemeral=True
        )
    db.execute("DELETE FROM bets WHERE user_id=? AND settled=0", (user.id,))
    db.execute("DELETE FROM players WHERE user_id=?", (user.id,))
    db.commit()
    await interaction.response.send_message(f"Removed {user.mention}.", ephemeral=True)


@participant.command(name="list", description="Staff: list registered participants")
@staff_only
async def participant_list(interaction: discord.Interaction):
    rows = q("SELECT * FROM players ORDER BY balance DESC")
    if not rows:
        return await interaction.response.send_message("No participants yet.", ephemeral=True)
    text = "\n".join(f"<@{r['user_id']}> — {r['balance']}" for r in rows)
    await interaction.response.send_message(
        f"**{len(rows)} participants**\n{text}"[:1990],
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# --------------------------------------------------------------------------
# /team
# --------------------------------------------------------------------------
team = app_commands.Group(name="team", description="Manage duos", guild_only=True)
bot.tree.add_command(team)


@team.command(name="create_legacy", description="Staff: create a legacy Discord-member duo")
@staff_only
async def team_create(
    interaction: discord.Interaction,
    name: str,
    player1: discord.Member,
    player2: discord.Member,
):
    if player1.id == player2.id:
        return await interaction.response.send_message("Pick two different players.", ephemeral=True)
    if find_team(name):
        return await interaction.response.send_message("A team with that name exists.", ephemeral=True)
    for p in (player1, player2):
        if team_of(p.id):
            return await interaction.response.send_message(
                f"{p.mention} is already on a team.", ephemeral=True
            )
    for p in (player1, player2):
        if not get_player(p.id):
            db.execute("INSERT INTO players(user_id,balance) VALUES(?,?)", (p.id, START_COINS))
    db.execute("INSERT INTO teams(name,p1,p2) VALUES(?,?,?)", (name, player1.id, player2.id))
    db.commit()
    await interaction.response.send_message(
        f"✅ Team **{name}**: {player1.mention} + {player2.mention}", ephemeral=True
    )


@team.command(name="delete_legacy", description="Staff: delete a legacy Discord-member duo (only if it has no matches)")
@staff_only
@app_commands.autocomplete(name=team_ac)
async def team_delete(interaction: discord.Interaction, name: str):
    t = find_team(name)
    if not t:
        return await interaction.response.send_message("Team not found.", ephemeral=True)
    if q1("SELECT 1 FROM matches WHERE team_a=? OR team_b=?", (t["id"], t["id"])):
        return await interaction.response.send_message(
            "That team has matches. Cancel/delete is not possible once matches exist.", ephemeral=True
        )
    db.execute("DELETE FROM teams WHERE id=?", (t["id"],))
    db.commit()
    await interaction.response.send_message(f"Deleted team **{t['name']}**.", ephemeral=True)


@team.command(name="list_legacy", description="List legacy Discord-member duos")
async def team_list(interaction: discord.Interaction):
    rows = q("SELECT * FROM teams ORDER BY name")
    if not rows:
        return await interaction.response.send_message("No teams yet.", ephemeral=True)
    text = "\n".join(f"**{r['name']}** — <@{r['p1']}> + <@{r['p2']}>" for r in rows)
    await interaction.response.send_message(
        text[:1990], allowed_mentions=discord.AllowedMentions.none()
    )


# --------------------------------------------------------------------------
# /match
# --------------------------------------------------------------------------
match = app_commands.Group(name="match", description="Manage matches", guild_only=True)
bot.tree.add_command(match)


@match.command(name="create", description="Staff: create a match and open predictions")
@app_commands.describe(team_a="Team A (bans first)", team_b="Team B", round_name="e.g. Round 1, Semi-final")
@app_commands.autocomplete(team_a=team_ac, team_b=team_ac)
@staff_only
async def match_create(
    interaction: discord.Interaction, team_a: str, team_b: str, round_name: str
):
    ta, tb = find_team(team_a), find_team(team_b)
    if not ta or not tb:
        return await interaction.response.send_message("One of those teams doesn't exist.", ephemeral=True)
    if ta["id"] == tb["id"]:
        return await interaction.response.send_message("A team can't play itself.", ephemeral=True)
    pc = cfg_get("predictions_channel")
    if not pc:
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)
    cur = db.execute(
        "INSERT INTO matches(round,team_a,team_b) VALUES(?,?,?)",
        (round_name, ta["id"], tb["id"]),
    )
    db.commit()
    mid = cur.lastrowid
    ch = bot.get_channel(int(pc)) or await bot.fetch_channel(int(pc))
    msg = await ch.send(embed=match_embed(mid), view=bet_view(mid))
    db.execute("UPDATE matches SET pred_channel=?, pred_msg=? WHERE id=?", (ch.id, msg.id, mid))
    db.commit()
    await interaction.response.send_message(
        f"✅ Match **#{mid}** created and posted in {ch.mention}.", ephemeral=True
    )


@match.command(name="draft", description="Staff: enter the next draft step (follows ban/pick order automatically)")
@app_commands.describe(match_id="Match number", gamemode="Gamemode for the next draft step")
@app_commands.choices(gamemode=GAMEMODE_CHOICES)
@staff_only
async def match_draft(interaction: discord.Interaction, match_id: int, gamemode: app_commands.Choice[str]):
    m = get_match(match_id)
    if not m or m["status"] in ("done", "cancelled"):
        return await interaction.response.send_message("Match not found or already finished.", ephemeral=True)
    used = {r["gamemode"] for r in q("SELECT gamemode FROM draft WHERE match_id=?", (match_id,))}
    step = len(used) + 1
    if step > len(DRAFT_ORDER):
        return await interaction.response.send_message("Draft is already complete.", ephemeral=True)
    if gamemode.value in used:
        return await interaction.response.send_message("That gamemode was already banned/picked.", ephemeral=True)
    action, side = DRAFT_ORDER[step - 1]
    team_id = None if side is None else (m["team_a"] if side == "a" else m["team_b"])
    db.execute(
        "INSERT INTO draft(match_id,step,action,team_id,gamemode) VALUES(?,?,?,?,?)",
        (match_id, step, action, team_id, gamemode.value),
    )
    db.commit()
    await refresh_pred_message(match_id)
    who = tn(team_id) if team_id else "Decider"
    await interaction.response.send_message(
        f"✅ Step {step}/{len(DRAFT_ORDER)} —**{who}** {action} → **{gamemode.value}**", ephemeral=True
    )


@match.command(name="undo_draft", description="Staff: remove the last draft step")
@staff_only
async def match_undo_draft(interaction: discord.Interaction, match_id: int):
    m = get_match(match_id)
    if not m:
        return await interaction.response.send_message("Match not found.", ephemeral=True)
    if q1("SELECT 1 FROM games WHERE match_id=?", (match_id,)):
        return await interaction.response.send_message("Scores already recorded. Undo games first.", ephemeral=True)
    last = q1("SELECT MAX(step) AS s FROM draft WHERE match_id=?", (match_id,))["s"]
    if not last:
        return await interaction.response.send_message("Nothing to undo.", ephemeral=True)
    db.execute("DELETE FROM draft WHERE match_id=? AND step=?", (match_id, last))
    db.commit()
    await refresh_pred_message(match_id)
    await interaction.response.send_message(f"Removed draft step {last}.", ephemeral=True)


@match.command(name="game", description="Staff: record the score of the next game (first to 3 game wins ends the match)")
@app_commands.describe(match_id="Match number", score_a="Team A's score", score_b="Team B's score")
@staff_only
async def match_game(
    interaction: discord.Interaction,
    match_id: int,
    score_a: app_commands.Range[int, 0, 1000],
    score_b: app_commands.Range[int, 0, 1000],
):
    m = get_match(match_id)
    if not m or m["status"] in ("done", "cancelled"):
        return await interaction.response.send_message("Match not found or already finished.", ephemeral=True)
    if score_a == score_b:
        return await interaction.response.send_message("Scores can't be tied. One team must win each game.", ephemeral=True)
    n = q1("SELECT COUNT(*) AS c FROM games WHERE match_id=?", (match_id,))["c"] + 1
    if n > len(GAME_STEPS):
        return await interaction.response.send_message("All games already recorded.", ephemeral=True)
    mode = game_mode(match_id, n)
    if not mode:
        return await interaction.response.send_message(
            f"Game {n}'s gamemode isn't set in the draft yet. Use /match draft first.", ephemeral=True
        )
    if m["status"] == "open":
        db.execute("UPDATE matches SET status='locked' WHERE id=?", (match_id,))
    db.execute(
        "INSERT INTO games(match_id,game_no,gamemode,score_a,score_b) VALUES(?,?,?,?,?)",
        (match_id, n, mode, score_a, score_b),
    )
    db.commit()
    wa, wb = game_wins(match_id)
    text = f"✅ Game {n} ({mode}): {tn(m['team_a'])} {score_a} - {score_b} {tn(m['team_b'])} · games {wa}-{wb}"
    if wa >= WINS_NEEDED or wb >= WINS_NEEDED:
        winner = m["team_a"] if wa >= WINS_NEEDED else m["team_b"]
        await interaction.response.defer(ephemeral=True)
        stats, posted = await finish_match(match_id, winner)
        text += (
            f"\n🏆 **{tn(winner)}** win the match! {stats[1]}/{stats[0]} predictions correct, "
            f"{stats[2]} coins paid out."
            + ("" if posted else "\n⚠️ Couldn't post to the results channel. Check /setup and bot permissions.")
        )
        return await interaction.followup.send(text, ephemeral=True)
    await refresh_pred_message(match_id)
    await interaction.response.send_message(text, ephemeral=True)


@match.command(name="undo_game", description="Staff: remove the last recorded game (unfinished matches only)")
@staff_only
async def match_undo_game(interaction: discord.Interaction, match_id: int):
    m = get_match(match_id)
    if not m or m["status"] in ("done", "cancelled"):
        return await interaction.response.send_message(
            "Match not found or already finished. Finished matches can't be edited.", ephemeral=True
        )
    last = q1("SELECT MAX(game_no) AS g FROM games WHERE match_id=?", (match_id,))["g"]
    if not last:
        return await interaction.response.send_message("No games recorded.", ephemeral=True)
    db.execute("DELETE FROM games WHERE match_id=? AND game_no=?", (match_id, last))
    db.commit()
    await refresh_pred_message(match_id)
    await interaction.response.send_message(f"Removed game {last}.", ephemeral=True)


@match.command(name="lock", description="Staff: close predictions for a match")
@staff_only
async def match_lock(interaction: discord.Interaction, match_id: int):
    m = get_match(match_id)
    if not m or m["status"] != "open":
        return await interaction.response.send_message("Match not found or not open.", ephemeral=True)
    db.execute("UPDATE matches SET status='locked' WHERE id=?", (match_id,))
    db.commit()
    await refresh_pred_message(match_id)
    await interaction.response.send_message("🔒 Predictions locked.", ephemeral=True)


@match.command(name="unlock", description="Staff: reopen predictions for a locked match")
@staff_only
async def match_unlock(interaction: discord.Interaction, match_id: int):
    m = get_match(match_id)
    if not m or m["status"] != "locked":
        return await interaction.response.send_message("Match not found or not locked.", ephemeral=True)
    db.execute("UPDATE matches SET status='open' WHERE id=?", (match_id,))
    db.commit()
    await refresh_pred_message(match_id)
    await interaction.response.send_message("🟢 Predictions reopened.", ephemeral=True)


@match.command(name="forfeit", description="Staff: a team forfeits (other team wins, bets settle)")
@app_commands.describe(forfeiting_team="The team that is forfeiting")
@app_commands.autocomplete(forfeiting_team=team_ac)
@staff_only
async def match_forfeit(interaction: discord.Interaction, match_id: int, forfeiting_team: str):
    m = get_match(match_id)
    t = find_team(forfeiting_team)
    if not m or m["status"] in ("done", "cancelled"):
        return await interaction.response.send_message("Match not found or already finished.", ephemeral=True)
    if not t or t["id"] not in (m["team_a"], m["team_b"]):
        return await interaction.response.send_message("That team isn't in this match.", ephemeral=True)
    winner = m["team_b"] if t["id"] == m["team_a"] else m["team_a"]
    await interaction.response.defer(ephemeral=True)
    stats, posted = await finish_match(match_id, winner, forfeit_team=t["id"])
    await interaction.followup.send(
        f"🏳️ **{t['name']}** forfeited. **{tn(winner)}** win. "
        f"{stats[1]}/{stats[0]} predictions correct, {stats[2]} coins paid out."
        + ("" if posted else "\n⚠️ Couldn't post to the results channel."),
        ephemeral=True,
    )


@match.command(name="cancel", description="Staff: cancel a match and refund all wagers")
@staff_only
async def match_cancel(interaction: discord.Interaction, match_id: int):
    m = get_match(match_id)
    if not m or m["status"] in ("done", "cancelled"):
        return await interaction.response.send_message("Match not found or already finished.", ephemeral=True)
    with db:
        for b in q("SELECT * FROM bets WHERE match_id=? AND settled=0", (match_id,)):
            db.execute("UPDATE players SET balance=balance+? WHERE user_id=?", (b["amount"], b["user_id"]))
            balance = get_player(b["user_id"])
            db.execute(
                "UPDATE bets SET settled=1, payout=amount WHERE match_id=? AND user_id=? AND settled=0",
                (match_id, b["user_id"]),
            )
            db.execute(
                """INSERT INTO cc_coin_transactions(user_id,delta,balance_after,reason,match_id,created_at)
                   VALUES(?,?,?,'bet_refund',?,datetime('now'))""",
                (b["user_id"], b["amount"], balance["balance"], match_id),
            )
        db.execute("UPDATE matches SET status='cancelled' WHERE id=?", (match_id,))
    await refresh_pred_message(match_id)
    await interaction.response.send_message("❌ Match cancelled and all wagers refunded.", ephemeral=True)


@match.command(name="repost_result", description="Staff: post a finished match's result to the results channel again")
@staff_only
async def match_repost(interaction: discord.Interaction, match_id: int):
    m = get_match(match_id)
    if not m or m["status"] != "done":
        return await interaction.response.send_message("Match isn't finished.", ephemeral=True)
    ok = await post_result(match_id)
    await interaction.response.send_message("Posted." if ok else "Couldn't post. Check /setup.", ephemeral=True)


@match.command(name="view", description="Show a match overview")
async def match_view(interaction: discord.Interaction, match_id: int):
    m = get_match(match_id)
    if not m:
        return await interaction.response.send_message("Match not found.", ephemeral=True)
    embed = result_embed(match_id) if m["status"] == "done" else match_embed(match_id)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="matches", description="List matches")
@app_commands.guild_only()
async def matches_cmd(interaction: discord.Interaction):
    rows = q("SELECT * FROM matches ORDER BY id DESC LIMIT 25")
    if not rows:
        return await interaction.response.send_message("No matches yet.", ephemeral=True)
    lines = []
    for r in rows:
        extra = f" → **{tn(r['winner'])}**" if r["winner"] else ""
        lines.append(
            f"`#{r['id']}` {r['round']}: {tn(r['team_a'])} vs {tn(r['team_b'])} "
            f"· {STATUS_TEXT[r['status']]}{extra}"
        )
    await interaction.response.send_message("\n".join(lines)[:1990])


# --------------------------------------------------------------------------
# CraftCoins
# --------------------------------------------------------------------------
@bot.tree.command(name="balance", description="Check a CraftCoin balance")
@app_commands.guild_only()
async def balance_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    user = user or interaction.user
    p = get_player(user.id)
    if not p:
        return await interaction.response.send_message(f"{user.display_name} isn't a registered participant.", ephemeral=True)
    pend = pending_total(user.id)
    await interaction.response.send_message(
        f"💰 **{user.display_name}**: **{p['balance']}** CraftCoins"
        + (f" (+{pend} currently wagered)" if pend else ""),
        ephemeral=True,
    )


@bot.tree.command(name="mybets", description="See your active and past wagers")
@app_commands.guild_only()
async def mybets_cmd(interaction: discord.Interaction):
    rows = q("SELECT * FROM bets WHERE user_id=? ORDER BY match_id DESC LIMIT 15", (interaction.user.id,))
    if not rows:
        return await interaction.response.send_message("You haven't placed any wagers.", ephemeral=True)
    lines = []
    for r in rows:
        m = get_match(r["match_id"])
        if not r["settled"]:
            res = "⏳ pending"
        elif r["payout"] > r["amount"]:
            res = f"✅ won +{r['payout'] - r['amount']}"
        elif m["status"] == "cancelled":
            res = "↩️ refunded"
        else:
            res = f"❌ lost {r['amount']}"
        lines.append(
            f"`#{r['match_id']}` {tn(m['team_a'])} vs {tn(m['team_b'])} — "
            f"{r['amount']} on **{tn(r['team_id'])}** · {res}"
        )
    await interaction.response.send_message("\n".join(lines)[:1990], ephemeral=True)


@bot.tree.command(name="leaderboard", description="CraftCoin leaderboard")
@app_commands.guild_only()
async def leaderboard_cmd(interaction: discord.Interaction):
    rows = q("SELECT * FROM players ORDER BY balance DESC LIMIT 10")
    if not rows:
        return await interaction.response.send_message("No participants yet.", ephemeral=True)
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{r['user_id']}> — **{r['balance']}**"
        for i, r in enumerate(rows)
    ]
    e = discord.Embed(title="💰 CraftCoin Leaderboard", description="\n".join(lines), colour=discord.Colour.gold())
    await interaction.response.send_message(embed=e, allowed_mentions=discord.AllowedMentions.none())


coins = app_commands.Group(name="coins", description="Adjust CraftCoins", guild_only=True)
bot.tree.add_command(coins)


async def _adjust(interaction, user, amount, mode):
    p = get_player(user.id)
    if not p:
        return await interaction.response.send_message("That user isn't registered.", ephemeral=True)
    if mode == "give":
        new = p["balance"] + amount
    elif mode == "take":
        new = max(0, p["balance"] - amount)
    else:
        new = amount
    delta = new - p["balance"]
    with db:
        db.execute("UPDATE players SET balance=? WHERE user_id=?", (new, user.id))
        import tournament_core
        tournament_core.record_coin(db, user.id, delta, f"staff_{mode}", interaction.user.id)
    await interaction.response.send_message(
        f"{user.mention} now has **{new}** CraftCoins.", ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@coins.command(name="give", description="Staff: give CraftCoins")
@staff_only
async def coins_give(interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 1000000]):
    await _adjust(interaction, user, amount, "give")


@coins.command(name="take", description="Staff: take CraftCoins")
@staff_only
async def coins_take(interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 1000000]):
    await _adjust(interaction, user, amount, "take")


@coins.command(name="set", description="Staff: set a balance")
@staff_only
async def coins_set(interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 0, 1000000]):
    await _adjust(interaction, user, amount, "set")


# --------------------------------------------------------------------------
# Giveaway (everyone who plays ALL assigned matches)
# --------------------------------------------------------------------------
giveaway = app_commands.Group(name="giveaway", description="Participant giveaway", guild_only=True)
bot.tree.add_command(giveaway)


def giveaway_status():
    eligible, pending, out = [], [], []
    for t in q("SELECT * FROM teams"):
        ms = q(
            "SELECT * FROM matches WHERE (team_a=? OR team_b=?) AND status!='cancelled'",
            (t["id"], t["id"]),
        )
        players = [t["p1"], t["p2"]]
        if not ms:
            pending += players
        elif any(x["forfeit_team"] == t["id"] for x in ms):
            out += players
        elif all(x["status"] == "done" for x in ms):
            eligible += players
        else:
            pending += players
    return eligible, pending, out


@giveaway.command(name="status", description="Staff: who is eligible for the participant giveaway")
@staff_only
async def giveaway_list(interaction: discord.Interaction):
    el, pe, out = giveaway_status()
    fmt = lambda ids: ", ".join(f"<@{i}>" for i in ids) or "none"
    await interaction.response.send_message(
        f"**✅ Eligible ({len(el)})**: {fmt(el)}\n"
        f"**⏳ Still have matches ({len(pe)})**: {fmt(pe)}\n"
        f"**🚫 Ineligible / forfeited ({len(out)})**: {fmt(out)}"[:1990],
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@giveaway.command(name="draw", description="Staff: randomly pick a winner from eligible participants")
@staff_only
async def giveaway_draw(interaction: discord.Interaction):
    el, _, _ = giveaway_status()
    if not el:
        return await interaction.response.send_message("Nobody is eligible yet.", ephemeral=True)
    w = random.choice(el)
    await interaction.response.send_message(
        f"🎉 Giveaway winner (1x Nitro Basic): <@{w}> — drawn from {len(el)} eligible participants!"
    )


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)


import public_betting
public_betting.install(__import__("sys").modules["__main__"])

# Additive Minecraft-username roster, fixed bracket, and tournament draft.
import tournament_discord
tournament_discord.install(__import__("sys").modules["__main__"])


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file.")
    bot.run(TOKEN)
