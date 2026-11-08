"""Persistent, database-backed Discord information panels for the Craftcore tournament."""
import logging
import discord
from discord import app_commands
import tournament_core as tc
import tournament_discord as td

log=logging.getLogger("craftcore-panels")
PANEL_KEYS=(
    ("home","TOURNAMENT HOME"),
    ("information","TOURNAMENT INFORMATION"),
    ("prizes","PRIZES"),
    ("gamemodes","GAMEMODES"),
    ("teams","TEAM DIRECTORY"),
    ("bracket","TOURNAMENT BRACKET"),
    ("matches","MATCH INFORMATION"),
    ("draft","DRAFT"),
    ("coins","CRAFTCOINS"),
    ("leaderboard","LEADERBOARD"),
    ("giveaway","GIVEAWAY"),
    ("hall","HALL OF FAME"),
    ("players","PLAYER STATISTICS"),
)

def _setting(db,key,default):
    row=db.execute("SELECT value FROM cc_tournament_settings WHERE key=?",(key,)).fetchone()
    return row["value"] if row else default

def _coin_metrics(mod):
    linked=mod.db.execute("""SELECT p.user_id,p.balance,l.username FROM players p
                             JOIN cc_discord_links l ON l.discord_id=p.user_id""").fetchall()
    output=[]
    for row in linked:
        bets=mod.db.execute("""SELECT b.amount,b.payout,m.status FROM bets b
                               JOIN matches m ON m.id=b.match_id
                               WHERE b.user_id=? AND b.settled=1""",(row["user_id"],)).fetchall()
        completed=[b for b in bets if b["status"]=="done"]
        wins=sum(1 for b in completed if b["payout"]>b["amount"])
        profit=sum(b["payout"]-b["amount"] for b in completed)
        output.append({"username":row["username"],"balance":row["balance"],"profit":profit,
                       "bets":len(completed),"wins":wins,
                       "accuracy":round(100*wins/len(completed),1) if completed else 0.0})
    return output

