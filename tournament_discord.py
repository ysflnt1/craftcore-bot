"""Slash commands and dropdowns for the single Craftcore tournament."""
import io
import logging
import random
import sqlite3
from typing import Optional

import discord
from discord import app_commands
import tournament_core as tc

log = logging.getLogger("craftcore-tournament")
MATCHES = [app_commands.Choice(name=f"{k} · {label}", value=k) for k,_,label,*_ in tc.DEFS]
RESULT_TYPES = [
    app_commands.Choice(name="Normal",value="normal"),
    app_commands.Choice(name="Forfeit",value="forfeit"),
    app_commands.Choice(name="Disqualification",value="disqualification"),
    app_commands.Choice(name="Staff override",value="override"),
]

def staff(mod, interaction):
    try:
        mod.is_staff(interaction)
        return True
    except app_commands.CheckFailure:
        return False

def team_name(db, tid):
    return tc.name(db,tid)

def bracket_embed(db):
    rows=tc.bracket(db)
    e=discord.Embed(title="🏆 CRAFTCORE 2v2 TOURNAMENT",description="16 teams · Single elimination · 15 matches",colour=discord.Colour.blurple())
    for stage in ("Round 1","Quarterfinals","Semifinals","Grand Final"):
        values=[]
        for m in rows:
            if m["stage"]!=stage:continue
            state=f" → **{m['winner_name']}**" if m["winner_name"] else (" · ✅ completed" if m["status"]=="completed" else " · ⏳ waiting" if m["status"]=="waiting" else " · Ready")
            score=f" ({m['score_a']}–{m['score_b']})" if m["score_a"] is not None else ""
            values.append(f"`{m['match_key']}` **{m['team_a_name']}** vs **{m['team_b_name']}**{score}{state}")
        if values:e.add_field(name=stage,value="\n".join(values),inline=False)
    e.set_footer(text="Use /bracket result to record winners. All staff changes are audited.")
    return e

def draft_embed(db,key):
    s=tc.draft(db,key); m=s["match"]
    e=discord.Embed(title=f"🎮 Draft · {key}",description=f"**Team A:** {team_name(db,m['team_a_id'])}\n**Team B:** {team_name(db,m['team_b_id'])}",colour=discord.Colour.blurple())
    if s["selections"]:
        lines=[]
        for r in s["selections"]:
            who=team_name(db,r["team_id"]) if r["team_id"] else "Decider"
            lines.append(f"**{r['step']}.** {who} · {r['action']} · **{r['gamemode']}**")
        e.add_field(name="Selections",value="\n".join(lines),inline=False)
    if not s["complete"]:
        action,side=tc.ORDER[s["next_step"]-1]
        who=team_name(db,m["team_a_id"] if side=="a" else m["team_b_id"]) if side else "Both teams"
        e.add_field(name="Next turn",value=f"Step {s['next_step']}/9 · **{who} {action}**",inline=True)
    else:e.add_field(name="Draft",value="Complete · all nine selections saved",inline=True)
    e.add_field(name="Remaining gamemodes",value=", ".join(s["remaining"]) or "None",inline=False)
    e.set_footer(text="Cart PvP is HT ONLY.")
    return e

async def player_ac(mod,interaction,current):
    rows=mod.db.execute("SELECT username FROM cc_tournament_players WHERE username LIKE ? COLLATE NOCASE ORDER BY username LIMIT 25",(f"%{current}%",)).fetchall()
    return [app_commands.Choice(name=r["username"],value=r["username"]) for r in rows]

async def team_ac(mod,interaction,current):
    return [app_commands.Choice(name=t["name"][:100],value=str(t["id"])) for t in tc.teams(mod.db) if current.casefold() in t["name"].casefold()][:25]

async def winner_ac(mod,interaction,current):
    return [app_commands.Choice(name=t["name"][:100],value=str(t["id"])) for t in tc.teams(mod.db) if current.casefold() in t["name"].casefold()][:25]

def bind_autocomplete(function,mod):
    async def callback(interaction,current):
        return await function(mod,interaction,current)
    return callback

