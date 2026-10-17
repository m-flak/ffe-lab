import json
import time
import unittest

from ffelab import adversary as adv
from ffelab import db as dbmod
from ffelab.harness.fake import FakeBackend
from ffelab.ledger import Ledger
from ffelab.loop import logging_pass


def setup():
    con = dbmod.connect(":memory:")
    con.executemany("INSERT INTO functions (num, pos, ord, name, n_ins, has_frame, aligned4, asm_text)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    [(i, 0x1000 + 16 * i, i - 1, f"Thing{i}", 10, 1, 1,
                      f"push ebp\nmov ebp,esp\ncall FUNC_00000{(i % 5) + 1}_SecretName\n"
                      f"mov eax,[_DATA_PlayerObject]\nret") for i in range(1, 6)])
    # 1 is called by 2,3,4 (high influence); 5 is a leaf nobody calls
    con.executemany("INSERT INTO calls VALUES (?,?,?)",
                    [(2, 1, "direct"), (3, 1, "direct"), (4, 1, "direct"), (4, 2, "direct")])
    con.execute("INSERT INTO segments VALUES (0, 0, 4, 1, 5, 'Secret', '[\"DATA_1\"]', NULL)")
    con.commit()
    return Ledger(con)


class TestNoSelfGrading(unittest.TestCase):
    def test_resolution_needs_experiment(self):
        L = setup()
        b = L.place_bet("human", 1, "c", "p", 0.9)
        with self.assertRaises(TypeError):
            L.resolve_bet(b, True)                               # no experiment id at all
        with self.assertRaises(ValueError):
            L.resolve_bet(b, True, 999)                          # experiment doesn't exist

    def test_experiment_must_test_that_function(self):
        L = setup()
        b = L.place_bet("human", 1, "c", "p", 0.9)
        exp = L.record_experiment("logging_pass", "combat", {"funcs": [5]})
        with self.assertRaises(ValueError):
            L.resolve_bet(b, True, exp)
        exp2 = L.record_experiment("logging_pass", "combat", {"funcs": [1]})
        self.assertAlmostEqual(L.resolve_bet(b, True, exp2), 0.01)
        with self.assertRaises(ValueError):
            L.resolve_bet(b, False, exp2)                        # can't resolve twice

    def test_old_experiment_cannot_resolve_new_bet(self):
        L = setup()
        exp = L.record_experiment("logging_pass", "combat", {"funcs": [1]})
        L.con.execute("UPDATE experiments SET created='2000-01-01 00:00:00' WHERE id=?", (exp,))
        b = L.place_bet("human", 1, "c", "p", 0.9)
        with self.assertRaises(ValueError):
            L.resolve_bet(b, True, exp)


class TestAutoResolveAndSettle(unittest.TestCase):
    def test_spec_bets_resolve_from_a_run(self):
        L = setup()
        hit = L.place_bet("ai:x", 1, "c", "fires in combat", 0.8, spec={"scenario": "combat", "func": 1, "expect": "hit"})
        miss = L.place_bet("ai:x", 5, "c", "silent in combat", 0.8, spec={"scenario": "combat", "func": 5, "expect": "hit"})
        fb = FakeBackend({"combat": {1: 1.0}}, context={"combat": ["combat"]})
        res = logging_pass(fb, L, "combat", [1, 5], ["combat"])
        outcomes = {r["bet"]: r["outcome"] for r in res["resolved"]}
        self.assertEqual(outcomes, {hit: True, miss: False})
        row = L.con.execute("SELECT experiment_id FROM bets WHERE id=?", (hit,)).fetchone()
        self.assertEqual(row["experiment_id"], res["experiment"])

    def test_claims_settle_on_experiments_not_opinions(self):
        L = setup()
        L.call(1, "ui", 0.95, source="human")
        L.call(1, "ui", 0.95, source="ai:x")                     # piling on opinions settles nothing
        self.assertEqual(L.settle_claims(), [])
        exp = L.record_experiment("logging_pass", "combat", {"funcs": [1]})
        for _ in range(6):
            L.update(1, {"physics": 4.0}, "halt", "log_hit", experiment_id=exp)
        settled = L.settle_claims()
        self.assertTrue(settled)
        self.assertTrue(all(s["outcome"] is False and s["settled_as"] == "physics" for s in settled))
        self.assertLess(L.reliability("human"), 0.9)


