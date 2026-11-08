import sqlite3
import unittest

import tournament_core as tc


class TournamentCoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        tc.ensure_schema(self.db)

    def tearDown(self):
        self.db.close()

    def initialise(self):
        self.assertEqual(tc.seed_initial_teams(self.db), 16)
        self.assertEqual(tc.seed_initial_teams(self.db), 0)
        self.assertTrue(tc.initialise_bracket(self.db))
        self.assertFalse(tc.initialise_bracket(self.db))

    def test_initial_roster_is_exact_and_idempotent(self):
        self.initialise()
        rows = tc.teams(self.db)
        self.assertEqual(len(rows), 16)
        self.assertEqual(
            [(t["name"], t["player1"], t["player2"]) for t in rows],
            list(tc.INITIAL),
        )
        self.assertEqual(len(tc.bracket(self.db)), 15)

    def test_duplicate_minecraft_username_is_rejected_case_insensitively(self):
        self.initialise()
        with self.assertRaises(ValueError):
            tc.create_team(self.db, "Extra team", "LEVI", "AnotherPlayer")

    def test_fixed_bracket_advances_automatically(self):
        self.initialise()
        first = tc.get_match(self.db, "R1-L1")
        second = tc.get_match(self.db, "R1-L2")
        tc.set_result(self.db, "R1-L1", first["team_a_id"])
        tc.set_result(self.db, "R1-L2", second["team_b_id"])
        qf = tc.get_match(self.db, "QF-L1")
        self.assertEqual(qf["team_a_id"], first["team_a_id"])
        self.assertEqual(qf["team_b_id"], second["team_b_id"])
        self.assertEqual(qf["status"], "ready")

    def test_all_nine_draft_turns_and_duplicate_prevention(self):
        self.initialise()
        result = None
        for mode in tc.MODES:
            result = tc.draft_add(self.db, "R1-L1", mode)
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["selections"]), 9)
        self.assertEqual(result["selections"][0]["action"], "ban")
        self.assertEqual(result["selections"][7]["action"], "ban")
        self.assertEqual(result["selections"][8]["action"], "decider")
        with self.assertRaises(ValueError):
            tc.draft_add(self.db, "R1-L1", tc.MODES[0])

    def test_downstream_results_require_confirmation_and_are_cleared(self):
        self.initialise()
        for key in ("R1-L1", "R1-L2", "R1-L3", "R1-L4"):
            m = tc.get_match(self.db, key)
            tc.set_result(self.db, key, m["team_a_id"])
        for key in ("QF-L1", "QF-L2"):
            m = tc.get_match(self.db, key)
            tc.set_result(self.db, key, m["team_a_id"])
        m = tc.get_match(self.db, "R1-L1")
        with self.assertRaises(tc.DownstreamResultsExist) as caught:
            tc.set_result(self.db, "R1-L1", m["team_b_id"])
        self.assertIn("QF-L1", caught.exception.match_keys)
        tc.set_result(self.db, "R1-L1", m["team_b_id"], confirm=True)
        self.assertIsNone(tc.get_match(self.db, "QF-L1")["winner_team_id"])
        self.assertIsNotNone(tc.get_match(self.db, "QF-L2")["winner_team_id"])

    def test_full_bracket_final_and_champion(self):
        self.initialise()
        # Every match is won by Team A; advancement resolves topologically.
        for key, *_ in tc.DEFS:
            match = tc.get_match(self.db, key)
            tc.set_result(self.db, key, match["team_a_id"])
        gf = tc.get_match(self.db, "GF")
        self.assertEqual(gf["status"], "completed")
        champion = self.db.execute(
            "SELECT value FROM cc_tournament_settings WHERE key='champion_team_id'"
        ).fetchone()
        self.assertEqual(int(champion["value"]), gf["winner_team_id"])

    def test_bracket_backup_round_trip_and_validation(self):
        self.initialise()
        m = tc.get_match(self.db, "R1-L1")
        tc.set_result(self.db, "R1-L1", m["team_a_id"], 3, 1)
        raw = tc.export_bracket(self.db)
        tc.set_result(self.db, "R1-L1", m["team_b_id"])
        tc.import_bracket(self.db, raw)
        self.assertEqual(tc.get_match(self.db, "R1-L1")["winner_team_id"], m["team_a_id"])
        broken = raw.replace('"R1-L1"', '"INVALID"')
        with self.assertRaises(ValueError):
            tc.import_bracket(self.db, broken)

    def test_bracket_reset_keeps_teams_and_legacy_wallet_rows(self):
        self.initialise()
        self.db.execute("CREATE TABLE players(user_id INTEGER PRIMARY KEY,balance INTEGER NOT NULL)")
        self.db.execute("INSERT INTO players VALUES(123,987)")
        m = tc.get_match(self.db, "R1-L1")
        tc.set_result(self.db, "R1-L1", m["team_a_id"])
        tc.reset_bracket(self.db)
        self.assertEqual(len(tc.teams(self.db)), 16)
        self.assertEqual(self.db.execute("SELECT balance FROM players WHERE user_id=123").fetchone()[0], 987)
        self.assertEqual(tc.get_match(self.db, "R1-L1")["status"], "ready")

    def test_import_same_roster_is_idempotent(self):
        self.initialise()
        rows = [{"name": n, "player1": a, "player2": b} for n, a, b in tc.INITIAL]
        self.assertEqual(tc.import_teams(self.db, rows), 0)


if __name__ == "__main__":
    unittest.main()
