
import logging
from typing import Optional

import discord
from discord import app_commands
import tournament_core as tc

log = logging.getLogger("craftcore-public-betting")


def install(mod):
    """
    Install public betting features into the existing Craftcore bot.

    The original bot module is passed in so existing tournament
    commands, database tables and match functionality are preserved.
    """

    db = mod.db
    bot = mod.bot

    def ensure_player(user_id):
        """Create a CraftCoin wallet for any Discord user on first use."""
        try:
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute(
                "INSERT OR IGNORE INTO players(user_id, balance) VALUES(?, ?)",
                (user_id, mod.START_COINS),
            )
            if inserted.rowcount == 1:
                tc.record_coin(db, user_id, mod.START_COINS, "starting_balance")
            db.commit()
        except Exception:
            db.rollback()
            log.exception("Could not create CraftCoin wallet for Discord user %s", user_id)
            return None
        return mod.get_player(user_id)

    def public_place_bet(uid, mid, team_id, amount):
        """Allow any Discord server member to place a wager."""

        match_data = mod.get_match(mid)

        if not db.execute("SELECT 1 FROM cc_bracket_matches WHERE legacy_match_id=?",(mid,)).fetchone():
            return False, "Betting is available only for matches in the fixed Craftcore bracket."
        if not match_data or match_data["status"] != "open":
            return False, "Predictions for this match are closed."

        if team_id not in (match_data["team_a"], match_data["team_b"]):
            return False, "Invalid team selected."

        if amount <= 0:
            return False, "Wager must be at least 1 CraftCoin."

        player = ensure_player(uid)
        if not player:
            return False, "Your CraftCoin wallet could not be created. Please try again."
        if tc.wallet_frozen(db, uid):
            return False, "Your CraftCoin wallet is frozen. Contact tournament staff."

        old = mod.q1(
            "SELECT * FROM bets WHERE match_id=? AND user_id=?",
            (mid, uid),
        )

        if old and old["settled"]:
            return False, "This match already has a settled wager for your account."

        available = player["balance"] + (old["amount"] if old else 0)

        if amount > available:
            return False, (
                f"You only have **{available}** CraftCoins available."
            )

        try:
            db.execute("BEGIN IMMEDIATE")
            locked_match = mod.get_match(mid)
            locked_player = mod.get_player(uid)
            if not locked_match or locked_match["status"] != "open":
                db.rollback()
                return False, "Predictions for this match are closed."
            locked_old = mod.q1("SELECT * FROM bets WHERE match_id=? AND user_id=?", (mid, uid))
            if locked_old and locked_old["settled"]:
                db.rollback()
                return False, "This match already has a settled wager for your account."
            locked_available = locked_player["balance"] + (locked_old["amount"] if locked_old else 0)
            if amount > locked_available:
                db.rollback()
                return False, f"You only have **{locked_available}** CraftCoins available."
            if tc.wallet_frozen(db, uid):
                db.rollback()
                return False, "Your CraftCoin wallet is frozen. Contact tournament staff."
            if locked_old:
                db.execute("UPDATE players SET balance=balance+? WHERE user_id=?", (locked_old["amount"], uid))
                tc.record_coin(db, uid, locked_old["amount"], "bet_replaced_refund", match_id=mid)
            db.execute(
                "UPDATE players SET balance=balance-? WHERE user_id=? AND balance>=?",
                (amount, uid, amount),
            )
            if db.execute("SELECT changes()").fetchone()[0] != 1:
                raise ValueError("Insufficient CraftCoins.")
            tc.record_coin(db, uid, -amount, "bet_placed", match_id=mid)
            db.execute(
                """INSERT INTO bets(match_id,user_id,team_id,amount)
                   VALUES(?,?,?,?)
                   ON CONFLICT(match_id,user_id)
                   DO UPDATE SET team_id=excluded.team_id,amount=excluded.amount,settled=0,payout=0""",
                (mid, uid, team_id, amount),
            )
            db.commit()
        except Exception:
            db.rollback()
            log.exception("Could not place bet for user %s on match %s", uid, mid)
            return False, "Your wager could not be saved. No balance was changed."

        note = " (replaced your previous wager)" if old else ""

        total_return = int(round(amount * mod.WIN_MULT))
        return True, (
            f"Wagered **{amount}** CraftCoins on **{mod.tn(team_id)}**"
            f"{note}. If correct, your total return is **{total_return}** "
            f"CraftCoins (stake included; net profit **{total_return - amount}**)."
        )

    # Replace the original betting function.
    # BetModal will now use this public version.
    mod.place_bet = public_place_bet
    mod.ensure_player = ensure_player

    async def public_bet_button_callback(self, interaction):
        """Allow any Discord server member to open the betting modal."""

        match_data = mod.get_match(self.match_id)

        if not db.execute("SELECT 1 FROM cc_bracket_matches WHERE legacy_match_id=?",(self.match_id,)).fetchone():
            return await interaction.response.send_message("This is not a match in the fixed tournament bracket.",ephemeral=True)
        if not match_data or match_data["status"] != "open":
            return await interaction.response.send_message(
                "Predictions for this match are closed.",
                ephemeral=True,
            )

        # Automatically provision a wallet for every Discord server member.
        if not ensure_player(interaction.user.id):
            return await interaction.response.send_message(
                "Your CraftCoin wallet could not be created. Please try again.",
                ephemeral=True,
            )

        await interaction.response.send_modal(
            mod.BetModal(self.match_id, self.side)
        )

    # Replace the original participant-only betting button callback.
    mod.BetButton.callback = public_bet_button_callback

    async def public_balance(interaction: discord.Interaction,
                             user: Optional[discord.Member] = None):
        """Public wallet command."""

        target = user or interaction.user
        player = ensure_player(target.id)
        if not player:
            return await interaction.response.send_message(
                "Your CraftCoin wallet could not be loaded. Please try again.",
                ephemeral=True,
            )

        pending = mod.pending_total(target.id)

        await interaction.response.send_message(
            f"💰 **{target.display_name}'s CraftCoin Wallet**\n\n"
            f"Available balance: **{player['balance']} CraftCoins**\n"
            f"Currently wagered: **{pending} CraftCoins**",
            ephemeral=True,
        )

    # Register the public balance command.
    existing_balance = bot.tree.get_command("balance")

    if existing_balance:
        bot.tree.remove_command("balance")

    bot.tree.add_command(
        app_commands.Command(
            name="balance",
            description="Check your CraftCoin wallet",
            callback=public_balance,
        )
    )

    async def show_balance(interaction):
        player = ensure_player(interaction.user.id)
        if not player:
            return await interaction.response.send_message(
                "You need to be linked to a registered Minecraft participant before using a CraftCoin wallet.",
                ephemeral=True,
            )
        pending = mod.pending_total(interaction.user.id)

        await interaction.response.send_message(
            f"💰 **Your CraftCoin Wallet**\n\n"
            f"Balance: **{player['balance']} CraftCoins**\n"
            f"Currently wagered: **{pending} CraftCoins**",
            ephemeral=True,
        )

    async def show_leaderboard(interaction):
        rows = mod.q(
            "SELECT * FROM players ORDER BY balance DESC LIMIT 10"
        )

        if not rows:
            return await interaction.response.send_message(
                "No players on the leaderboard yet.",
                ephemeral=True,
            )

        medals = ["🥇", "🥈", "🥉"]

        lines = []

        for i, player in enumerate(rows):
            position = (
                medals[i] if i < 3 else f"`{i + 1}.`"
            )

            lines.append(
                f"{position} <@{player['user_id']}> — "
                f"**{player['balance']} CraftCoins**"
            )

        embed = discord.Embed(
            title="🏆 Craftcore Leaderboard",
            description="\n".join(lines),
            colour=discord.Colour.gold(),
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def show_matches(interaction):
        rows = mod.q(
            """
            SELECT * FROM matches
            WHERE status IN ('open', 'locked')
            ORDER BY id DESC
            LIMIT 15
            """
        )

        if not rows:
            return await interaction.response.send_message(
                "There are no active matches right now.",
                ephemeral=True,
            )

        embed = discord.Embed(
            title="⚔️ Active Craftcore Matches",
            colour=discord.Colour.blurple(),
        )

        for match_data in rows:
            team_a = mod.tn(match_data["team_a"])
            team_b = mod.tn(match_data["team_b"])

            status = mod.STATUS_TEXT.get(
                match_data["status"],
                match_data["status"],
            )

            embed.add_field(
                name=f"Match #{match_data['id']} · {match_data['round']}",
                value=(
                    f"**{team_a} vs {team_b}**\n"
                    f"{status}\n"
                    f"Use `/match view match_id:{match_data['id']}` "
                    f"for details."
                ),
                inline=False,
            )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )

    class BettingPanel(discord.ui.View):
        """Persistent public betting dashboard."""

        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(
            label="My Balance",
            emoji="💰",
            style=discord.ButtonStyle.success,
            custom_id="craftcore:panel:balance",
        )
        async def balance_button(self, interaction, button):
            await show_balance(interaction)

        @discord.ui.button(
            label="Leaderboard",
            emoji="🏆",
            style=discord.ButtonStyle.primary,
            custom_id="craftcore:panel:leaderboard",
        )
        async def leaderboard_button(self, interaction, button):
            await show_leaderboard(interaction)

        @discord.ui.button(
            label="Active Matches",
            emoji="⚔️",
            style=discord.ButtonStyle.secondary,
            custom_id="craftcore:panel:matches",
        )
        async def matches_button(self, interaction, button):
            await show_matches(interaction)

    def create_panel_embed():
        embed = discord.Embed(
            title="💰 CRAFTCORE | BETTING EXCHANGE",
            description=(
                "Welcome to the official Craftcore 2v2 betting exchange!\n\n"
                "Predict match winners, earn CraftCoins and compete "
                "with everyone in the server."
            ),
            colour=discord.Colour.gold(),
        )

        embed.add_field(
            name="🪙 Starting Balance",
            value=f"**{mod.START_COINS} CraftCoins**",
            inline=True,
        )

        embed.add_field(
            name="📈 Profit Multiplier",
            value=f"**{mod.WIN_MULT}x**",
            inline=True,
        )

        embed.add_field(
            name="👥 Who Can Bet?",
            value="Anyone in this Discord server can join and place a wager.",
            inline=False,
        )

        embed.add_field(
            name="📋 How to Bet",
            value=(
                "1. Open a match in the predictions channel.\n"
                "2. Click the button for your chosen team.\n"
                "3. Enter your wager.\n"
                "4. Wait for the match result.\n"
                "5. Collect your winnings if correct."
            ),
            inline=False,
        )

        embed.add_field(
            name="🎮 Useful Commands",
            value=(
                "`/balance` — Check your wallet\n"
                "`/mybets` — View your wagers\n"
                "`/leaderboard` — Top bettors\n"
                "`/matches` — View tournament matches"
            ),
            inline=False,
        )

        embed.set_footer(
            text="Craftcore 2v2 Tournament • CraftCoin System"
        )

        return embed

    @app_commands.guild_only()
    @mod.staff_only
    async def panel_command(interaction: discord.Interaction):
        """Staff command to publish the public betting panel."""

        await interaction.response.send_message(
            embed=create_panel_embed(),
            view=BettingPanel(),
        )

    bot.tree.add_command(
        app_commands.Command(
            name="panel",
            description="Staff: publish the Craftcore betting dashboard",
            callback=panel_command,
        )
    )

    # Register persistent panel buttons so they survive bot restarts.
    bot.add_view(BettingPanel())

    log.info("Public betting system and dashboard installed.")
