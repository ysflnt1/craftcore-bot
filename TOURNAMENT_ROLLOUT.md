# Craftcore 2v2 Tournament rollout

This branch extends the existing Discord bot for one Minecraft PvP 2v2 tournament. It adds new tournament tables and commands while retaining the existing bot, game engine, CraftCoin tables and betting history.

## Implemented

### Roster and bracket
- Seeds the 16 supplied duos, preserving Minecraft username spelling and case.
- Each team has two Minecraft usernames; duplicate usernames are rejected case-insensitively.
- A separate immutable bracket slot is maintained independently of display seeds.
- Before launch, staff can create, edit, delete, search, list, import or export teams and replace a registered duo without changing its Round 1 position.
- Staff can lock/unlock registration, shuffle display seeds, and initialise the bracket when the roster is ready.
- Initial pairings are fixed: Levi/Yugo, Asharam/grape214, Kawaii/Firepro, motives/Terabyte, Itsuki/Hasdjd, KKalataa/Vyrqz, karlstun/jambaza, and jelka/riuchantou.
- Single-elimination progression is persisted for all 15 matches: 8 Round 1, 4 Quarterfinals, 2 Semifinals and the Grand Final.
- Staff result, score, referee, forfeit and disqualification management; match history; dispute open/resolve; result undo/correction; bracket reset; validated JSON backup import/export.
- Changing an upstream result prompts for confirmation, cancels dependent legacy matches, refunds affected wagers, clears dependent drafts/results and rebuilds the downstream betting match records. A correction is blocked rather than making a wallet negative.

### Gameplay and betting bridge
- Every Minecraft team can be mirrored into the existing match/game tables using internal placeholder IDs for players who have not linked Discord.
- Discord linking is optional for playing; it is required only for CraftCoin wallet and betting features.
- Once both bracket teams are known, the existing match engine receives a legacy match record. Existing /match game and /match forfeit gameplay updates the Minecraft bracket automatically.
- Staff can also record a bracket result directly with /match result or /bracket result.
- The existing bet modal/panel now accepts new wagers only for fixed-bracket matches and only from Discord accounts linked to registered Minecraft participants.
- Betting closes when the match locks/starts or is disputed.
- Correct predictions return 1.5x the original stake in total (stake included). Wager debits, payouts, refunds, staff adjustments and result corrections are logged.
- Match completion and bet settlement are performed in one SQLite write transaction. Downstream wager refunds and winner corrections are guarded against negative wallet balances.
- Wallet freeze/unfreeze and transaction history are available to staff/participants.

### Draft and match administration
- Nine persisted steps: Team A ban, Team B ban, Team A pick, Team B pick, Team A pick, Team B pick, Team A ban, Team B ban, final remaining mode as decider.
- All nine configured modes are available, including Cart PvP (HT ONLY).
- Duplicate mode selection is prevented; staff can start, view, undo, override and inspect draft history.
- Drafts are copied into the legacy game engine's draft table so /match game uses the tournament's selected modes.
- Draft changes are locked after the first game is recorded.
- Staff can assign referees, raise/resolve match disputes and view match history. An open dispute blocks game recording and result settlement.

### Discord panels, information and announcements
- 13 permanent Discord information panels: Home, Tournament Information, Prizes, Gamemodes, Team Directory, Bracket, Match Information, Draft, CraftCoins, Leaderboard, Giveaway, Hall of Fame and Player Statistics.
- Panel channel/message IDs are stored. Staff commands set up, refresh and repair panel messages; persistent navigation controls are reattached after restarts.
- Tournament description, format, rules and registration information are editable by staff.
- Staff can configure an announcement channel and publish announcements. Match results, completed drafts, registration changes, giveaway draws and prize milestones generate announcements.
- A player/team statistics layer reports match counts, wins, losses, win rate and placement. Hall of Fame records the champion, runner-up, semifinalists and staff-awarded MVP/special awards.