def panel_embed(mod,key,user_id=None):
    title=dict(PANEL_KEYS).get(key)
    if not title: raise ValueError("Unknown panel.")
    colour=discord.Colour.from_rgb(37,92,145)
    e=discord.Embed(title=f"CRAFTCORE 2v2 · {title}",colour=colour)
    teams=tc.teams(mod.db)
    if key=="home":
        latest=mod.db.execute("SELECT title,content FROM cc_announcements ORDER BY id DESC LIMIT 3").fetchall()
        e.description=_setting(mod.db,"tournament_description","One Minecraft PvP 2v2 single-elimination tournament.")
        e.add_field(name="Format",value="16 teams · Single elimination · 15 matches",inline=True)
        e.add_field(name="Registration",value="Locked" if tc.locked(mod.db) else "Open",inline=True)
        e.add_field(name="Registered teams",value=f"{len(teams)}/16",inline=True)
        e.add_field(name="Latest announcements",value="\n".join(f"**{r['title']}** · {r['content'][:130]}" for r in latest) or "No announcements yet.",inline=False)
        e.set_footer(text="Use the menu below to navigate. No live round or upcoming-match countdown is shown here.")
    elif key=="information":
        e.description=_setting(mod.db,"tournament_description","Craftcore Minecraft PvP 2v2 tournament.")
        e.add_field(name="Format",value=_setting(mod.db,"tournament_format","16-team single elimination; 15 matches."),inline=False)
        e.add_field(name="Team requirements",value="Each team has two players identified by their Minecraft usernames.",inline=False)
        e.add_field(name="Registration",value=_setting(mod.db,"registration_information","Registration is managed by tournament staff."),inline=False)
        e.add_field(name="Rules",value=_setting(mod.db,"tournament_rules","Play assigned matches, follow the match draft, and respect staff rulings."),inline=False)
        e.add_field(name="Draft",value="Team A ban · Team B ban · A pick · B pick · A pick · B pick · A ban · B ban · Decider.",inline=False)
        e.add_field(name="Disqualification",value="A forfeit or disqualification makes both players on that duo ineligible for the participant giveaway.",inline=False)
    elif key=="prizes":
        e.add_field(name="Winning duo",value="2x Nitro — one prize for each player on the champion team.",inline=False)
        e.add_field(name="Participant giveaway",value="1x Nitro Basic Wheel Spin. Every player must play all matches assigned to their team. Winning the tournament is not required.",inline=False)
        e.add_field(name="CraftCoin leaderboard",value="The participant with the highest CraftCoin balance at tournament end receives a special in-game reward when the new season starts.",inline=False)
        draw=mod.db.execute("SELECT username,claim_status FROM cc_giveaway_draws ORDER BY id DESC LIMIT 1").fetchone()
        e.add_field(name="Participant giveaway status",value=f"Winner: {draw['username']} · {draw['claim_status']}" if draw else "Not drawn yet.",inline=False)
    elif key=="gamemodes":
        e.description="\n".join(f"{i}. {mode}" for i,mode in enumerate(tc.MODES,1))
        e.add_field(name="Ban-and-pick",value="The teams each ban once, pick twice, ban once, then use the final remaining mode as the decider.",inline=False)
        e.add_field(name="Important",value="Cart PvP is HT ONLY.",inline=False)
    elif key=="teams":
        lines=[f"**Seed {t['seed'] or '—'} · {t['name']}**\n{t['player1']} + {t['player2']}" for t in teams]
        e.description="\n\n".join(lines)[:4000] or "No teams registered."
        e.set_footer(text=f"{len(teams)}/16 teams")
    elif key=="bracket":
        return td.bracket_embed(mod.db)
    elif key=="matches":
        rows=tc.bracket(mod.db)
        lines=[]
        for m in rows:
            score=f" · {m['score_a']}-{m['score_b']}" if m["score_a"] is not None else ""
            ref=f" · Ref: {m['referee']}" if m["referee"] else ""
            winner=f" · Winner: {m['winner_name']}" if m["winner_name"] else ""
            lines.append(f"**{m['match_key']}** · {m['team_a_name']} vs {m['team_b_name']}{score}{winner}{ref}")
        e.description="\n".join(lines)[:4000] or "Bracket is not initialised."
        e.set_footer(text="Use /match view or /match result for individual match management.")
    elif key=="draft":
        lines=[]
        for m in tc.bracket(mod.db):
            state=tc.draft(mod.db,m["match_key"])
            if state["selections"]:
                picked=", ".join(f"{x['action']} {x['gamemode']}" for x in state["selections"])
                status="Complete" if state["complete"] else f"Step {state['next_step']}/9"
                lines.append(f"**{m['match_key']}** · {status}\n{picked}")
        e.description="\n\n".join(lines)[:4000] or "No match drafts have started."
        e.set_footer(text="Use /draft start, /draft view, /draft undo or /draft override.")
    elif key=="coins":
        e.description="Participant wallets start at 100 CraftCoins. A correct prediction returns 1.5x the stake in total, including the original stake."
        if user_id:
            link=mod.db.execute("SELECT username FROM cc_discord_links WHERE discord_id=?",(user_id,)).fetchone()
            wallet=mod.get_player(user_id)
            if link and wallet:
                pending=mod.pending_total(user_id)
                e.add_field(name="Your linked account",value=link["username"],inline=True)
                e.add_field(name="Balance",value=f"{wallet['balance']} CC",inline=True)
                e.add_field(name="Currently wagered",value=f"{pending} CC",inline=True)
                tx=tc.coin_history(mod.db,user_id,5)
                e.add_field(name="Recent transactions",value="\n".join(f"{x['delta']:+} · {x['reason']} · balance {x['balance_after']}" for x in tx) or "No transactions.",inline=False)
            else:e.add_field(name="Wallet",value="Your Discord account is not linked to a tournament Minecraft username.",inline=False)
        e.set_footer(text="Use /coins balance, /coins history, /coins leaderboard and /mybets.")
    elif key=="leaderboard":
        metrics=_coin_metrics(mod)
        balance=sorted(metrics,key=lambda x:(-x["balance"],x["username"].casefold()))
        profit=sorted(metrics,key=lambda x:(-x["profit"],x["username"].casefold()))
        accuracy=sorted((x for x in metrics if x["bets"]),key=lambda x:(-x["accuracy"],-x["bets"],x["username"].casefold()))
        e.add_field(name="Highest balances",value="\n".join(f"{i}. {x['username']} · {x['balance']} CC" for i,x in enumerate(balance[:10],1)) or "No linked wallets.",inline=False)
        e.add_field(name="Biggest net profits",value="\n".join(f"{i}. {x['username']} · {x['profit']:+} CC" for i,x in enumerate(profit[:5],1)) or "No settled bets.",inline=True)
        e.add_field(name="Best prediction accuracy",value="\n".join(f"{i}. {x['username']} · {x['accuracy']}% ({x['bets']})" for i,x in enumerate(accuracy[:5],1)) or "No settled bets.",inline=True)
    elif key=="giveaway":
        status=tc.giveaway_eligibility(mod.db)
        eligible=sum(1 for x in status if x["status"]=="eligible")
        pending=sum(1 for x in status if x["status"]=="pending")
        out=sum(1 for x in status if x["status"]=="ineligible")
        draw=mod.db.execute("SELECT * FROM cc_giveaway_draws ORDER BY id DESC LIMIT 1").fetchone()
        e.add_field(name="Prize",value="1x Nitro Basic Wheel Spin (separate from the champion duo's 2x Nitro).",inline=False)
        e.add_field(name="Eligibility",value=f"✅ Eligible: {eligible}\n⏳ Pending: {pending}\n🚫 Ineligible: {out}",inline=True)
        e.add_field(name="Giveaway draw",value=f"Winner: {draw['username']} · {draw['claim_status']}" if draw else "Not drawn yet.",inline=True)
        e.add_field(name="Rule",value="A player must complete every match assigned to their duo. A forfeit or disqualification makes the duo ineligible. Tournament victory is not required.",inline=False)
    elif key=="hall":
        stats=tc.team_stats(mod.db)
        champion=next((x for x in stats if x["placement"]==1),None)
        runner=next((x for x in stats if x["placement"]==2),None)
        semi=[x["name"] for x in stats if x["placement"] in (3,4)]
        awards=mod.db.execute("SELECT * FROM cc_awards ORDER BY award_key").fetchall()
        e.add_field(name="Champion",value=champion["name"] if champion else "Not decided yet.",inline=True)
        e.add_field(name="Runner-up",value=runner["name"] if runner else "Not decided yet.",inline=True)
        e.add_field(name="Semifinalists",value=", ".join(semi) or "Not decided yet.",inline=False)
        e.add_field(name="MVP and awards",value="\n".join(f"**{x['award_key']}** · {x['username']} — {x['description'] or ''}" for x in awards) or "No awards recorded yet.",inline=False)
    elif key=="players":
        rows=tc.player_stats(mod.db)
        rows.sort(key=lambda x:(x["placement"] is None,x["placement"] or 99,-x["wins"],x["username"].casefold()))
        lines=[f"**{x['username']}** · {x['team_name']} · {x['wins']}W/{x['losses']}L · {x['win_percent']}% · {('Place '+str(x['placement'])) if x['placement'] else 'In progress'}" for x in rows[:20]]
        e.description="\n".join(lines)[:4000] or "No player statistics available."
    return e

