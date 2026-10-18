import json
import unittest

from pydantic import ValidationError

from ffelab import db as dbmod
from store import fresh_db
from ffelab import models as m
from ffelab.harness.fake import FakeBackend
from ffelab.ledger import DEFAULT_LABELS, Ledger
from ffelab.loop import logging_pass


def ledger():
    con = fresh_db()
    con.executemany("INSERT INTO functions (num, pos, ord, name, n_ins, has_frame, aligned4, asm_text)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    [(i, 0x1000 + 16 * i, i - 1, None, 5, 1, 1, "push ebp\nret") for i in range(1, 4)])
    con.commit()
    return Ledger(con)


class TestSchema(unittest.TestCase):
    def test_every_record_matches_its_table(self):
        """The models are the shared schema: a column added to one side only fails here."""
        con = fresh_db()
        for rec in m.RECORDS:
            cols = set(dbmod.columns(con, rec.table))
            self.assertEqual(cols, set(rec.model_fields), rec.__name__)

    def test_labels_have_one_source(self):
        self.assertEqual(list(m.TOP_LABELS), DEFAULT_LABELS)


class TestRowsRoundTrip(unittest.TestCase):
    def test_ledger_rows_validate(self):
        L = ledger()
        L.call(1, "render/planet", 0.8, source="human")
        b = L.place_bet("ai:x", 2, "c", "fires", 0.7, spec={"scenario": "combat", "func": 2, "expect": "hit"})
        fb = FakeBackend({"combat": {2: 1.0}}, context={"combat": ["combat"]})
        res = logging_pass(fb, L, "combat", [1, 2], ["combat"])
        bet = L.get_bet(b)
        self.assertTrue(bet.outcome)
        self.assertEqual(bet.spec.expect, "hit")
        self.assertEqual(bet.experiment_id, res["experiment"])
        exp = L.get_experiment(res["experiment"])
        self.assertEqual(exp.kind, "logging_pass")
        self.assertEqual(exp.detail["funcs"], [1, 2])
        kinds = {e.kind for e in L.evidence(1)}
        self.assertEqual(kinds, {"call", "log_hit"} if res["counts"][1] else {"call", "log_miss"})
        claim = next(m.Bet.from_row(r) for r in L.con.execute("SELECT * FROM bets WHERE kind='claim'"))
        self.assertEqual(claim.label, "render/planet")

    def test_halt_snapshot_round_trip(self):
        h = m.HaltSnapshot(func=3, scenario="docked", stack=[1, 2], context=["docked"])
        self.assertEqual(m.HaltSnapshot.from_json(h.to_json()), h)
        with self.assertRaises(ValidationError):
            h.hits = -1                                        # validated on assignment too


class TestRejectsBadAgentOutput(unittest.TestCase):
    def test_confidence_and_labels(self):
        L = ledger()
        with self.assertRaises(ValueError):
            L.call(1, "render", 1.3)                           # was silently accepted before
        with self.assertRaises(ValueError):
            L.call(1, "graphics", 0.8)
        with self.assertRaises(ValueError):
            m.BlindLabelVerdict(label="graphics", confidence=0.5, reasoning="")
        with self.assertRaises(ValueError):
            m.BlindLabelVerdict(label="render", confidence=0.5, reasoning="", extra="x")

    def test_bet_specs(self):
        L = ledger()
        with self.assertRaises(ValueError):
            L.place_bet("ai:x", 1, "c", "p", 0.5, spec={"scenario": "s", "func": 1})            # no criterion
        with self.assertRaises(ValueError):
            L.place_bet("ai:x", 1, "c", "p", 0.5, spec={"scenario": "s", "func": 1, "expect": "hit",
                                                        "min_rate": 0.5})                       # two criteria
        with self.assertRaises(ValueError):
            L.place_bet("ai:x", 1, "c", "p", 0.5, spec={"scenario": "s", "func": 1, "expect": "visible"})
        b = L.place_bet("ai:x", 1, "c", "p", 0.5, spec={"knockout": True, "scenario": "s", "func": 1,
                                                         "expect": "visible"})
        self.assertTrue(L.get_bet(b).spec.knockout)
        stored = L.con.execute("SELECT spec FROM bets WHERE id=?", (b,)).fetchone()["spec"]
        self.assertEqual(json.loads(stored)["expect"], "visible")

    def test_red_team_verdict_needs_a_rival_to_attack(self):
        with self.assertRaises(ValueError):
            m.RedTeamVerdict(p_claim_wrong=0.8, attack=True, reasoning="x")
        v = m.RedTeamVerdict(p_claim_wrong=0.2, attack=False, reasoning="x")
        self.assertIsNone(v.rival)

    def test_resolved_bet_must_cite_experiment(self):
        with self.assertRaises(ValueError):
            m.Bet(id=1, source="human", claim="c", prediction="p", prob=0.5, outcome=True)


if __name__ == "__main__":
    unittest.main()
