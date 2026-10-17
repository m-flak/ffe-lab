import tempfile
import types
import unittest
from pathlib import Path

from ffelab import db as dbmod
from store import fresh_db
from ffelab.evals import dataset as ds
from ffelab.evals import runner
from ffelab.evals import solvers as sv
from ffelab.ledger import Ledger
from ffelab.models import BlindLabelVerdict, RedTeamVerdict

REPO = Path(__file__).resolve().parents[1]


def ledger():
    con = fresh_db()
    con.executemany("INSERT INTO functions (num, pos, ord, name, n_ins, has_frame, aligned4, asm_text)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    [(i, 0x1000 + 16 * i, i - 1, f"FUNC_{i}_Secret", 5, 1, 1,
                      f"push ebp\ncall FUNC_00000{i}_Secret\nret") for i in range(1, 5)])
    con.commit()
    return Ledger(con)


class FakeClient:
    """Stands in for anthropic.Anthropic(): returns canned structured outputs."""

    def __init__(self, answer, stop_reason="end_turn"):
        self.calls = []
        self.answer, self.stop_reason = answer, stop_reason
        self.messages = types.SimpleNamespace(parse=self.parse)

    def parse(self, **kw):
        self.calls.append(kw)
        usage = types.SimpleNamespace(input_tokens=1000, output_tokens=100,
                                      cache_read_input_tokens=0, cache_creation_input_tokens=0)
        return types.SimpleNamespace(parsed_output=self.answer(kw), stop_reason=self.stop_reason, usage=usage)


class TestExportFollowsLedgerRules(unittest.TestCase):
    def test_only_experiment_settled_labels_become_gold(self):
        L = ledger()
        L.call(1, "ui", 0.99, source="human")                 # opinions alone never make a case
        L.call(1, "ui", 0.99, source="ai:x")
        exp = L.record_experiment("logging_pass", "combat", {"funcs": [2, 3]})
        for _ in range(6):
            L.update(2, {"physics": 4.0}, "halt", "log_hit", experiment_id=exp)
        L.update(3, {"physics": 4.0}, "halt", "log_hit", experiment_id=exp)  # too little evidence
        cases = ds.export_blind(L)
        self.assertEqual([c.func for c in cases], [2])
        self.assertEqual(cases[0].gold, "physics")
        self.assertEqual(cases[0].experiment_ids, [exp])
        self.assertNotIn("Secret", cases[0].view["asm"])          # names stay hidden

    def test_settled_claims_become_red_team_cases_without_leaking_the_answer(self):
        L = ledger()
        L.call(2, "ui", 0.9, source="human")
        exp = L.record_experiment("logging_pass", "combat", {"funcs": [2]})
        for _ in range(6):
            L.update(2, {"physics": 4.0}, "halt", "log_hit", experiment_id=exp)
        L.settle_claims()
        cases = ds.export_red_team(L)
        self.assertEqual(len(cases), 1)
        c = cases[0]
        self.assertTrue(c.claim_wrong)
        self.assertEqual(c.claim.label, "ui")
        self.assertEqual(c.claim.suggested_test, ds.pre_experiment_test("ui", L.labels))

    def test_split_is_stable(self):
        self.assertEqual(ds.split_for("func-688"), ds.split_for("func-688"))
        splits = [ds.split_for(f"func-{i}") for i in range(1000)]
        self.assertTrue(0.25 < splits.count("test") / 1000 < 0.35)


class TestSmokeDatasets(unittest.TestCase):
    def test_load_and_heuristic_gate(self):
        for name, base in (("smoke_blind", "heuristic-blind"), ("smoke_redteam", "heuristic-redteam")):
            path = REPO / "evals" / "datasets" / f"{name}.jsonl"
            cases = ds.load(path)
            self.assertTrue(all(c.provenance == "synthetic" for c in cases))
            rep = runner.run(cases, sv.HeuristicSolver(cases[0].task), str(path.relative_to(REPO)))
            b = runner.Baseline.model_validate_json((REPO / "evals" / "baselines" / f"{base}.json").read_text())
            self.assertEqual(runner.gate(rep, b), [])