class PanelSelect(discord.ui.Select):
    def __init__(self,mod):
        self.mod=mod
        super().__init__(placeholder="Open a tournament panel…",min_values=1,max_values=1,
            options=[discord.SelectOption(label=label,title=label,value=key) for key,label in PANEL_KEYS],
            custom_id="craftcore:panels:navigate")
    async def callback(self,interaction):
        await interaction.response.send_message(embed=panel_embed(self.mod,self.values[0],interaction.user.id),
                                                view=PanelNavView(self.mod),ephemeral=True)

class PanelNavView(discord.ui.View):
    def __init__(self,mod):
        super().__init__(timeout=None)
        self.mod=mod
        self.add_item(PanelSelect(mod))
        for key,label in (("home","Home"),("teams","Teams"),("bracket","Bracket"),("coins","My Coins")):
            button=discord.ui.Button(label=label,style=discord.ButtonStyle.secondary,
                                     custom_id=f"craftcore:panels:{key}")
            async def callback(interaction,k=key):
                await interaction.response.send_message(embed=panel_embed(self.mod,k,interaction.user.id),
                                                        view=PanelNavView(self.mod),ephemeral=True)
            button.callback=callback
            self.add_item(button)

async def publish_announcement(mod,category,title,content,actor_id=None):
    channel_row=mod.db.execute("SELECT value FROM cc_tournament_settings WHERE key='announcement_channel'").fetchone()
    channel_id=int(channel_row["value"]) if channel_row else None
    message_id=None
    if channel_id:
        try:
            channel=mod.bot.get_channel(channel_id) or await mod.bot.fetch_channel(channel_id)
            embed=discord.Embed(title=title,description=content[:4000],colour=discord.Colour.blurple())
            msg=await channel.send(embed=embed)
            message_id=msg.id
        except Exception:
            log.exception("Could not send tournament announcement %s",title)
    with mod.db:
        mod.db.execute("INSERT INTO cc_announcements(category,title,content,channel_id,message_id,actor_id,created_at) VALUES(?,?,?,?,?,?,?)",
                       (category,title,content,channel_id,message_id,actor_id,tc.now()))
        tc.audit(mod.db,"announcement_created",{"category":category,"title":title,"message_id":message_id},actor_id)
    return message_id

