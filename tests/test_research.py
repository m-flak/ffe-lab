import json
import os
import tempfile
import unittest
from pathlib import Path

from ffelab import research as rs
from ffelab.ledger import Ledger

from store import fresh_db


def make_ledger():
    con = fresh_db()
    con.executemany(
        "INSERT INTO functions (num,pos,ord,name,prefix,n_ins,has_frame,aligned4,filler_tail,asm_text)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(1, 0x1000, 0, "DPal16toPal", "DPal", 3, 1, 1, 0, "push ebp\nret"),
         (2, 0x1010, 1, None, None, 3, 1, 1, 0, "push ebp\nmov esi,DATA_009252\ncall FUNC_000001\nret"),
         (3, 0x1020, 2, None, None, 3, 1, 1, 0, "mov al,[DATA_009252+eax]\nret")])
    con.executemany("INSERT INTO calls VALUES (?,?,?)", [(2, 1, "direct")])
    con.executemany("INSERT INTO data_refs VALUES (?,?)", [(2, 9252), (3, 9252)])
    con.commit()
    return Ledger(con)


class TestRead(unittest.TestCase):
    def test_view_shows_names_and_refs(self):
        v = rs.view(make_ledger(), 2)
        self.assertEqual(v["callees"], ["FUNC_000001_DPal16toPal"])
        self.assertEqual(v["data_refs"], ["DATA_009252"])
        self.assertIn("DATA_009252", v["asm"])

    def test_refs_and_grep(self):
        led = make_ledger()
        self.assertEqual(rs.refs(led.con, rs.data_num("DATA_009252")), ["FUNC_000002", "FUNC_000003"])
        self.assertEqual([g["func"] for g in rs.grep(led.con, r"\[DATA_009252")], ["FUNC_000003"])

    def test_label_parsing(self):
        self.assertEqual(rs.func_num("FUNC_001805_Name"), 1805)
        self.assertEqual(rs.data_num("_DATA_009252"), 9252)


class TestClaims(unittest.TestCase):
    def test_claim_is_an_open_bet_with_its_basis(self):
        led = make_ledger()
        rs.claim(led, 2, "render/texture/palette", 0.8, "ai:muse", "fills DATA_009252")
        bet = led.con.execute("SELECT * FROM bets WHERE func=2").fetchone()
        self.assertEqual((bet["source"], bet["kind"], bet["outcome"]), ("ai:muse", "claim", None))
        ev = led.con.execute("SELECT detail FROM evidence WHERE func=2").fetchone()
        self.assertIn("fills DATA_009252", ev["detail"])
        self.assertGreater(led.belief(2)["render"], 0.5)

    def test_refusals(self):
        led = make_ledger()
        with self.assertRaises(ValueError):
            rs.claim(led, 2, "render", 0.8, "ai:muse", "  ")            # no basis
        with self.assertRaises(ValueError):
            rs.claim(led, 2, "render", 0.8, "muse", "x")                # not ai:<name>
        with self.assertRaises(ValueError):
            rs.claim(led, 2, "render", 0.8, "human", "x")
        with self.assertRaises(KeyError):
            rs.claim(led, 99, "render", 0.8, "ai:muse", "x")
        rs.claim(led, 2, "render", 0.8, "ai:muse", "x")
        with self.assertRaises(ValueError):                             # no stacking
            rs.claim(led, 2, "render/texture", 0.9, "ai:muse", "y")
        self.assertEqual(led.con.execute("SELECT COUNT(*) AS n FROM bets").fetchone()["n"], 1)

    def test_import_is_all_or_nothing(self):
        led = make_ledger()
        d = Path(tempfile.mkdtemp())
        bad = d / "bad.jsonl"
        bad.write_text(json.dumps({"func": "FUNC_000002", "label": "render", "confidence": 0.7, "basis": "a"}) + "\n"
                       + json.dumps({"func": 3, "label": "render", "confidence": 0.7, "basis": ""}) + "\n")
        with self.assertRaises(ValueError):
            rs.import_claims(led, bad, "ai:muse")
        self.assertEqual(led.con.execute("SELECT COUNT(*) AS n FROM bets").fetchone()["n"], 0)
        good = d / "good.jsonl"
        good.write_text(json.dumps({"func": 2, "label": "render", "confidence": 0.7, "basis": "a"}) + "\n\n"
                        + json.dumps({"func": 3, "label": "render", "confidence": 0.6, "basis": "b"}) + "\n")
        self.assertEqual(len(rs.import_claims(led, good, "ai:muse")), 2)

    def test_retag_moves_open_work_only(self):
        led = make_ledger()
        rs.claim(led, 2, "render/texture/palette", 0.85, "ai:lut-analysis", "fills the LUT")
        n = rs.retag(led.con, "ai:lut-analysis", "ai:muse")
        self.assertEqual((n["bets"], n["evidence"]), (1, 1))
        self.assertEqual(led.con.execute("SELECT source FROM bets").fetchone()["source"], "ai:muse")
        led.con.execute("UPDATE bets SET outcome=1, brier=0.02")
        led.con.commit()
        with self.assertRaises(ValueError):
            rs.retag(led.con, "ai:muse", "ai:other")


if __name__ == "__main__":
    unittest.main()


class TestResearchMCP(unittest.TestCase):
    def test_toolset_is_read_plus_claim(self):
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("mcp not installed")
        import asyncio
        import importlib
        d = tempfile.mkdtemp()
        os.environ.update(FFELAB_DB=os.path.join(d, "r.db"), FFELAB_SOURCE="ai:muse")
        try:
            srv = importlib.import_module("ffelab.mcp_research")
        finally:
            del os.environ["FFELAB_SOURCE"]
        names = {t.name for t in asyncio.run(srv.mcp.list_tools())}
        self.assertIn("claim", names)
        self.assertFalse(names & {"place_bet", "resolve_bet", "settle_claims", "record_call",
                                  "run_logging_pass", "run_halt", "close_contradiction"})
        self.assertIn("refused", asyncio.run(srv.claim("1", "render", 0.7, "x")))     # no such function: a clean refusal
