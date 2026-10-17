"""Storage layer: the Postgres adapter, run keys, concurrent agents, embeddings, copy.

Runs on SQLite by default; set FFELAB_TEST_PG to run the same tests on Postgres
(see tests/store.py). The pgvector index test only runs on Postgres.
"""
import re
import threading
import unittest

from ffelab import db as dbmod
from ffelab import embed
from ffelab.ledger import AlreadyResolved, Ledger, RunKeyExists
from store import PG_URL, fresh_db, shared_store


def seed(con, asm: dict[int, str], names: dict[int, str] | None = None):
    names = names or {}
    con.executemany("INSERT INTO functions (num, pos, ord, name, n_ins, has_frame, aligned4, asm_text)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    [(f, 0x1000 + 16 * f, f, names.get(f), len(a.splitlines()), 1, 1, a) for f, a in asm.items()])
    con.commit()
    return con


LOOP = "push ebp\nmov ebp,esp\nmov ecx,[DATA_000010]\n.l1:\nadd eax,[esi+4]\nloop .l1\npop ebp\nret"
CALLS = "push ebp\nmov ebp,esp\ncall FUNC_000002\ncall FUNC_000003\ncall FUNC_000004\npop ebp\nret"
FLOAT = "fld dword [ebp+8]\nfmul st0,st1\nfstp dword [ebp-4]\nfld dword [ebp+12]\nfsqrt\nret"


class TestTranslate(unittest.TestCase):
    def test_placeholders_and_literals(self):
        pytest_skip_without_psycopg(self)
        from ffelab.pg import translate
        self.assertEqual(translate("SELECT * FROM t WHERE a=? AND b LIKE ? || '/%'", True),
                         "SELECT * FROM t WHERE a=%s AND b LIKE %s || '/%%'")
        self.assertEqual(translate("SELECT 'it''s ?' , ?", True), "SELECT 'it''s ?' , %s")
        self.assertEqual(translate("UPDATE bets SET resolved=CURRENT_TIMESTAMP", False),
                         "UPDATE bets SET resolved=ffelab_now()")

    def test_schema_renders_for_postgres(self):
        s = dbmod.pg_schema()
        self.assertNotIn("AUTOINCREMENT", s)
        self.assertNotRegex(s, r"\bREAL\b")             # 4-byte in Postgres: would round probabilities
        self.assertIn("vec vector,", s)
        self.assertIn("vec BYTEA,", dbmod.pg_schema(vector=False))


def pytest_skip_without_psycopg(tc):
    try:
        import psycopg  # noqa: F401
    except ImportError:
        tc.skipTest("psycopg not installed")


class TestRunKey(unittest.TestCase):
    def test_retry_returns_the_same_experiment(self):
        L = Ledger(fresh_db())
        a = L.record_experiment("halt", "docked", {"func": 1}, run_key="wf-7:step-3")
        b = L.record_experiment("halt", "docked", {"func": 1}, run_key="wf-7:step-3")
        self.assertEqual(a, b)
        self.assertEqual(L.experiment_for_run("wf-7:step-3").id, a)
        self.assertIsNone(L.experiment_for_run("wf-7:step-4"))
        c, d = L.record_experiment("halt", "docked"), L.record_experiment("halt", "docked")   # no key: always new
        self.assertEqual(len({a, c, d}), 3)
        n = L.con.execute("SELECT COUNT(*) AS n FROM experiments").fetchone()["n"]
        self.assertEqual(n, 3)

    def test_timestamps_look_the_same_on_both_backends(self):
        L = Ledger(fresh_db())
        e = L.record_experiment("halt", "s")
        created = L.con.execute("SELECT created FROM experiments WHERE id=?", (e,)).fetchone()["created"]
        self.assertRegex(created, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")


class TestAtomicSteps(unittest.TestCase):
    """A campaign step whose timed-out attempt (a "zombie") may still be running."""

    def step(self, L, key, fail_after_record=False):
        with L.atomic():
            e = L.record_experiment("logging_pass", "s", {"funcs": [1]}, run_key=key, if_exists="raise")
            L.update(1, {"render": 3.0}, "halt", "log_hit", experiment_id=e)
            if fail_after_record:
                raise ConnectionError("emulator died mid-step")
            L.update(1, {"render": 2.0}, "halt", "log_hit", experiment_id=e)
        return e

    def counts(self, L):
        return (L.con.execute("SELECT COUNT(*) AS n FROM experiments").fetchone()["n"],
                L.con.execute("SELECT COUNT(*) AS n FROM evidence").fetchone()["n"])

    def test_loser_writes_nothing(self):
        L = Ledger(seed(fresh_db(), {1: "ret"}))
        e = self.step(L, "wf/1")
        before = (L.belief(1), self.counts(L))
        with self.assertRaises(RunKeyExists):
            self.step(L, "wf/1")
        self.assertEqual((L.belief(1), self.counts(L)), before)
        self.assertEqual(L.experiment_for_run("wf/1").id, e)
        self.assertEqual(L.record_experiment("halt", "s", run_key="wf/1"), e)   # default mode still returns it

    def test_crash_mid_step_leaves_no_half_experiment(self):
        L = Ledger(seed(fresh_db(), {1: "ret"}))
        with self.assertRaises(ConnectionError):
            self.step(L, "wf/2", fail_after_record=True)
        self.assertEqual(self.counts(L), (0, 0))
        self.assertIsNone(L.experiment_for_run("wf/2"))
        self.step(L, "wf/2")                             # the retry records it cleanly
        self.assertEqual(self.counts(L), (1, 2))

    def test_racing_attempts_one_wins(self):
        connect = shared_store()
        seed(connect(), {1: "ret"})
        start, results = threading.Barrier(4), []

        def attempt():
            L = Ledger(connect())
            start.wait()
            try:
                results.append(self.step(L, "wf/3"))
            except RunKeyExists:
                results.append("lost")

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(results.count("lost"), 3)
        self.assertEqual(self.counts(Ledger(connect())), (1, 2))


class TestConcurrentAgents(unittest.TestCase):
    """Several agents, each with its own connection, writing the same ledger."""

    def test_no_lost_belief_updates(self):
        connect = shared_store()
        seed(connect(), {1: "ret"})
        likes = [{"render": 2.0}, {"ai": 1.5}, {"render": 1.2}, {"math": 0.5}] * 3
        errors = []
        start = threading.Barrier(len(likes))            # connect first, then all update at once

        def agent(like):
            try:
                L = Ledger(connect())
                start.wait()
                L.update(1, like, "ai:t", "halt")
                L.con.close()
            except Exception as e:                       # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=agent, args=(lk,)) for lk in likes]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(errors, [])
        L = Ledger(connect())
        stored = L.belief(1)
        self.assertEqual(len(L.evidence(1)), len(likes))
        rebuilt = L.rebuild(1)                           # the belief every row implies
        for lab in L.labels:
            self.assertAlmostEqual(stored[lab], rebuilt[lab], places=9)

    def test_a_bet_resolves_once(self):
        connect = shared_store()
        seed(connect(), {1: "ret"})
        L = Ledger(connect())
        bet = L.place_bet("ai:x", 1, "c", "p", 0.7)
        exp = L.record_experiment("logging_pass", "s", {"funcs": [1]})
        results = []

        def agent(outcome):
            try:
                results.append(Ledger(connect()).resolve_bet(bet, outcome, exp))
            except AlreadyResolved:
                results.append("already")

        threads = [threading.Thread(target=agent, args=(i % 2 == 0,)) for i in range(6)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(results.count("already"), 5)


class TestEmbeddings(unittest.TestCase):
    def setUp(self):
        self.con = seed(fresh_db(), {1: LOOP, 2: LOOP.replace("DATA_000010", "DATA_000999"),
                                     3: CALLS, 4: FLOAT, 5: LOOP, 6: ""},
                        names={5: "SumShipMasses"})
        self.n = embed.build(self.con)

    def test_like_finds_like(self):
        self.assertEqual(self.n, 5)                     # FUNC 6 has no code, so no vector
        top = embed.similar(self.con, 1, k=3)
        self.assertEqual({r["func"] for r in top[:2]}, {2, 5})
        self.assertAlmostEqual(top[0]["score"], 1.0, places=3)   # same code up to addresses
        self.assertLess(top[2]["score"], 0.5)
        self.assertEqual(set(top[0]), {"func", "score"})        # nothing a blind labeller shouldn't see

    def test_names_do_not_change_vectors(self):
        a = embed.asm_vector(self.con, 1)
        self.con.execute("UPDATE functions SET asm_text=? WHERE num=1",
                         (LOOP.replace("DATA_000010", "_DATA_ShipTable"),))
        self.assertAlmostEqual(float(a @ embed.asm_vector(self.con, 1)), 1.0, places=5)

    def test_rebuild_is_an_upsert(self):
        self.assertEqual(embed.build(self.con), 5)
        n = self.con.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
        self.assertEqual(n, 5)

    def test_fingerprint_model(self):
        for f, sc, hits in [(1, "docked", 50), (1, "combat", 0), (2, "docked", 48), (2, "combat", 1),
                            (3, "docked", 0), (3, "combat", 50)]:
            self.con.execute("INSERT INTO fingerprints (func, scenario, hits, frames, rate) VALUES (?,?,?,?,?)",
                             (f, sc, hits, 50, hits / 50))
        self.assertEqual(embed.build(self.con, "fp-scenario-v1"), 3)
        self.assertEqual(embed.similar(self.con, 1, "fp-scenario-v1", k=1)[0]["func"], 2)

    def test_errors(self):
        with self.assertRaises(ValueError):
            embed.similar(self.con, 1, "word2vec")
        with self.assertRaises(KeyError):
            embed.similar(self.con, 6)

    @unittest.skipUnless(PG_URL, "pgvector index only exists on Postgres")
    def test_postgres_search_uses_the_hnsw_index(self):
        if not embed.has_pgvector(self.con):
            self.skipTest("pgvector extension not installed on this server")
        self.con.execute("SET enable_seqscan = off")     # 5 rows: force the planner to show it can use it
        q = self.con.execute("SELECT vec FROM embeddings WHERE func=1").fetchone()["vec"]
        plan = "\n".join(r[0] for r in self.con.execute(
            "EXPLAIN SELECT func FROM embeddings WHERE model = 'asm-ngram-v1'"
            " ORDER BY vec::vector(256) <=> ?::vector(256) LIMIT 3", (str(q),)))
        self.assertIn("embeddings_hnsw_asm_ngram_v1", plan)


class TestCopy(unittest.TestCase):
    def test_sqlite_to_other_store_keeps_ids(self):
        src = seed(dbmod.connect(":memory:"), {1: LOOP, 2: CALLS})
        L = Ledger(src)
        L.call(1, "render/planet", 0.8)
        L.place_bet("ai:x", 2, "c", "p", 0.6, spec={"scenario": "s", "func": 2, "expect": "hit"})
        e = L.record_experiment("logging_pass", "s", {"funcs": [2]}, run_key="wf:1")
        L.update(2, {"ai": 3.0}, "halt", "log_hit", experiment_id=e)
        L.auto_resolve(e, "s", {2: 5}, 10)
        embed.build(src)
        dst = fresh_db()
        counts = dbmod.copy(src, dst)
        self.assertEqual(counts["functions"], 2)
        self.assertNotIn("embeddings", counts)
        for t in ("bets", "evidence", "experiments", "beliefs", "vocabulary", "func_labels"):
            cols = ", ".join(f'"{c}"' for c in dbmod.columns(src, t))
            a = [tuple(r) for r in src.execute(f"SELECT {cols} FROM {t} ORDER BY 1, 2")]
            b = [tuple(r[c] for c in dbmod.columns(src, t)) for r in dst.execute(f"SELECT {cols} FROM {t} ORDER BY 1, 2")]
            self.assertEqual(a, b, t)
        D = Ledger(dst)
        self.assertEqual(D.experiment_for_run("wf:1").id, e)
        self.assertTrue(D.get_bet(2).outcome)
        self.assertGreater(D.place_bet("ai:y", 1, "c", "p", 0.5), 2)   # ids continue after the copied ones
        with self.assertRaises(ValueError):
            dbmod.copy(src, dst)                         # never merges into a store that has data


if __name__ == "__main__":
    unittest.main()