async def refresh_panels(mod,repair=False):
    rows=mod.db.execute("SELECT * FROM cc_panel_messages ORDER BY panel_key").fetchall()
    by_key={r["panel_key"]:dict(r) for r in rows}
    channel_cache={}
    updated=created=0
    for key,label in PANEL_KEYS:
        row=by_key.get(key)
        if not row:
            continue
        channel_id=row["channel_id"]
        channel=channel_cache.get(channel_id)
        if channel is None:
            try:channel=mod.bot.get_channel(channel_id) or await mod.bot.fetch_channel(channel_id)
            except Exception:
                log.exception("Panel channel unavailable: %s",channel_id);continue
            channel_cache[channel_id]=channel
        message=None
        try:message=await channel.fetch_message(row["message_id"])
        except discord.NotFound:message=None
        except Exception:
            log.exception("Could not fetch panel message %s",row["message_id"])
        if message:
            try:
                await message.edit(embed=panel_embed(mod,key),view=PanelNavView(mod))
                updated+=1
            except Exception:log.exception("Could not refresh panel %s",key)
        elif repair:
            try:
                message=await channel.send(embed=panel_embed(mod,key),view=PanelNavView(mod))
                with mod.db:
                    mod.db.execute("UPDATE cc_panel_messages SET message_id=?,updated_at=? WHERE panel_key=?",
                                   (message.id,tc.now(),key))
                created+=1
            except Exception:log.exception("Could not repair panel %s",key)
    return updated,created