class DraftSelect(discord.ui.Select):
    def __init__(self,mod,key):
        self.mod,self.key=mod,key
        s=tc.draft(mod.db,key)
        super().__init__(placeholder="Choose this turn's gamemode…",min_values=1,max_values=1,
            options=[discord.SelectOption(label=x,value=x) for x in s["remaining"]],
            custom_id=f"craftcore:draft:{key}:{s['next_step']}")
    async def callback(self,interaction):
        if not staff(self.mod,interaction):
            return await interaction.response.send_message("Only tournament staff can make draft selections.",ephemeral=True)
        try:s=tc.draft_add(self.mod.db,self.key,self.values[0],interaction.user.id)
        except (ValueError,sqlite3.Error) as ex:
            return await interaction.response.send_message(str(ex),ephemeral=True)
        await interaction.response.edit_message(embed=draft_embed(self.mod.db,self.key),view=DraftView(self.mod,self.key) if not s["complete"] else None)

class DraftView(discord.ui.View):
    def __init__(self,mod,key):
        super().__init__(timeout=300)
        self.add_item(DraftSelect(mod,key))

class OwnerConfirm(discord.ui.View):
    def __init__(self,owner):
        super().__init__(timeout=120); self.owner=owner
    async def interaction_check(self,interaction):
        if interaction.user.id!=self.owner:
            await interaction.response.send_message("Only the staff member who opened this confirmation can use it.",ephemeral=True)
            return False
        return True

class ResultConfirm(OwnerConfirm):
    def __init__(self,mod,owner,key,winner,sa,sb,typ,ref,forfeit_team=None,disqualified_team=None):
        super().__init__(owner)
        self.mod,self.key,self.winner,self.sa,self.sb,self.typ,self.ref=mod,key,winner,sa,sb,typ,ref
        self.forfeit_team,self.disqualified_team=forfeit_team,disqualified_team
    @discord.ui.button(label="Confirm result change and refund dependent wagers",style=discord.ButtonStyle.danger)
    async def confirm(self,interaction,button):
        try:
            cleared=tc.set_result(self.mod.db,self.key,self.winner,self.sa,self.sb,self.typ,self.ref,
                                  interaction.user.id,True,self.forfeit_team,self.disqualified_team)
            await self.mod.sync_tournament_bracket()
        except ValueError as ex:return await interaction.response.edit_message(content=f"Result not saved: {ex}",embed=None,view=None)
        await interaction.response.edit_message(content=f"✅ Saved. Downstream match records were reset and affected wagers refunded: {', '.join(cleared) if cleared else 'none'}.",embed=None,view=None)
    @discord.ui.button(label="Cancel",style=discord.ButtonStyle.secondary)
    async def cancel(self,interaction,button):
        await interaction.response.edit_message(content="Cancelled; no bracket data changed.",embed=None,view=None)

class ResetConfirm(OwnerConfirm):
    def __init__(self,mod,owner):super().__init__(owner);self.mod=mod
    @discord.ui.button(label="Reset bracket progress",style=discord.ButtonStyle.danger)
    async def confirm(self,interaction,button):
        try:tc.reset_bracket(self.mod.db,interaction.user.id)
        except ValueError as ex:return await interaction.response.edit_message(content=str(ex),view=None)
        await interaction.response.edit_message(content="✅ Bracket and drafts reset. Teams, CraftCoin balances and legacy betting history are untouched.",view=None)
    @discord.ui.button(label="Cancel",style=discord.ButtonStyle.secondary)
    async def cancel(self,interaction,button):await interaction.response.edit_message(content="Reset cancelled.",view=None)

class ImportConfirm(OwnerConfirm):
    def __init__(self,mod,owner,payload):super().__init__(owner);self.mod,self.payload=mod,payload
    @discord.ui.button(label="Overwrite bracket progress",style=discord.ButtonStyle.danger)
    async def confirm(self,interaction,button):
        try:tc.import_bracket(self.mod.db,self.payload,interaction.user.id)
        except ValueError as ex:return await interaction.response.edit_message(content=f"Backup rejected: {ex}",view=None)
        await interaction.response.edit_message(content="✅ Backup imported. Team and economy data preserved.",view=None)
    @discord.ui.button(label="Cancel",style=discord.ButtonStyle.secondary)
    async def cancel(self,interaction,button):await interaction.response.edit_message(content="Import cancelled.",view=None)