class TestClaudeSolver(unittest.TestCase):
    def test_prompt_comes_from_the_agent_file_and_answers_are_scored(self):
        cases = ds.load(REPO / "evals" / "datasets" / "smoke_blind.jsonl")
        client = FakeClient(lambda kw: BlindLabelVerdict(label="render", confidence=0.9, reasoning="x"))
        s = sv.ClaudeSolver("blind_label", "claude-sonnet-5-5", client=client)
        rep = runner.run(cases, s, "smoke_blind.jsonl")
        kw = client.calls[0]
        self.assertIn("without knowing what anyone else thinks", kw["system"])
        self.assertNotIn("tools: mcp__", kw["system"])              # frontmatter stripped
        self.assertIs(kw["output_format"], BlindLabelVerdict)
        m = rep.metrics["all"]
        self.assertAlmostEqual(m.accuracy, round(1 / 14, 4))
        self.assertGreater(m.brier, 0.6)                             # confidently wrong 13 times
        self.assertGreater(m.overconfidence, 0.8)
        self.assertAlmostEqual(m.cost_usd, 14 * (1000 * 2 + 100 * 10) / 1e6)
        self.assertEqual(m.by_subsystem["render"]["accuracy"], 1.0)

    def test_prompt_change_changes_the_fingerprint(self):
        client = FakeClient(lambda kw: None)
        a = sv.ClaudeSolver("blind_label", client=client)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("---\nname: x\n---\nLabel things.\n")
        b = sv.ClaudeSolver("blind_label", prompt_path=f.name, client=client)
        self.assertNotEqual(a.prompt_sha, b.prompt_sha)

    def test_refusals_count_as_errors(self):
        cases = ds.load(REPO / "evals" / "datasets" / "smoke_redteam.jsonl")
        client = FakeClient(lambda kw: None, stop_reason="refusal")
        rep = runner.run(cases, sv.ClaudeSolver("red_team", client=client), "smoke_redteam.jsonl")
        self.assertEqual(rep.metrics["all"].errors, len(cases))
        self.assertEqual(rep.metrics["all"].accuracy, 0.0)


class TestGate(unittest.TestCase):
    def test_worse_prompt_fails_the_gate(self):
        path = REPO / "evals" / "datasets" / "smoke_redteam.jsonl"
        cases = ds.load(path)

        def good(kw):
            c = next(c for c in cases if f'"func": {c.func},' in kw["messages"][0]["content"])
            return RedTeamVerdict(p_claim_wrong=0.8 if c.claim_wrong else 0.2, attack=bool(c.claim_wrong),
                                  rival=c.gold if c.claim_wrong else None,
                                  rival_predicts="hit" if c.claim_wrong else None, reasoning="x")

        def lazy(kw):                                               # attacks everything with high confidence
            return RedTeamVerdict(p_claim_wrong=0.95, attack=True, rival="ui", rival_predicts="hit", reasoning="x")

        base = runner.baseline_from(runner.run(cases, sv.ClaudeSolver("red_team", client=FakeClient(good)),
                                               "smoke_redteam.jsonl"))
        self.assertEqual(base.solver_prefix, "claude")
        worse = runner.run(cases, sv.ClaudeSolver("red_team", client=FakeClient(lazy)), "smoke_redteam.jsonl")
        fails = runner.gate(worse, base)
        self.assertTrue(any(f.startswith("accuracy") for f in fails))
        self.assertTrue(any(f.startswith("brier") for f in fails))

    def test_replay_rescoring(self):
        path = REPO / "evals" / "datasets" / "smoke_blind.jsonl"
        cases = ds.load(path)
        rep = runner.run(cases, sv.HeuristicSolver("blind_label"), str(path))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(rep.model_dump_json())
        again = runner.run(cases, sv.ReplaySolver(f.name), str(path))
        self.assertEqual(again.metrics["all"].accuracy, rep.metrics["all"].accuracy)


if __name__ == "__main__":
    unittest.main()