def install(mod):
    tc.ensure_schema(mod.db)
    mod.panel_view_factory=lambda:PanelNavView(mod)
    mod.publish_tournament_announcement=lambda category,title,content,actor_id=None:publish_announcement(mod,category,title,content,actor_id)
    mod.refresh_tournament_panels=lambda repair=False:refresh_panels(mod,repair)

    panel_group=app_commands.Group(name="panels",description="Permanent tournament information panels",guild_only=True)
    mod.bot.tree.add_command(panel_group)

    @panel_group.command(name="setup",description="Staff: create or update all 13 tournament panels in a channel")
    @mod.staff_only
    async def panels_setup(interaction,channel:discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        for key,label in PANEL_KEYS:
            old=mod.db.execute("SELECT * FROM cc_panel_messages WHERE panel_key=?",(key,)).fetchone()
            msg=None
            if old and old["channel_id"]==channel.id:
                try:msg=await channel.fetch_message(old["message_id"])
                except discord.NotFound:msg=None
                except Exception:msg=None
            if msg:
                await msg.edit(embed=panel_embed(mod,key),view=PanelNavView(mod))
            else:
                msg=await channel.send(embed=panel_embed(mod,key),view=PanelNavView(mod))
            with mod.db:
                mod.db.execute("INSERT INTO cc_panel_messages(panel_key,guild_id,channel_id,message_id,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(panel_key) DO UPDATE SET guild_id=excluded.guild_id,channel_id=excluded.channel_id,message_id=excluded.message_id,updated_at=excluded.updated_at",
                               (key,interaction.guild_id,channel.id,msg.id,tc.now()))
        await interaction.followup.send(f"✅ Created/refreshed all {len(PANEL_KEYS)} panels in {channel.mention}. Message and channel IDs are stored for restart recovery.",ephemeral=True)

    @panel_group.command(name="refresh",description="Staff: refresh all saved information panels")
    @mod.staff_only
    async def panels_refresh(interaction):
        await interaction.response.defer(ephemeral=True)
        updated,missing=await refresh_panels(mod,False)
        await interaction.followup.send(f"✅ Refreshed {updated} panels. Missing messages: {missing}. Use /panels repair to recreate missing panels.",ephemeral=True)

    @panel_group.command(name="repair",description="Staff: recreate missing panel messages and refresh all panels")
    @mod.staff_only
    async def panels_repair(interaction):
        await interaction.response.defer(ephemeral=True)
        updated,created=await refresh_panels(mod,True)
        await interaction.followup.send(f"✅ Refreshed {updated} panels and recreated {created} missing messages.",ephemeral=True)

    ann=app_commands.Group(name="announcement",description="Tournament announcements",guild_only=True)
    mod.bot.tree.add_command(ann)
    @ann.command(name="set_channel",description="Staff: choose where tournament announcements are posted")
    @mod.staff_only
    async def announcement_channel(interaction,channel:discord.TextChannel):
        with mod.db:
            mod.db.execute("INSERT INTO cc_tournament_settings(key,value) VALUES('announcement_channel',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(channel.id),))
            tc.audit(mod.db,"announcement_channel_changed",{"channel_id":channel.id},interaction.user.id)
        await interaction.response.send_message(f"✅ Tournament announcements will be posted in {channel.mention}.",ephemeral=True)
    @ann.command(name="post",description="Staff: publish a tournament announcement")
    @mod.staff_only
    async def announcement_post(interaction,title:str,message:str):
        await publish_announcement(mod,"staff",title,message,interaction.user.id)
        await interaction.response.send_message("✅ Announcement saved. It was posted if an announcement channel is configured.",ephemeral=True)

    # Editable details are stored in the single tournament settings table.
    tg=mod.bot.tree.get_command("tournament")
    if isinstance(tg,app_commands.Group):
        @tg.command(name="edit_info",description="Staff: edit tournament description, format, rules or registration information")
        @app_commands.choices(field=[
            app_commands.Choice(name="Description",value="tournament_description"),
            app_commands.Choice(name="Format",value="tournament_format"),
            app_commands.Choice(name="Rules",value="tournament_rules"),
            app_commands.Choice(name="Registration",value="registration_information"),
        ])
        @mod.staff_only
        async def tournament_edit_info(interaction,field:app_commands.Choice[str],value:str):
            if len(value)>1500:return await interaction.response.send_message("Information must be 1500 characters or fewer.",ephemeral=True)
            with mod.db:
                mod.db.execute("INSERT INTO cc_tournament_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(field.value,value))
                tc.audit(mod.db,"tournament_information_edited",{"field":field.value,"value":value},interaction.user.id)
            await refresh_panels(mod,False)
            await interaction.response.send_message(f"✅ Updated tournament {field.name.lower()}.",ephemeral=True)

    log.info("Thirteen persistent tournament panels and announcements installed.")