def install(mod):
    tc.ensure_schema(mod.db)
    try:
        count=tc.seed_initial_teams(mod.db)
        made=tc.initialise_bracket(mod.db)
        log.info("Tournament initialised: %d initial teams added; bracket created=%s",count,made)
    except ValueError:
        log.exception("Tournament roster/bracket conflict; staff must resolve it. Legacy bot remains available.")
    bot=mod.bot

    # Add Minecraft roster commands to the existing /team group. Legacy
    # Discord-member team commands are retained with *_legacy names.
    group=bot.tree.get_command("team")
    if not isinstance(group,app_commands.Group):
        group=app_commands.Group(name="team",description="Minecraft-username tournament teams",guild_only=True)
        bot.tree.add_command(group)

    @group.command(name="create",description="Staff: create a duo using Minecraft usernames")
    @app_commands.describe(name="Team name",player1="Minecraft username",player2="Minecraft username",seed="Optional seed 1–16")
    @mod.staff_only
    async def team_create(interaction,name:str,player1:str,player2:str,seed:Optional[int]=None):
        if tc.locked(mod.db):return await interaction.response.send_message("Registrations are locked.",ephemeral=True)
        try:tid=tc.create_team(mod.db,name,player1,player2,seed,interaction.user.id)
        except ValueError as ex:return await interaction.response.send_message(str(ex),ephemeral=True)
        await interaction.response.send_message(f"✅ Registered **{name}** — `{player1}` + `{player2}` (ID {tid}).",ephemeral=True)

    @group.command(name="edit",description="Staff: edit a Minecraft roster team")
    @app_commands.autocomplete(team_id=bind_autocomplete(team_ac,mod))
    @mod.staff_only
    async def team_edit(interaction,team_id:str,name:str,player1:str,player2:str,seed:Optional[int]=None):
        if tc.locked(mod.db):return await interaction.response.send_message("Registrations are locked.",ephemeral=True)
        try:tc.edit_team(mod.db,int(team_id),name,player1,player2,seed,interaction.user.id)
        except (ValueError,TypeError) as ex:return await interaction.response.send_message(str(ex),ephemeral=True)
        await interaction.response.send_message(f"✅ Updated **{name}**. Fixed bracket positions are unchanged.",ephemeral=True)

    @group.command(name="delete",description="Staff: delete a team not referenced by the bracket")
    @app_commands.autocomplete(team_id=bind_autocomplete(team_ac,mod))
    @mod.staff_only
    async def team_delete(interaction,team_id:str):
        if tc.locked(mod.db):return await interaction.response.send_message("Registrations are locked.",ephemeral=True)
        try:t=tc.team(mod.db,int(team_id));tc.delete_team(mod.db,int(team_id),interaction.user.id)
        except (ValueError,TypeError) as ex:return await interaction.response.send_message(str(ex),ephemeral=True)
        await interaction.response.send_message(f"✅ Deleted **{t['name']}**.",ephemeral=True)

    @group.command(name="list",description="View all Minecraft tournament teams")
    async def team_list(interaction):
        rows=tc.teams(mod.db)
        if not rows:return await interaction.response.send_message("No teams registered.",ephemeral=True)
        lines=[f"`{t['seed'] or '—'}` **{t['name']}** — `{t['player1']}` + `{t['player2']}`" for t in rows]
        e=discord.Embed(title="CRAFTCORE · TEAM DIRECTORY",description="\n".join(lines)[:4000],colour=discord.Colour.blurple())
        e.set_footer(text=f"{len(rows)}/16 teams")
        await interaction.response.send_message(embed=e,ephemeral=True)

    @group.command(name="search",description="Search a tournament team or Minecraft username")
    async def team_search(interaction,query:str):
        rows=[t for t in tc.teams(mod.db) if query.casefold() in t["name"].casefold() or query.casefold() in t["player1"].casefold() or query.casefold() in t["player2"].casefold()]
        if not rows:return await interaction.response.send_message("No matching team or player.",ephemeral=True)
        await interaction.response.send_message("\n".join(f"**{t['name']}** — `{t['player1']}` + `{t['player2']}` · seed {t['seed'] or '—'}" for t in rows[:20]),ephemeral=True)

    @group.command(name="import",description="Staff: import CSV or JSON tournament teams")
    @mod.staff_only
    async def team_import(interaction,file:discord.Attachment):
        if tc.locked(mod.db):return await interaction.response.send_message("Registrations are locked.",ephemeral=True)
        try:count=tc.import_teams(mod.db,tc.parse_teams(file.filename,await file.read()),interaction.user.id)
        except (ValueError,UnicodeDecodeError,OSError) as ex:return await interaction.response.send_message(f"Import failed: {ex}",ephemeral=True)
        await interaction.response.send_message(f"✅ Import complete. Added {count} new teams; identical existing teams were skipped.",ephemeral=True)

    @group.command(name="export",description="Staff: export tournament teams as CSV or JSON")
    @app_commands.choices(format=[app_commands.Choice(name="CSV",value="csv"),app_commands.Choice(name="JSON",value="json")])
    @mod.staff_only
    async def team_export(interaction,format:app_commands.Choice[str]):
        raw=tc.export_teams(mod.db,format.value).encode("utf-8")
        await interaction.response.send_message(file=discord.File(io.BytesIO(raw),filename=f"craftcore-teams.{format.value}"),ephemeral=True)

    @group.command(name="shuffle_seeds",description="Staff: randomise seed numbers without changing fixed match pairings")
    @mod.staff_only
    async def team_shuffle(interaction):
        rows=tc.teams(mod.db)
        if len(rows)!=16:return await interaction.response.send_message("Register all 16 teams first.",ephemeral=True)
        ids=[t["id"] for t in rows];random.shuffle(ids)
        with mod.db:
            mod.db.execute("UPDATE cc_tournament_teams SET seed=NULL")
            for seed,tid in enumerate(ids,1):mod.db.execute("UPDATE cc_tournament_teams SET seed=? WHERE id=?",(seed,tid))
            tc.audit(mod.db,"team_seeds_randomised",{"team_ids":ids},interaction.user.id)
        await interaction.response.send_message("✅ Seeds randomised. Initial bracket pairings were not changed.",ephemeral=True)

    tg=app_commands.Group(name="tournament",description="Craftcore 2v2 tournament",guild_only=True);bot.tree.add_command(tg)
    @tg.command(name="status",description="View tournament roster and setup status")
    async def tournament_status(interaction):
        count=len(tc.teams(mod.db)); n=mod.db.execute("SELECT COUNT(*) FROM cc_bracket_matches").fetchone()[0]
        e=discord.Embed(title=tc.NAME,description="Single elimination · 16 teams · 15 matches",colour=discord.Colour.blurple())
        e.add_field(name="Teams",value=f"{count}/16",inline=True);e.add_field(name="Registration",value="Locked" if tc.locked(mod.db) else "Open",inline=True);e.add_field(name="Bracket",value=f"{n}/15 matches",inline=True)
        e.add_field(name="Draft gamemodes",value=", ".join(tc.MODES),inline=False);e.set_footer(text="Cart PvP is HT ONLY.")
        await interaction.response.send_message(embed=e,ephemeral=True)
    @tg.command(name="initialise",description="Staff: safely seed the 16 teams and exact bracket")
    @mod.staff_only
    async def tournament_initialise(interaction):
        try:a=tc.seed_initial_teams(mod.db,interaction.user.id);b=tc.initialise_bracket(mod.db,interaction.user.id)
        except ValueError as ex:return await interaction.response.send_message(f"Stopped safely: {ex}",ephemeral=True)
        await interaction.response.send_message(f"✅ Added {a} initial teams. Bracket {'created' if b else 'already exists'} (15 matches).",ephemeral=True)
    @tg.command(name="lock_registrations",description="Staff: lock team registrations and edits")
    @mod.staff_only
    async def lock_reg(interaction):
        tc.set_locked(mod.db,True,interaction.user.id);await interaction.response.send_message("🔒 Registrations locked.",ephemeral=True)
    @tg.command(name="unlock_registrations",description="Staff: reopen team registrations and edits")
    @mod.staff_only
    async def unlock_reg(interaction):
        tc.set_locked(mod.db,False,interaction.user.id);await interaction.response.send_message("🟢 Registrations reopened.",ephemeral=True)

    bg=app_commands.Group(name="bracket",description="Fixed Craftcore tournament bracket",guild_only=True);bot.tree.add_command(bg)
    @bg.command(name="view",description="View all 15 matches in the 16-team bracket")
    async def bracket_view(interaction):await interaction.response.send_message(embed=bracket_embed(mod.db),ephemeral=True)
    @bg.command(name="result",description="Staff: choose a match winner and optionally record scores")
    @app_commands.choices(match_key=MATCHES,result_type=RESULT_TYPES)
    @app_commands.autocomplete(winner=bind_autocomplete(winner_ac,mod))
    @mod.staff_only
    async def bracket_result(interaction,match_key:app_commands.Choice[str],winner:str,result_type:app_commands.Choice[str],score_a:Optional[int]=None,score_b:Optional[int]=None,referee:Optional[str]=None):
        try:
            tid=int(winner);cleared=tc.set_result(mod.db,match_key.value,tid,score_a,score_b,result_type.value,referee,interaction.user.id)
        except tc.DownstreamResultsExist as ex:
            detail=", ".join(f"{k} ({next(x[2] for x in tc.DEFS if x[0]==k)})" for k in ex.match_keys)
            return await interaction.response.send_message(f"⚠️ This changes completed downstream results: **{detail}**. Confirm to clear them.",view=ResultConfirm(mod,interaction.user.id,match_key.value,tid,score_a,score_b,result_type.value,referee),ephemeral=True)
        except (ValueError,TypeError) as ex:return await interaction.response.send_message(str(ex),ephemeral=True)
        await interaction.response.send_message(f"✅ **{team_name(mod.db,tid)}** recorded as winner of **{match_key.value}**. Invalidated: {', '.join(cleared) if cleared else 'none'}.",ephemeral=True)
    @bg.command(name="undo",description="Staff: clear a match winner (confirms dependent result invalidation)")
    @app_commands.choices(match_key=MATCHES)
    @mod.staff_only
    async def bracket_undo(interaction,match_key:app_commands.Choice[str]):
        try:cleared=tc.set_result(mod.db,match_key.value,None,actor=interaction.user.id)
        except tc.DownstreamResultsExist as ex:
            return await interaction.response.send_message(f"Undoing **{match_key.value}** clears completed downstream results: **{', '.join(ex.match_keys)}**. Confirm?",view=ResultConfirm(mod,interaction.user.id,match_key.value,None,None,None,"normal",None),ephemeral=True)
        except ValueError as ex:return await interaction.response.send_message(str(ex),ephemeral=True)
        await interaction.response.send_message(f"✅ Cleared {match_key.value}. Downstream cleared: {', '.join(cleared) if cleared else 'none'}.",ephemeral=True)
    @bg.command(name="history",description="Staff: view match result change history")
    @app_commands.choices(match_key=MATCHES)
    @mod.staff_only
    async def bracket_history(interaction,match_key:app_commands.Choice[str]):
        rows=tc.history(mod.db,match_key.value)
        if not rows:return await interaction.response.send_message("No match history recorded yet.",ephemeral=True)
        await interaction.response.send_message(f"**{match_key.value} history**\n"+"\n".join(f"`{r['created_at']}` · {r['action']} · staff {r['actor_id'] or 'system'}" for r in rows),ephemeral=True)
    @bg.command(name="reset",description="Staff: reset bracket results and drafts, preserving teams and coins")
    @mod.staff_only
    async def bracket_reset(interaction):
        await interaction.response.send_message("This clears all 15 match results and drafts. It preserves teams, wallets and existing legacy bets. Continue?",view=ResetConfirm(mod,interaction.user.id),ephemeral=True)
    @bg.command(name="export",description="Staff: export bracket progress to JSON")
    @mod.staff_only
    async def bracket_export(interaction):
        await interaction.response.send_message(file=discord.File(io.BytesIO(tc.export_bracket(mod.db).encode()),filename="craftcore-bracket-backup.json"),ephemeral=True)
    @bg.command(name="import",description="Staff: validate and import a bracket JSON backup")
    @mod.staff_only
    async def bracket_import(interaction,file:discord.Attachment):
        try:
            payload=await file.read()
            temp=sqlite3.connect(":memory:");mod.db.backup(temp);temp.row_factory=sqlite3.Row;tc.import_bracket(temp,payload,interaction.user.id);temp.close()
        except (ValueError,sqlite3.Error,OSError) as ex:return await interaction.response.send_message(f"Backup rejected: {ex}",ephemeral=True)
        await interaction.response.send_message("Backup validated. This overwrites current match progress. Continue?",view=ImportConfirm(mod,interaction.user.id,payload),ephemeral=True)

    dg=app_commands.Group(name="draft",description="Nine-step match ban-and-pick",guild_only=True);bot.tree.add_command(dg)
    @dg.command(name="start",description="Staff: start or continue an interactive draft")
    @app_commands.choices(match_key=MATCHES)
    @mod.staff_only
    async def draft_start(interaction,match_key:app_commands.Choice[str]):
        try:s=tc.draft(mod.db,match_key.value)
        except ValueError as ex:return await interaction.response.send_message(str(ex),ephemeral=True)
        if s["match"]["team_a_id"] is None or s["match"]["team_b_id"] is None:return await interaction.response.send_message("Both teams must be available first.",ephemeral=True)
        await interaction.response.send_message(embed=draft_embed(mod.db,match_key.value),view=DraftView(mod,match_key.value) if not s["complete"] else None,ephemeral=True)
    @dg.command(name="view",description="View saved draft progress and reopen its dropdown (staff)")
    @app_commands.choices(match_key=MATCHES)
    async def draft_view(interaction,match_key:app_commands.Choice[str]):
        try:s=tc.draft(mod.db,match_key.value)
        except ValueError as ex:return await interaction.response.send_message(str(ex),ephemeral=True)
        if not staff(mod,interaction):return await interaction.response.send_message(embed=draft_embed(mod.db,match_key.value),ephemeral=True)
        await interaction.response.send_message(embed=draft_embed(mod.db,match_key.value),view=DraftView(mod,match_key.value) if not s["complete"] else None,ephemeral=True)
    @dg.command(name="undo",description="Staff: undo the latest selection in a match draft")
    @app_commands.choices(match_key=MATCHES)
    @mod.staff_only
    async def draft_undo(interaction,match_key:app_commands.Choice[str]):
        try:step=tc.draft_undo(mod.db,match_key.value,interaction.user.id)
        except ValueError as ex:return await interaction.response.send_message(str(ex),ephemeral=True)
        await interaction.response.send_message(f"✅ Removed draft step {step}. Use /draft view to reopen the dropdown.",ephemeral=True)

    # Link Discord-specific wallets to a Minecraft participant explicitly.
    pg=bot.tree.get_command("participant")
    if isinstance(pg,app_commands.Group):
        @pg.command(name="link",description="Staff: link a Minecraft player to a Discord wallet")
        @app_commands.autocomplete(minecraft_username=bind_autocomplete(player_ac,mod))
        @mod.staff_only
        async def participant_link(interaction,minecraft_username:str,member:discord.Member):
            row=mod.db.execute("SELECT username FROM cc_tournament_players WHERE username=? COLLATE NOCASE",(minecraft_username,)).fetchone()
            if not row:return await interaction.response.send_message("That Minecraft username is not registered.",ephemeral=True)
            uname=row["username"]
            by_name=mod.db.execute("SELECT discord_id FROM cc_discord_links WHERE username=? COLLATE NOCASE",(uname,)).fetchone()
            by_user=mod.db.execute("SELECT username FROM cc_discord_links WHERE discord_id=?",(member.id,)).fetchone()
            if by_name and by_name["discord_id"]!=member.id:return await interaction.response.send_message(f"`{uname}` is already linked to a different Discord account.",ephemeral=True)
            if by_user and by_user["username"].casefold()!=uname.casefold():return await interaction.response.send_message(f"{member.mention} is already linked to `{by_user['username']}`.",ephemeral=True)
            with mod.db:
                existed=mod.get_player(member.id)
                mod.db.execute("INSERT OR IGNORE INTO players(user_id,balance) VALUES(?,?)",(member.id,mod.START_COINS))
                if not existed:
                    tc.record_coin(mod.db,member.id,mod.START_COINS,"starting_balance",interaction.user.id)
                mod.db.execute("INSERT INTO cc_discord_links(username,discord_id,linked_by,linked_at) VALUES(?,?,?,?) ON CONFLICT(username) DO UPDATE SET discord_id=excluded.discord_id,linked_by=excluded.linked_by,linked_at=excluded.linked_at",(uname,member.id,interaction.user.id,tc.now()))
                tc.audit(mod.db,"discord_wallet_linked",{"username":uname,"discord_id":member.id},interaction.user.id)
            wallet=mod.get_player(member.id)
            created=await mod.sync_tournament_bracket()
            await interaction.response.send_message(
                f"✅ Linked `{uname}` to {member.mention}. Existing wallet balance preserved: **{wallet['balance']}**. "
                f"New bracket betting matches created: **{created}**.",
                ephemeral=True,
            )
        @pg.command(name="unlink",description="Staff: unlink a Minecraft player without deleting their wallet")
        @app_commands.autocomplete(minecraft_username=bind_autocomplete(player_ac,mod))
        @mod.staff_only
        async def participant_unlink(interaction,minecraft_username:str):
            row=mod.db.execute("SELECT discord_id FROM cc_discord_links WHERE username=? COLLATE NOCASE",(minecraft_username,)).fetchone()
            if not row:return await interaction.response.send_message("That participant is not linked.",ephemeral=True)
            with mod.db:
                mod.db.execute("DELETE FROM cc_discord_links WHERE username=? COLLATE NOCASE",(minecraft_username,))
                tc.audit(mod.db,"discord_wallet_unlinked",{"username":minecraft_username,"discord_id":row["discord_id"]},interaction.user.id)
            await interaction.response.send_message(f"✅ Unlinked `{minecraft_username}`. Wallet and bet history preserved.",ephemeral=True)
    # CraftCoin history and wallet freeze controls complement the legacy /coins group.
    cg=bot.tree.get_command("coins")
    if isinstance(cg,app_commands.Group):
        @cg.command(name="history",description="View your CraftCoin transaction history")
        async def coins_history(interaction,member:Optional[discord.Member]=None):
            target=member or interaction.user
            if target.id!=interaction.user.id and not staff(mod,interaction):
                return await interaction.response.send_message("Only staff can view another player's wallet history.",ephemeral=True)
            if not mod.db.execute("SELECT 1 FROM cc_discord_links WHERE discord_id=?",(target.id,)).fetchone():
                return await interaction.response.send_message("That Discord account is not linked to a tournament participant.",ephemeral=True)
            rows=tc.coin_history(mod.db,target.id)
            if not rows:return await interaction.response.send_message("No CraftCoin transactions recorded yet.",ephemeral=True)
            lines=[f"{r['created_at']} · {r['delta']:+} · {r['reason']} · balance {r['balance_after']}" for r in rows]
            await interaction.response.send_message(f"CraftCoin history · {target.display_name}\n"+"\n".join(lines)[:1800],ephemeral=True)

        @cg.command(name="freeze",description="Staff: freeze a participant wallet and prevent new wagers")
        @mod.staff_only
        async def coins_freeze(interaction,member:discord.Member):
            try:tc.set_wallet_frozen(mod.db,member.id,True,interaction.user.id)
            except ValueError as ex:return await interaction.response.send_message(str(ex),ephemeral=True)
            await interaction.response.send_message(f"Froze {member.mention}'s wallet. Existing balance and bets were not changed.",ephemeral=True)

        @cg.command(name="unfreeze",description="Staff: unfreeze a participant wallet")
        @mod.staff_only
        async def coins_unfreeze(interaction,member:discord.Member):
            try:tc.set_wallet_frozen(mod.db,member.id,False,interaction.user.id)
            except ValueError as ex:return await interaction.response.send_message(str(ex),ephemeral=True)
            await interaction.response.send_message(f"Unfroze {member.mention}'s wallet.",ephemeral=True)


    async def sync_legacy_bracket():
        """Mirror linked Minecraft duos into the legacy betting/game tables."""
        linked = {}
        for t in tc.teams(mod.db):
            a = mod.db.execute("SELECT discord_id FROM cc_discord_links WHERE username=? COLLATE NOCASE",(t["player1"],)).fetchone()
            b = mod.db.execute("SELECT discord_id FROM cc_discord_links WHERE username=? COLLATE NOCASE",(t["player2"],)).fetchone()
            if not a or not b:
                continue
            a_id,b_id=a["discord_id"],b["discord_id"]
            old=mod.find_team(t["name"])
            if old:
                if {old["p1"],old["p2"]}!={a_id,b_id}:
                    log.warning("Legacy team name conflict for %s; not mirroring this Minecraft team",t["name"])
                    continue
                legacy_id=old["id"]
            else:
                if mod.team_of(a_id) or mod.team_of(b_id):
                    log.warning("A linked Discord account already belongs to another legacy team; skipping %s",t["name"])
                    continue
                try:
                    with mod.db:
                        cur=mod.db.execute("INSERT INTO teams(name,p1,p2) VALUES(?,?,?)",(t["name"],a_id,b_id))
                        legacy_id=cur.lastrowid
                except sqlite3.IntegrityError:
                    log.exception("Could not mirror tournament team %s",t["name"])
                    continue
            with mod.db:
                mod.db.execute("UPDATE cc_tournament_teams SET legacy_team_id=? WHERE id=?",(legacy_id,t["id"]))
            linked[t["id"]]=legacy_id

        channel_id=mod.cfg_get("predictions_channel")
        created=0
        for m in tc.bracket(mod.db):
            a=linked.get(m["team_a_id"]) if m["team_a_id"] else None
            b=linked.get(m["team_b_id"]) if m["team_b_id"] else None
            if not a or not b:
                continue
            if a==b:
                log.error("Tournament match %s maps both teams to the same legacy team",m["match_key"])
                continue
            legacy_id=m.get("legacy_match_id")
            previous=mod.get_match(legacy_id) if legacy_id else None
            if legacy_id and (not previous or previous["status"]=="cancelled"):
                with mod.db:
                    mod.db.execute("UPDATE cc_bracket_matches SET legacy_match_id=NULL WHERE match_key=?",(m["match_key"],))
                legacy_id=None
            if not legacy_id:
                if m["status"]=="completed":
                    continue
                try:
                    with mod.db:
                        cur=mod.db.execute("INSERT INTO matches(round,team_a,team_b) VALUES(?,?,?)",
                                           (f"{m['stage']} · {m['label']}",a,b))
                        legacy_id=cur.lastrowid
                        mod.db.execute("UPDATE cc_bracket_matches SET legacy_match_id=? WHERE match_key=?",
                                       (legacy_id,m["match_key"]))
                    created+=1
                except sqlite3.IntegrityError:
                    log.exception("Could not create legacy betting match for %s",m["match_key"])
                    continue
            legacy=mod.get_match(legacy_id)
            if not legacy:
                continue
            if legacy["status"]=="done" and legacy["winner"] is not None and m["winner_team_id"] is None:
                try:
                    wa,wb=mod.game_wins(legacy_id)
                    forfeiter=legacy["forfeit_team"]
                    has_score=forfeiter is None and wa!=wb
                    tc.sync_legacy_result(mod.db,legacy_id,legacy["winner"],forfeiter,
                                          wa if has_score else None,wb if has_score else None)
                    m=tc.get_match(mod.db,m["match_key"])
                except (ValueError,tc.DownstreamResultsExist):
                    log.exception("Could not recover legacy result for tournament match %s",m["match_key"])
            elif legacy["status"] in ("open","locked") and not mod.q1(
                "SELECT 1 FROM bets WHERE match_id=? LIMIT 1",(legacy_id,)
            ) and not mod.q1("SELECT 1 FROM games WHERE match_id=? LIMIT 1",(legacy_id,)):
                if (legacy["team_a"],legacy["team_b"])!=(a,b):
                    with mod.db:
                        mod.db.execute("UPDATE matches SET team_a=?,team_b=? WHERE id=?",(a,b,legacy_id))
            if channel_id and not legacy["pred_msg"] and legacy["status"]=="open":
                try:
                    channel=mod.bot.get_channel(int(channel_id)) or await mod.bot.fetch_channel(int(channel_id))
                    message=await channel.send(embed=mod.match_embed(legacy_id),view=mod.bet_view(legacy_id))
                    with mod.db:
                        mod.db.execute("UPDATE matches SET pred_channel=?,pred_msg=? WHERE id=?",
                                       (channel.id,message.id,legacy_id))
                except Exception:
                    log.exception("Could not post prediction panel for bracket match %s",m["match_key"])

        # Keep the legacy game-mode draft in sync; /match game reads that table.
        for m in tc.bracket(mod.db):
            if not m.get("legacy_match_id"):
                continue
            legacy=mod.get_match(m["legacy_match_id"])
            if legacy and legacy["status"] in ("open","locked") and not mod.q1("SELECT 1 FROM games WHERE match_id=? LIMIT 1",(m["legacy_match_id"],)):
                with mod.db:
                    mod.db.execute("DELETE FROM draft WHERE match_id=?",(m["legacy_match_id"],))
                    for selection in tc.draft(mod.db,m["match_key"])["selections"]:
                        legacy_team=None
                        if selection["team_id"] is not None:
                            tr=tc.team(mod.db,selection["team_id"])
                            legacy_team=tr.get("legacy_team_id") if tr else None
                        mod.db.execute(
                            "INSERT INTO draft(match_id,step,action,team_id,gamemode) VALUES(?,?,?,?,?)",
                            (m["legacy_match_id"],selection["step"],selection["action"],legacy_team,selection["gamemode"])
                        )
        return created

    mod.sync_tournament_bracket = sync_legacy_bracket

    log.info("Tournament teams, bracket, draft, wallet links and coin controls installed.")
