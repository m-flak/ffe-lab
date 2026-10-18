import unittest

from ffelab import embed, graph
from ffelab.ledger import Ledger
from ffelab.models import Evidence

from store import fresh_db

# Two groups that call inside themselves, joined by one bridge (6 -> 11).
# 3 <-> 4 is a cycle. Named functions carry an inherited family prefix:
# Draw* (render) in the first group, Int64* (math) in the second.
CALLS = [(1, 2), (1, 3), (2, 3), (3, 4), (4, 3), (4, 5), (2, 5), (5, 6), (1, 6), (2, 6),
         (6, 11),
         (11, 12), (11, 13), (12, 13), (13, 14), (12, 14), (14, 15), (13, 15), (15, 16), (11, 16)]
PREFIX = {1: "Draw", 3: "Draw", 5: "Draw", 6: "Draw", 12: "Int64", 14: "Int64", 15: "Int64", 16: "Int64"}
FUNCS = [1, 2, 3, 4, 5, 6, 11, 12, 13, 14, 15, 16, 20]          # 20 calls nothing and is never called


def make_store(calls=CALLS, prefix=PREFIX):
    con = fresh_db()
    con.executemany(
        "INSERT INTO functions (num,pos,ord,name,prefix,n_ins,has_frame,aligned4,filler_tail,asm_text)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(f, 0x1000 + 16 * i, i, prefix.get(f), prefix.get(f), 3, 1, 1, 0, "push ebp\nmov ebp,esp\nret")
         for i, f in enumerate(FUNCS)])
    con.executemany("INSERT INTO calls VALUES (?,?,?)", [(a, b, "direct") for a, b in calls])
    con.commit()
    return con


class TestLayers(unittest.TestCase):
    def test_heights_depths_and_cycles(self):
        rows = {r["func"]: r for r in graph.features(make_store())}
        self.assertEqual(rows[16]["height"], 0)                  # calls nothing
        self.assertEqual(rows[15]["height"], 1)
        self.assertEqual(rows[1]["depth"], 0)                    # nothing calls it
        self.assertEqual(rows[3]["cycle_size"], 2)
        self.assertEqual(rows[3]["cycle"], rows[4]["cycle"])     # the cycle is one node
        self.assertEqual(rows[3]["height"], rows[4]["height"])
        self.assertIsNone(rows[2]["cycle"])
        self.assertEqual((rows[6]["fan_in"], rows[6]["fan_out"]), (3, 1))
        self.assertGreater(rows[1]["height"], rows[11]["height"])  # the first group sits above the second
        self.assertIsNone(rows[20]["span"])

    def test_long_chain_without_recursion_limits(self):
        n = 5000
        succ = [[i + 1] for i in range(n - 1)] + [[0]]           # one big cycle
        comps = graph._sccs(succ)
        self.assertEqual(len(comps), 1)
        self.assertEqual(len(comps[0]), n)


class TestBasis(unittest.TestCase):
    def setUp(self):
        self.con = make_store()
        self.report = graph.build(self.con)

    def test_build(self):
        self.assertEqual(self.report["functions"], len(FUNCS))
        self.assertEqual(self.report["embedded"], len(FUNCS) - 1)  # the isolated one has no position
        self.assertEqual(self.report["largest_cycle"], 2)
        n = self.con.execute("SELECT COUNT(*) AS n FROM graph_features").fetchone()["n"]
        self.assertEqual(n, len(FUNCS))

    def test_neighbours_stay_in_their_group(self):
        group = {1, 2, 3, 4, 5, 6}
        near = [r["func"] for r in embed.similar(self.con, 2, graph.MODEL, k=3)]
        self.assertTrue(set(near) <= group, near)
        near = [r["func"] for r in embed.similar(self.con, 13, graph.MODEL, k=3)]
        self.assertTrue(set(near).isdisjoint(group), near)

    def test_search_is_blind_safe(self):
        for r in embed.similar(self.con, 2, graph.MODEL, k=5):
            self.assertEqual(set(r), {"func", "score"})

    def test_names_do_not_change_vectors(self):
        other = make_store(prefix={})
        graph.build(other)
        a = dict(embed.similar(self.con, 2, graph.MODEL, k=5)[0])
        b = dict(embed.similar(other, 2, graph.MODEL, k=5)[0])
        self.assertEqual(a, b)

    def test_rebuild_drops_functions_that_lost_their_calls(self):
        self.con.execute("DELETE FROM calls WHERE caller=16 OR callee=16")
        self.con.commit()
        graph.build(self.con)
        row = self.con.execute("SELECT 1 FROM embeddings WHERE func=16 AND model=?", (graph.MODEL,)).fetchone()
        self.assertIsNone(row)


