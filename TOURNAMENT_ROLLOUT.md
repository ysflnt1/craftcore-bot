# Craftcore 2v2 Tournament implementation notes

This branch adds the first tournament foundation to the existing bot. It is additive: the existing players, teams, matches, draft, games, and bets tables are not dropped or converted. New tournament state uses cc_ tables.

## Included in this branch

- Fixed 16-team Minecraft username roster, with the supplied spelling and casing.
- Idempotent startup seeding and explicit staff re-initialisation.
- CSV and JSON team imports; CSV and JSON team exports.
- Team create, edit, delete protection, search, list, seeds, and seed randomisation.
- Fixed 15-match single-elimination bracket with the supplied Round 1 pairings and derived Quarterfinals, Semifinals, and Grand Final.
- Automatic bracket slot advancement.
- Staff result entry, scores, forfeit/disqualification/override result types, undo, history, and confirmation before completed downstream results are cleared.
- Downstream draft invalidation when the teams in a later match change.
- Bracket JSON export/import with roster and match-ID validation and overwrite confirmation.
- Nine-step persisted ban/pick draft with dropdown selection, duplicate-gamemode prevention, view, and undo.
- Explicit staff linking of a registered Minecraft participant to a Discord wallet.
- Linked-participant-only entry to the existing CraftCoin/betting interface, wager transaction records, wallet freeze controls, and transaction history.
- Atomic recording of the legacy match result and its outstanding bet settlements.
- Configurable STAFF_ROLE_IDS (comma-separated) in addition to the existing /setup staff role.

## Commands added or adjusted

Minecraft tournament roster commands live under /team:
- /team create, /team edit, /team delete, /team list, /team search
- /team import, /team export, /team shuffle_seeds

Legacy Discord-member team commands remain available as /team create_legacy, /team delete_legacy, and /team list_legacy.

Other tournament commands:
- /tournament status, /tournament initialise, /tournament lock_registrations, /tournament unlock_registrations
- /bracket view, /bracket result, /bracket undo, /bracket history, /bracket reset, /bracket export, /bracket import
- /draft start, /draft view, /draft undo
- /participant link, /participant unlink
- /coins history, /coins freeze, /coins unfreeze

Staff role checks still permit Discord administrators and members with Manage Server. Configure STAFF_ROLE_IDS in Render as a comma-separated list of role IDs, or use the existing /setup command for one staff role.

## Database and data safety

The migration creates cc_tournament_teams, cc_tournament_players, cc_discord_links, cc_bracket_matches, cc_match_history, cc_draft_selections, cc_tournament_settings, cc_audit_log, cc_coin_transactions, and cc_wallet_controls. It does not delete old data. The initial roster seed is safe to run repeatedly. A conflicting pre-existing tournament roster stops initialisation rather than overwriting it.

Existing CraftCoin balances and legacy betting rows remain in their original tables. New coin ledger entries are recorded from the point this branch is deployed; historical movements cannot be reconstructed from the existing schema.

## Render persistence requirement

The existing Render service is a Free web service and SQLite writes to the relative craftcore.db path. That is not durable across restarts/redeployments. To use SQLite safely, add a paid Render persistent disk mounted at /var/data and set DB_PATH=/var/data/craftcore.db. Do not set that path until the disk is attached. Alternatively, migrate the bot to a managed PostgreSQL database in a separate change. This branch does not change your Render plan or create a billable service.

STAFF_ROLE_IDS is an optional comma-separated list. Existing settings remain:
- DISCORD_TOKEN
- GUILD_ID
- STARTING_COINS (defaults to 100)
- WIN_MULTIPLIER (defaults to 1.5)
- DB_PATH (currently defaults to craftcore.db)

## Not yet complete

This is not the complete feature list from the development brief. In particular, the following still need implementation and end-to-end verification:

- The existing betting matches table is still the legacy match system; the new cc_bracket_matches bracket has not yet been bridged to it. Bets are not yet offered directly against the new fixed-bracket match IDs.
- Full interactive betting administration, disputed-match settlement workflow, and all requested economy audit/leaderboard metrics.
- The 13 permanent Discord information panels and their persisted message/channel refresh/repair lifecycle (the existing legacy betting panel remains).
- Tournament-wide player/team statistics, placement, MVP/awards, Hall of Fame, and participant giveaway eligibility/claim tracking against the new bracket.
- Full match referee assignment, announcements, staff audit coverage for every legacy command, captain-based draft permissions, and a full reset/import/export treatment of every draft and panel state.
- A durable Render database configuration. Free Render storage cannot meet the tournament persistence requirement.

## Checks

Run locally with Python 3.12:

    python -m compileall -q .
    pip install pytest
    pytest -q

The GitHub Actions workflow in .github/workflows/tests.yml compiles the project, imports the bot to validate Discord command registration, and runs the tournament-core tests on pushes and pull requests. The latest checked run passed: 10 tests, 0 failures.