class TestContradictions(unittest.TestCase):
    def test_experiments_against_a_confident_claim_are_flagged(self):
        L = setup()
        L.call(1, "economy", 0.9, source="human")
        self.assertEqual(L.contradictions(), [])
        exp = L.record_experiment("halt", "combat", {"func": 1})
        for _ in range(3):
            L.update(1, {"ai": 3.0, "physics": 2.0}, "halt", "halt", experiment_id=exp)
        c = L.contradictions()
        self.assertEqual(len(c), 1)
        self.assertEqual((c[0]["claimed"], c[0]["favoured"]), ("economy", "ai"))
        with self.assertRaises(ValueError):
            L.commit_name(1, "economy_Thing", 0.95)              # can't name over a contradiction
        L.close_contradiction(c[0]["id"], "claim_withdrawn")
        self.assertEqual(L.contradictions(), [])

    def test_late_claim_against_existing_evidence_is_flagged(self):
        L = setup()
        exp = L.record_experiment("halt", "docked", {"func": 2})
        for _ in range(3):
            L.update(2, {"economy": 3.0}, "halt", "halt", experiment_id=exp)
        L.call(2, "render", 0.9, source="human")
        self.assertEqual(L.contradictions()[0]["claimed"], "render")


class TestCalibration(unittest.TestCase):
    def test_overconfidence_and_curve(self):
        L = setup()
        exp = L.record_experiment("logging_pass", "s", {"funcs": [1]})
        for i in range(10):
            L.resolve_bet(L.place_bet("human", 1, "c", "p", 0.9, domain="render"), i < 6, exp)
        cal = L.calibration("human")
        self.assertAlmostEqual(cal["overconfidence"], 0.3)
        self.assertEqual(cal["curve"][-1]["n"], 10)
        self.assertAlmostEqual(cal["curve"][-1]["was_right"], 0.6)
        self.assertIn("render", cal["by_domain"])


class TestRedTeam(unittest.TestCase):
    def test_targets_prefer_influential_untested_claims(self):
        L = setup()
        L.call(1, "physics", 0.9, source="human")               # influential
        L.call(5, "physics", 0.9, source="human")               # leaf
        t = adv.attack_targets(L)
        self.assertEqual(t[0]["func"], 1)
        self.assertGreater(t[0]["influence"], t[1]["influence"])
        self.assertNotEqual(t[0]["rival"], "physics")
        self.assertIn(t[0]["test"]["claim_predicts"], ("hit", "miss"))

    def test_attack_resolves_and_scores_adversary(self):
        L = setup()
        L.call(1, "economy", 0.9, source="human")
        target = adv.attack_targets(L)[0]
        # economy predicts 'hit' when docked; the rival says it won't fire there
        a = adv.place_attack(L, target["claim_bet"], "docked", "physics", "miss", 0.7)
        fb = FakeBackend({"docked": {}}, context={"docked": ["docked"]})   # function never fires docked
        res = logging_pass(fb, L, "docked", [1], ["docked"])
        outcome = {r["bet"]: r["outcome"] for r in res["resolved"]}
        self.assertTrue(outcome[a["prediction_bet"]])
        rep = adv.adversary_report(L)
        self.assertEqual(rep["claims_attacked"], 1)
        self.assertEqual(rep["calibration"]["n"], 1)

    def test_attack_rejects_non_claims(self):
        L = setup()
        b = L.place_bet("human", 1, "c", "p", 0.9)
        with self.assertRaises(ValueError):
            adv.place_attack(L, b, "docked", "ai", "miss", 0.6)


class TestBlind(unittest.TestCase):
    def test_blind_view_hides_opinions_and_names(self):
        L = setup()
        L.call(1, "economy", 0.9, source="human")
        v = adv.blind_view(L, 1)
        blob = json.dumps(v)
        for leak in ("economy", "Thing1", "SecretName", "PlayerObject", "belief", "Secret\""):
            self.assertNotIn(leak, blob)
        self.assertEqual(v["callers"], [2, 3, 4])
        self.assertEqual(v["segment"]["size"], 5)

    def test_reveal_flags_disagreement_with_a_deciding_test(self):
        L = setup()
        L.call(1, "economy", 0.9, source="human")
        adv.record_blind(L, 1, "physics", 0.7, "ai:blind")
        r = adv.reveal(L, 1)
        self.assertFalse(r[0]["agrees"])
        self.assertIn("deciding_test", r[0])
        self.assertEqual(len(adv.disagreements(L)), 1)

    def test_reveal_agreement(self):
        L = setup()
        L.call(2, "ui", 0.8, source="human")
        adv.record_blind(L, 2, "ui", 0.6, "ai:blind")
        self.assertTrue(adv.reveal(L, 2)[0]["agrees"])
        self.assertEqual(adv.disagreements(L), [])


class TestMigration(unittest.TestCase):
    def test_v01_database_upgrades(self):
        import os, sqlite3, tempfile
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = sqlite3.connect(path)
        old.executescript("CREATE TABLE bets (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, domain TEXT,"
                          " func INTEGER, claim TEXT, prediction TEXT, prob REAL, outcome INTEGER, brier REAL,"
                          " created TEXT DEFAULT CURRENT_TIMESTAMP, resolved TEXT);")
        old.commit(); old.close()
        con = dbmod.connect(path)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(bets)")}
        self.assertTrue({"kind", "label", "spec", "experiment_id", "parent_id"} <= cols)


if __name__ == "__main__":
    unittest.main()