### Giveaways and prizes
- Giveaway eligibility is computed from each Minecraft team's assigned bracket matches. A participant is eligible only after all their assigned matches are completed; a forfeit/disqualification makes both players on that duo ineligible.
- Staff can view eligibility, draw one Nitro Basic Wheel Spin winner, and review the result.
- The participant must claim the prize; staff separately confirms delivery. The bot never marks it delivered automatically.
- Each champion player can claim their share of the 2x Nitro champion prize; staff confirms delivery individually.
- At Grand Final completion, the highest linked participant CraftCoin balance is snapshotted for the new-season in-game reward. Staff confirms delivery after the new season starts.

## Main commands

- Roster: /team create, /team edit, /team delete, /team list, /team search, /team import, /team export, /team shuffle_seeds
- Setup: /tournament status, /tournament setup, /tournament initialise, /tournament lock_registrations, /tournament unlock_registrations, /tournament edit_info, /tournament reset
- Matches: /match result, /match history, /match dispute, /match resolve_dispute, /match referee; existing /match game and /match forfeit remain the game-scoring flow
- Bracket: /bracket view, /bracket result, /bracket undo, /bracket history, /bracket reset, /bracket export, /bracket import
- Draft: /draft start, /draft view, /draft undo, /draft override, /draft history
- Betting: /bet, /bets, /betting status, /mybets, /coins balance, /coins history, /coins leaderboard, /coins freeze, /coins unfreeze
- Identity: /participant link, /participant unlink
- Panels and announcements: /panels setup, /panels refresh, /panels repair, /announcement set_channel, /announcement post
- Statistics/prizes: /player stats, /player leaderboard, /team stats, /team leaderboard, /awards mvp, /awards list, /giveaway eligibility, /giveaway participants, /giveaway draw, /giveaway claim, /giveaway confirm_delivery, /prize claim, /prize status, /prize confirm_delivery, /prize coin_reward_status, /prize confirm_coin_reward

Legacy Discord-member team commands and old giveaway commands are retained with explicit *_legacy names. New tournament roster identity remains Minecraft usernames.

## Data safety and migrations

The migration creates additive cc_* tables and adds nullable columns to the new tournament tables. It does not drop, rename or convert the legacy players, teams, matches, games, draft or bets tables. Existing CraftCoin balances and bet rows remain in their original tables. New transaction ledger entries start from this deployment; historical wallet movements cannot be reconstructed.

A conflicting initial roster stops seeding rather than overwriting team/player data. Roster identities are editable only before bracket initialisation. After launch, changes to results use audited correction and wager reconciliation paths.

## Render persistence requirement

The current Render service is a Free web service and SQLite currently uses the relative craftcore.db path. Render Free filesystems are ephemeral, so this does not satisfy durable tournament storage. To retain SQLite, attach a paid persistent disk mounted at /var/data and set DB_PATH=/var/data/craftcore.db. Alternatively, a managed PostgreSQL migration can be implemented separately. This branch does not change the Render plan or create a billable service.

Do not set DB_PATH to /var/data/craftcore.db until the disk is attached. The existing web.py health endpoint remains as the Render service health check; no new user-facing website has been added.

Render environment variables include DISCORD_TOKEN, GUILD_ID, STAFF_ROLE_IDS (comma-separated), STARTING_COINS (defaults to 100), WIN_MULTIPLIER (defaults to 1.5), and DB_PATH.

## Deliberate constraints and remaining operational work

- Team create/edit/delete/import are pre-launch operations. Once the bracket is initialised, the 16 team identities are locked to protect match/bet history.
- Draft control is currently staff-only; designated captain permissions are not separately configured.
- Prize delivery is a staff-recorded confirmation; the bot does not deliver Nitro or in-game items itself.
- New coin-ledger history begins at deployment; old balance changes remain only in the pre-existing database/bets history.
- Durable Render storage must be configured before production deployment. Free Render SQLite is not safe for this tournament.
- End-to-end Discord testing still needs to be performed in a private test guild with real role/channel permissions, participant links and match panels.

## CI verification

The GitHub Actions workflow compiles all Python files, imports the bot and validates the registered command groups/persistent panel view, then runs the tournament-core tests. The latest completed core/command workflow before the latest panel-metric-only updates passed 17 tests; check the latest run on the PR head before merging.