class TestVotes(unittest.TestCase):
    def setUp(self):
        self.con = make_store()
        graph.build(self.con)
        self.led = Ledger(self.con)

    def test_quotient_allows_only_seen_label_pairs(self):
        labels = graph.known_labels(self.led)
        edges = graph._edges(self.con)
        q = graph.quotient(edges, labels)
        self.assertEqual(q[("render", "render")], 3)             # 1->3, 1->6, 5->6: direct calls only
        self.assertEqual(q[("render", "math")], 0)                # the bridge 6->11 lands on an unnamed function
        # 13 is called by math (12) and calls only math (14, 15): math is the only label that adds no new pair
        self.assertEqual(graph.allowed_labels(13, edges, labels, q, self.led.labels), {"math"})
        self.assertIsNone(graph.allowed_labels(20, edges, labels, q, self.led.labels))

    def test_votes_favour_the_group_label_and_stay_static(self):
        r = graph.vote_all(self.led)
        self.assertGreater(r["knn_votes"], 0)
        self.assertGreater(r["quotient_votes"], 0)
        b2, b13 = self.led.belief(2), self.led.belief(13)
        self.assertGreater(b2["render"], b2["math"])
        self.assertGreater(b13["math"], b13["render"])
        for f in (2, 13):
            _, n_exp = self.led.experimental_belief(f)
            self.assertEqual(n_exp, 0)                            # never counts as an experiment
            kinds = {e.kind for e in self.led.evidence(f)}     # and the records validate
            self.assertTrue(kinds <= {"static_graph_knn", "static_quotient"}, kinds)
            self.assertFalse(any(e.is_experimental for e in self.led.evidence(f)))
        self.assertEqual(self.led.contradictions(), [])

    def test_rerun_replaces_instead_of_stacking(self):
        graph.vote_all(self.led)
        first = self.led.belief(2)
        graph.vote_all(self.led)
        n = self.con.execute("SELECT COUNT(*) AS n FROM evidence WHERE func=2 AND kind='static_graph_knn'").fetchone()["n"]
        self.assertEqual(n, 1)
        for lab, p in self.led.belief(2).items():
            self.assertAlmostEqual(p, first[lab], places=9)

    def test_outliers(self):
        self.assertEqual(graph.outliers(self.led), [])           # the bridge lands on an unnamed function
        # an unsupported cross-label call: math calling render
        self.con.execute("INSERT INTO calls VALUES (16, 5, 'direct')")
        self.con.commit()
        found = graph.outliers(Ledger(self.con))
        self.assertIn({"caller": 16, "caller_label": "math", "callee": 5, "callee_label": "render"}, found)

    def test_check(self):
        r = graph.check(self.led)
        self.assertEqual(r["labelled"], len(PREFIX))
        self.assertGreaterEqual(r["knn_top_label_accuracy"], 0.75)
        self.assertGreaterEqual(r["quotient_contains_true_label"], 0.75)


class TestEvidenceModel(unittest.TestCase):
    def test_static_kinds_are_not_experimental(self):
        for kind in ("static_callee", "static_graph_knn", "static_quotient"):
            e = Evidence(id=1, func=1, source="static", kind=kind, likelihoods={"render": 1.0})
            self.assertFalse(e.is_experimental)
        self.assertTrue(Evidence(id=1, func=1, source="halt", kind="halt", likelihoods={}).is_experimental)


if __name__ == "__main__":
    unittest.main()
