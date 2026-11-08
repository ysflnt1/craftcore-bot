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

    def test_prelaunch_team_replacement_keeps_original_bracket_slot(self):
        self.assertEqual(tc.seed_initial_teams(self.db), 16)
        original = tc.team(self.db, "Team Levi")
        slot = original["bracket_slot"]
        tc.delete_team(self.db, original["id"])
        replacement_id = tc.create_team(self.db, "Team Replacement", "NewPlayerA", "NewPlayerB")
        replacement = tc.team(self.db, replacement_id)
        self.assertEqual(replacement["bracket_slot"], slot)
        tc.initialise_bracket(self.db)
        first = tc.get_match(self.db, "R1-L1")
        yugo = tc.team(self.db, "Team Yugo")
        self.assertEqual(first["team_a_id"], replacement_id)
        self.assertEqual(first["team_b_id"], yugo["id"])
        with self.assertRaises(ValueError):
            tc.delete_team(self.db, replacement_id)

    def test_seed_refuses_to_exceed_fixed_sixteen_team_limit(self):
        tc.create_team(self.db, "Pre-existing custom team", "customA", "customB")
        with self.assertRaises(ValueError):
            tc.seed_initial_teams(self.db)
        self.assertEqual(len(tc.teams(self.db)), 1)

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

    def add_legacy_schema(self):
        self.db.executescript("""
        CREATE TABLE players(user_id INTEGER PRIMARY KEY,balance INTEGER NOT NULL);
        CREATE TABLE teams(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,p1 INTEGER,p2 INTEGER);
        CREATE TABLE matches(id INTEGER PRIMARY KEY AUTOINCREMENT,round TEXT,team_a INTEGER,team_b INTEGER,status TEXT,winner INTEGER,forfeit_team INTEGER,pred_channel INTEGER,pred_msg INTEGER);
        CREATE TABLE bets(match_id INTEGER,user_id INTEGER,team_id INTEGER,amount INTEGER,settled INTEGER DEFAULT 0,payout INTEGER DEFAULT 0,PRIMARY KEY(match_id,user_id));
        CREATE TABLE games(match_id INTEGER,game_no INTEGER,gamemode TEXT,score_a INTEGER,score_b INTEGER,PRIMARY KEY(match_id,game_no));
        CREATE TABLE draft(match_id INTEGER,step INTEGER,action TEXT,team_id INTEGER,gamemode TEXT,PRIMARY KEY(match_id,step));
        """)

    def link_legacy_match(self,key):
        m=tc.get_match(self.db,key)
        a=tc.team(self.db,m["team_a_id"]);b=tc.team(self.db,m["team_b_id"])
        legacy_a=self.db.execute("INSERT INTO teams(name,p1,p2) VALUES(?,?,?)",(a["name"],1000+a["id"],2000+a["id"])).lastrowid
        legacy_b=self.db.execute("INSERT INTO teams(name,p1,p2) VALUES(?,?,?)",(b["name"],1000+b["id"],2000+b["id"])).lastrowid
        self.db.execute("UPDATE cc_tournament_teams SET legacy_team_id=? WHERE id=?",(legacy_a,a["id"]))
        self.db.execute("UPDATE cc_tournament_teams SET legacy_team_id=? WHERE id=?",(legacy_b,b["id"]))
        legacy_match=self.db.execute("INSERT INTO matches(round,team_a,team_b,status) VALUES(?,?,?,'open')",(m["stage"],legacy_a,legacy_b)).lastrowid
        self.db.execute("UPDATE cc_bracket_matches SET legacy_match_id=? WHERE match_key=?",(legacy_match,key))
        self.db.executemany("INSERT INTO players(user_id,balance) VALUES(?,?)",((1000,900),(1001,1000)))
        # Bet has already debited 100 from user 1000; the payout is pending.
        self.db.execute("INSERT INTO bets(match_id,user_id,team_id,amount,settled,payout) VALUES(?,?,?,100,0,0)",(legacy_match,1000,legacy_a))
        self.db.commit()
        return legacy_match,legacy_a,legacy_b

    def test_legacy_bet_settles_at_1_5x_total_and_correction_recalculates(self):
        self.initialise()
        self.add_legacy_schema()
        mid,legacy_a,legacy_b=self.link_legacy_match("R1-L1")
        self.db.execute("UPDATE players SET balance=900 WHERE user_id=1000")
        match=tc.get_match(self.db,"R1-L1")
        tc.set_result(self.db,"R1-L1",match["team_a_id"])
        self.assertEqual(self.db.execute("SELECT balance FROM players WHERE user_id=1000").fetchone()[0],1050)
        bet=self.db.execute("SELECT * FROM bets WHERE match_id=? AND user_id=1000",(mid,)).fetchone()
        self.assertEqual((bet["settled"],bet["payout"]),(1,150))
        self.assertEqual(self.db.execute("SELECT status,winner FROM matches WHERE id=?",(mid,)).fetchone()["status"],"done")
        tc.set_result(self.db,"R1-L1",match["team_b_id"])
        self.assertEqual(self.db.execute("SELECT balance FROM players WHERE user_id=1000").fetchone()[0],900)
        self.assertEqual(self.db.execute("SELECT payout FROM bets WHERE match_id=? AND user_id=1000",(mid,)).fetchone()[0],0)

    def test_undoing_bridged_result_refunds_stake_and_cancels_old_match(self):
        self.initialise()
        self.add_legacy_schema()
        mid,legacy_a,_=self.link_legacy_match("R1-L1")
        m=tc.get_match(self.db,"R1-L1")
        tc.set_result(self.db,"R1-L1",m["team_a_id"])
        tc.set_result(self.db,"R1-L1",None)
        self.assertEqual(self.db.execute("SELECT balance FROM players WHERE user_id=1000").fetchone()[0],1000)
        legacy=self.db.execute("SELECT status FROM matches WHERE id=?",(mid,)).fetchone()
        self.assertEqual(legacy["status"],"cancelled")
        self.assertIsNone(tc.get_match(self.db,"R1-L1")["legacy_match_id"])

    def test_downstream_betting_match_is_refunded_when_upstream_winner_changes(self):
        self.initialise()
        self.add_legacy_schema()
        # Determine both quarterfinal contestants first.
        for key in ("R1-L1","R1-L2"):
            m=tc.get_match(self.db,key)
            tc.set_result(self.db,key,m["team_a_id"])
        qf=tc.get_match(self.db,"QF-L1")
        a=tc.team(self.db,qf["team_a_id"]);b=tc.team(self.db,qf["team_b_id"])
        la=self.db.execute("INSERT INTO teams(name,p1,p2) VALUES(?,?,?)",(a["name"],301,302)).lastrowid
        lb=self.db.execute("INSERT INTO teams(name,p1,p2) VALUES(?,?,?)",(b["name"],303,304)).lastrowid
        self.db.execute("UPDATE cc_tournament_teams SET legacy_team_id=? WHERE id=?",(la,a["id"]))
        self.db.execute("UPDATE cc_tournament_teams SET legacy_team_id=? WHERE id=?",(lb,b["id"]))
        legacy_mid=self.db.execute("INSERT INTO matches(round,team_a,team_b,status) VALUES('QF',?,?,'open')",(la,lb)).lastrowid
        self.db.execute("UPDATE cc_bracket_matches SET legacy_match_id=? WHERE match_key='QF-L1'",(legacy_mid,))
        self.db.execute("INSERT INTO players(user_id,balance) VALUES(500,900)")
        self.db.execute("INSERT INTO bets(match_id,user_id,team_id,amount,settled,payout) VALUES(?,500,?,100,0,0)",(legacy_mid,la))
        self.db.commit()
        upstream=tc.get_match(self.db,"R1-L1")
        with self.assertRaises(tc.DownstreamResultsExist):
            tc.set_result(self.db,"R1-L1",upstream["team_b_id"])
        tc.set_result(self.db,"R1-L1",upstream["team_b_id"],confirm=True)
        self.assertEqual(self.db.execute("SELECT balance FROM players WHERE user_id=500").fetchone()[0],1000)
        self.assertEqual(self.db.execute("SELECT status FROM matches WHERE id=?",(legacy_mid,)).fetchone()[0],"cancelled")
        self.assertIsNone(tc.get_match(self.db,"QF-L1")["legacy_match_id"])

    def test_draft_override_preserves_step_order_and_unique_modes(self):
        self.initialise()
        tc.draft_add(self.db, "R1-L1", tc.MODES[0])
        tc.draft_add(self.db, "R1-L1", tc.MODES[1])
        state = tc.draft_override(self.db, "R1-L1", 1, tc.MODES[2], 77)
        self.assertEqual(state["selections"][0]["gamemode"], tc.MODES[2])
        self.assertEqual(state["selections"][0]["action"], "ban")
        with self.assertRaises(ValueError):
            tc.draft_override(self.db, "R1-L1", 2, tc.MODES[2], 77)

    def test_stats_and_giveaway_eligibility_follow_bracket_results(self):
        self.initialise()
        first = tc.get_match(self.db, "R1-L1")
        losing_team = first["team_a_id"]
        tc.set_result(self.db, "R1-L1", first["team_b_id"], result_type="forfeit",
                      forfeit_team=losing_team)
        for key, *_ in tc.DEFS:
            if key == "R1-L1":
                continue
            match = tc.get_match(self.db, key)
            tc.set_result(self.db, key, match["team_a_id"])
        team_rows = tc.team_stats(self.db)
        champion = next(x for x in team_rows if x["placement"] == 1)
        runner_up = next(x for x in team_rows if x["placement"] == 2)
        self.assertEqual(tc.get_match(self.db, "GF")["winner_team_id"], champion["id"])
        self.assertGreaterEqual(champion["matches_played"], 4)
        self.assertEqual(len(tc.player_stats(self.db)), 32)
        eligible = tc.giveaway_eligibility(self.db)
        excluded = [x for x in eligible if x["team"] == tc.name(self.db, losing_team)]
        self.assertEqual(len(excluded), 2)
        self.assertTrue(all(x["status"] == "ineligible" for x in excluded))
        self.assertFalse(any(x["status"] == "pending" for x in eligible))
        self.assertEqual(sum(x["status"] == "eligible" for x in eligible), 30)

    def test_open_dispute_blocks_result_until_resolved(self):
        self.initialise()
        match = tc.get_match(self.db, "R1-L1")
        dispute_id = tc.open_dispute(self.db, "R1-L1", 100, "Possible rule violation", "open")
        with self.assertRaises(ValueError):
            tc.set_result(self.db, "R1-L1", match["team_a_id"])
        record = tc.resolve_dispute(self.db, "R1-L1", 200, "Reviewed evidence; resume match")
        self.assertEqual(record["id"], dispute_id)
        tc.set_result(self.db, "R1-L1", match["team_a_id"])
        self.assertEqual(tc.dispute_history(self.db, "R1-L1")[0]["status"], "resolved")

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

    def test_import_same_roster_is_idempotent_before_bracket_launch(self):
        self.assertEqual(tc.seed_initial_teams(self.db), 16)
        rows = [{"name": n, "player1": a, "player2": b} for n, a, b in tc.INITIAL]
        self.assertEqual(tc.import_teams(self.db, rows), 0)
        tc.initialise_bracket(self.db)
        with self.assertRaises(ValueError):
            tc.import_teams(self.db, rows)


if __name__ == "__main__":
    unittest.main()
