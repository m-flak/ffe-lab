import asyncio
import json
import unittest
from unittest import mock

from ffelab import behavior
from ffelab import db as dbmod
from ffelab.harness.fake import FakeBackend
from ffelab.ledger import Ledger
from ffelab.models import BlindLabelVerdict
from ffelab.orchestrator import questions as qs
from ffelab.orchestrator import tracing
from ffelab.orchestrator.budget import Budget, BudgetTracker
from ffelab.orchestrator.orchestrator import Orchestrator
from ffelab.orchestrator.workers import (BlindTask, ClaudeWorker, HeuristicWorker, RedTeamPlan, RedTeamTask,
                                         WorkerResult, WorkerUsage, build_tools)

try:
    import claude_agent_sdk as sdk
except ImportError:                                   # the orchestrator extra isn't installed
    sdk = None

try:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
except ImportError:
    TracerProvider = None


def setup(n=6):
    con = dbmod.connect(":memory:")
    con.executemany("INSERT INTO functions (num, pos, ord, name, n_ins, has_frame, aligned4, asm_text)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    [(i, 0x1000 + 16 * i, i - 1, f"Thing{i}", 10, 1, 1,
                      f"push ebp\nmov ebp,esp\ncall FUNC_00000{(i % 5) + 1}_SecretName\nret")
                     for i in range(1, n + 1)])
    con.executemany("INSERT INTO calls VALUES (?,?,?)", [(2, 1, "direct"), (3, 1, "direct")])
    con.commit()
    L = Ledger(con)
    behavior.register_scenario(L, "docked", ["docked"])
    behavior.register_scenario(L, "combat", ["combat"])
    return L


class ScriptedWorker:
    """Answers from a script: blind labels by func, red-team plans by claim bet."""
    name = "scripted"
    model = None

    def __init__(self, blind=None, plans=None, cost=0.1, ask=None, fail=False):
        self.blind, self.plans, self.cost, self.ask, self.fail = blind or {}, plans or {}, cost, ask, fail
        self.calls = []

    async def run(self, task, max_turns, max_usd, parent=None):
        self.calls.append((task, max_turns, max_usd))
        res = WorkerResult(worker=self.name, kind=task.kind, task=task.id,
                           usage=WorkerUsage(input_tokens=1000, output_tokens=100, cost_usd=self.cost))
        if self.fail:
            res.error = "boom"
            return res
        if isinstance(task, BlindTask):
            lab, conf = self.blind.get(task.func, ("unknown", 0.2))
            res.output = BlindLabelVerdict(label=lab, confidence=conf, reasoning="scripted")
        else:
            res.output = self.plans.get(task.target["claim_bet"]) or RedTeamPlan(
                p_claim_wrong=0.2, attack=False, reasoning="looks right")
        if self.ask:
            res.questions.append(qs.QuestionDraft(question=self.ask, func=task.func))
        return res


def orch(L, worker, backend=None, **kw):
    budget = kw.pop("budget", Budget(max_rounds=3, max_usd=10))
    backend = backend or FakeBackend({"docked": {}, "combat": {1: 0.5}})
    return Orchestrator(L, backend, worker, budget, **kw)


class TestBudget(unittest.TestCase):
    def test_limits_and_worker_cap(self):
        t = [0.0]
        tr = BudgetTracker(Budget(max_usd=1.0, max_tokens=5000, max_seconds=60, worker_max_usd=0.5),
                           clock=lambda: t[0])
        self.assertIsNone(tr.exhausted())
        self.assertEqual(tr.worker_cap(), 0.5)
        tr.add(0.8, 100, 10)
        self.assertAlmostEqual(tr.worker_cap(4), 0.05)      # a share of what's left, not 0.5 each
        tr.add(0.3, 100, 10)
        self.assertEqual(tr.exhausted(), "budget_usd")
        tr2 = BudgetTracker(Budget(max_usd=None, max_tokens=500), clock=lambda: t[0])
        tr2.add(0, 400, 200)
        self.assertEqual(tr2.exhausted(), "budget_tokens")
        tr3 = BudgetTracker(Budget(max_seconds=60), clock=lambda: t[0])
        t[0] = 61
        self.assertEqual(tr3.exhausted(), "time")

    def test_run_stops_on_budget(self):
        L = setup(20)
        w = ScriptedWorker(cost=0.3)
        rep = asyncio.run(orch(L, w, budget=Budget(max_usd=1.0, max_rounds=10), blind_per_round=2,
                               concurrency=1).run())
        self.assertEqual(rep.stop, "budget_usd")
        self.assertGreaterEqual(rep.spend.usd, 1.0)
        self.assertLess(rep.spend.usd, 1.0 + 0.3 * 2)         # stops within one round's overshoot
        self.assertTrue(all(c[2] is not None and c[2] <= 0.5 for c in w.calls))


class TestRound(unittest.TestCase):
    def test_blind_labels_recorded_and_not_repeated(self):
        L = setup()
        w = ScriptedWorker(blind={1: ("render", 0.7)})
        o = orch(L, w, blind_per_round=3)
        r1 = asyncio.run(o.run_round(1))
        self.assertEqual(len(r1.blind_labels), 3)
        first = {b["func"] for b in r1.blind_labels}
        r2 = asyncio.run(o.run_round(2))
        self.assertFalse(first & {b["func"] for b in r2.blind_labels})
        rows = L.con.execute("SELECT * FROM blind_labels WHERE source='ai:blind'").fetchall()
        self.assertEqual(len(rows), 6)
        # a blind label enters the ledger as an opinion (claim), never as experiment evidence
        self.assertTrue(all(e.kind == "call" for f in first for e in L.evidence(f)))

    def test_attack_places_bets_before_the_run_and_the_run_resolves_them(self):
        L = setup()
        L.call(1, "render", 0.9, source="human")
        claim = L.con.execute("SELECT id FROM bets WHERE kind='claim'").fetchone()["id"]
        plan = RedTeamPlan(p_claim_wrong=0.7, attack=True, rival="ai", rival_predicts="hit",
                           scenario="combat", reasoning="it reads ship state")
        w = ScriptedWorker(plans={claim: plan})
        o = orch(L, w, blind_per_round=0)
        r = asyncio.run(o.run_round(1))
        self.assertEqual(len(r.attacks), 1)
        a = r.attacks[0]
        self.assertEqual(a.decision, "attacked")
        self.assertEqual(r.bets_resolved, 1)
        pred = L.con.execute("SELECT * FROM bets WHERE spec IS NOT NULL").fetchone()
        self.assertEqual(pred["source"], "adversary")
        self.assertEqual(pred["experiment_id"], a.experiment)
        self.assertEqual(pred["outcome"], 1)                   # FUNC 1 hits in combat (fake truth)
        exp = L.get_experiment(a.experiment)
        self.assertGreaterEqual(str(exp.created), str(L.get_bet(pred["id"]).created))
        # the same claim isn't attacked again while its rival bet is open
        r2 = asyncio.run(o.run_round(2))
        self.assertEqual(r2.attacks, [])

    def test_invalid_and_declined_attacks_run_nothing(self):
        L = setup()
        L.call(1, "render", 0.9, source="human")
        L.call(2, "math", 0.9, source="human")
        c1, c2 = [r["id"] for r in L.con.execute("SELECT id FROM bets WHERE kind='claim' ORDER BY id")]
        w = ScriptedWorker(plans={c1: RedTeamPlan(p_claim_wrong=0.7, attack=True, rival="ai", rival_predicts="hit",
                                                  scenario="no-such-save", reasoning="x")})
        r = asyncio.run(orch(L, w, blind_per_round=0).run_round(1))
        by = {a.claim_bet: a for a in r.attacks}
        self.assertEqual(by[c1].decision, "invalid")
        self.assertEqual(by[c2].decision, "declined")
        self.assertEqual(L.con.execute("SELECT COUNT(*) AS n FROM experiments").fetchone()["n"], 0)
        self.assertEqual(L.con.execute("SELECT COUNT(*) AS n FROM bets WHERE kind='rival'").fetchone()["n"], 0)

    def test_plan_validation(self):
        with self.assertRaises(ValueError):
            RedTeamPlan(p_claim_wrong=0.7, attack=True, rival="ai", rival_predicts="hit", reasoning="no scenario")

    def test_failed_workers_are_reported_and_stop_on_no_progress(self):
        L = setup()
        rep = asyncio.run(orch(L, ScriptedWorker(fail=True), budget=Budget(max_rounds=5, patience=2)).run())
        self.assertEqual(rep.stop, "no_progress")
        self.assertEqual(len(rep.rounds), 2)
        self.assertTrue(rep.rounds[0].worker_errors)
        self.assertEqual(rep.spend.failed_workers, rep.spend.workers)

    def test_nothing_to_do(self):
        L = setup(2)
        rep = asyncio.run(orch(L, ScriptedWorker(), blind_per_round=2).run())
        self.assertEqual(rep.stop, "nothing_to_do")


class TestQuestions(unittest.TestCase):
    def test_agent_questions_batch_and_stop(self):
        L = setup(10)
        w = ScriptedWorker(ask="Which save state shows the planet close up?")
        rep = asyncio.run(orch(L, w, budget=Budget(max_rounds=5, max_open_questions=3), blind_per_round=2).run())
        self.assertEqual(rep.stop, "questions_waiting")
        self.assertGreaterEqual(len(rep.open_questions), 3)
        # same question about the same function is queued once
        n = len(qs.open_questions(L.con))
        qs.ask_draft(L.con, "x", qs.QuestionDraft(question="Which save state shows the planet close up?", func=1))
        self.assertEqual(len(qs.open_questions(L.con)), n)

    def test_contradictions_are_queued_once_and_closed_by_answer(self):
        L = setup()
        qs.ensure_schema(L.con)
        L.call(1, "render", 0.9, source="human")
        exp = L.record_experiment("logging_pass", "combat", {"funcs": [1]})
        for _ in range(4):
            L.update(1, {lab: (5.0 if lab == "ai" else 0.2) for lab in L.labels}, "halt", "log_hit", "x",
                     experiment_id=exp)
        self.assertTrue(L.contradictions())
        added = qs.sync_contradictions(L)
        self.assertEqual(len(added), 1)
        self.assertEqual(qs.sync_contradictions(L), [])
        q = qs.get(L.con, added[0])
        self.assertEqual(q.kind, "contradiction")
        qs.answer(L, q.id, "the combat save was broken", close="explained")
        self.assertEqual(L.contradictions(), [])
        self.assertEqual(qs.get(L.con, q.id).status, "answered")

    def test_answer_with_claim_is_an_opinion(self):
        L = setup()
        qs.ensure_schema(L.con)
        qid = qs.ask_draft(L.con, "w", qs.QuestionDraft(question="what is this?", func=3))
        qs.answer(L, qid, "it draws the HUD", claim="ui/hud", confidence=0.8)
        bet = L.con.execute("SELECT * FROM bets WHERE func=3 AND kind='claim'").fetchone()
        self.assertEqual((bet["source"], bet["label"], bet["outcome"]), ("human", "ui/hud", None))
        with self.assertRaises(ValueError):
            qs.answer(L, qid, "x", close="explained")          # not a contradiction question


class TestHeuristicWorker(unittest.TestCase):
    def test_dry_run(self):
        L = setup(4)
        L.con.execute("UPDATE functions SET asm_text='push ebp\nint 0x21\nret' WHERE num=2")
        rep = asyncio.run(orch(L, HeuristicWorker(L), blind_per_round=4).run())
        labels = {b["func"]: b["label"] for b in rep.rounds[0].blind_labels}
        self.assertEqual(labels[2], "io")
        self.assertEqual(rep.spend.usd, 0)


@unittest.skipIf(sdk is None, "claude-agent-sdk not installed")
class TestTools(unittest.TestCase):
    @mock.patch("ffelab.orchestrator.workers._have", return_value=False)
    def test_tools_are_read_only_and_blind(self, _):
        L = setup()
        L.call(1, "render", 0.9, source="human")
        res = WorkerResult(worker="w", kind="red-team", task="t")
        tools = {t.name: t for t in build_tools(L, "red-team", res)}
        self.assertEqual(set(tools), {"blind_view", "list_scenarios", "ask_human"})
        self.assertEqual({t.name for t in build_tools(L, "blind-labeller", res)}, {"blind_view", "ask_human"})
        out = asyncio.run(tools["blind_view"].handler({"func": 1}))
        view = json.loads(out["content"][0]["text"])
        self.assertNotIn("SecretName", view["asm"])
        self.assertNotIn("render", json.dumps(view))
        err = asyncio.run(tools["blind_view"].handler({"func": 999}))
        self.assertTrue(err["is_error"])
        scen = json.loads(asyncio.run(tools["list_scenarios"].handler({}))["content"][0]["text"])
        self.assertEqual(scen, {"combat": ["combat"], "docked": ["docked"]})
        asyncio.run(tools["ask_human"].handler({"question": "is the combat save broken?", "func": 1}))
        self.assertEqual(res.questions[0].func, 1)
        self.assertEqual(res.tool_calls, ["blind_view", "blind_view", "list_scenarios", "ask_human"])


@unittest.skipIf(sdk is None, "claude-agent-sdk not installed")
class TestGraphTools(unittest.TestCase):
    """similar_functions / quotient_outliers register only when embed.py / graph.py are installed."""

    def fake_modules(self):
        import types
        embed = types.ModuleType("ffelab.embed")
        embed.DEFAULT_MODEL = "asm-ngram-v1"
        embed.calls = []

        def similar(con, func, model="asm-ngram-v1", k=10):
            embed.calls.append((func, model, k))
            if func == 999:
                raise KeyError("FUNC 999 has no embedding")
            return [{"func": 3, "score": 0.91}]
        embed.similar = similar
        graph = types.ModuleType("ffelab.graph")
        graph.outliers = lambda ledger: [{"caller": 1, "caller_label": "ui", "callee": 2, "callee_label": "math"},
                                         {"caller": 4, "caller_label": "io", "callee": 5, "callee_label": "ai"}]
        return {"ffelab.embed": embed, "ffelab.graph": graph}

    def test_without_the_modules(self):
        with mock.patch("ffelab.orchestrator.workers._have", return_value=False):
            from ffelab.orchestrator.workers import system_prompt, tool_names
            self.assertEqual(tool_names("blind-labeller"), ["blind_view", "ask_human"])
            self.assertEqual(tool_names("red-team"), ["blind_view", "list_scenarios", "ask_human"])
            self.assertNotIn("similar_functions", system_prompt("blind-labeller"))

    def test_with_the_modules(self):
        L = setup()
        mods = self.fake_modules()
        with mock.patch.dict("sys.modules", mods), \
                mock.patch("ffelab.orchestrator.workers._have", return_value=True):
            from ffelab.orchestrator.workers import system_prompt, tool_names
            blind = tool_names("blind-labeller")
            red = tool_names("red-team")
            self.assertEqual(set(blind), {"blind_view", "similar_functions", "ask_human"})
            self.assertEqual(set(red), {"blind_view", "list_scenarios", "similar_functions",
                                        "quotient_outliers", "ask_human"})
            self.assertNotIn("graph_votes", blind + red)                  # writes evidence: never a worker tool
            self.assertIn("call_layers", system_prompt("blind-labeller"))
            self.assertNotIn("quotient_outliers", system_prompt("blind-labeller"))
            self.assertIn("quotient_outliers", system_prompt("red-team"))

            res = WorkerResult(worker="w", kind="red-team", task="t")
            tools = {t.name: t for t in build_tools(L, "red-team", res)}
            out = asyncio.run(tools["similar_functions"].handler({"func": 2, "model": "callgraph-spectral-v1", "k": 3}))
            self.assertEqual(json.loads(out["content"][0]["text"]), [{"func": 3, "score": 0.91}])
            self.assertEqual(mods["ffelab.embed"].calls[-1], (2, "callgraph-spectral-v1", 3))
            asyncio.run(tools["similar_functions"].handler({"func": 2}))
            self.assertEqual(mods["ffelab.embed"].calls[-1], (2, "asm-ngram-v1", 10))
            self.assertTrue(asyncio.run(tools["similar_functions"].handler({"func": 999}))["is_error"])
            only = json.loads(asyncio.run(tools["quotient_outliers"].handler({"func": 5}))["content"][0]["text"])
            self.assertEqual([o["caller"] for o in only], [4])
            everything = json.loads(asyncio.run(tools["quotient_outliers"].handler({}))["content"][0]["text"])
            self.assertEqual(len(everything), 2)

            blind_tools = {t.name for t in build_tools(L, "blind-labeller", res)}
            self.assertEqual(blind_tools, {"blind_view", "similar_functions", "ask_human"})
            opts = ClaudeWorker(L, query_fn=lambda **k: None).options("blind-labeller", res, 5, None, None)
            self.assertEqual(set(opts.allowed_tools), {f"mcp__ffelab__{t}" for t in blind_tools})


def fake_query(script, captured):
    """A stand-in for claude_agent_sdk.query: records options, runs tool calls, yields messages."""
    async def query(prompt, options):
        captured["prompt"], captured["options"] = prompt, options
        for item in script:
            if callable(item):
                await item(captured)
            else:
                yield item
    return query


def assistant(mid, blocks, usage):
    return sdk.AssistantMessage(content=blocks, model="claude-opus-5-5", message_id=mid, usage=usage)


def result(structured, **kw):
    base = dict(subtype="success", duration_ms=10, duration_api_ms=9, is_error=False, num_turns=2,
                session_id="s", total_cost_usd=0.0123, structured_output=structured,
                usage={"input_tokens": 300, "output_tokens": 80, "cache_read_input_tokens": 1000})
    return sdk.ResultMessage(**base | kw)


@unittest.skipIf(sdk is None, "claude-agent-sdk not installed")
class TestClaudeWorker(unittest.TestCase):
    def run_worker(self, script, task=None):
        L = setup()
        captured = {"tools": {}}
        real = sdk.create_sdk_mcp_server

        def capture(name, tools=None):
            captured["tools"].update({t.name: t for t in tools})
            return real(name, tools=tools)
        with mock.patch.object(sdk, "create_sdk_mcp_server", side_effect=capture):
            w = ClaudeWorker(L, query_fn=fake_query(script, captured))
            res = asyncio.run(w.run(task or BlindTask(func=2), 7, 0.25))
        return res, captured

    @mock.patch("ffelab.orchestrator.workers._have", return_value=False)
    def test_success(self, _):
        async def call_tool(c):
            await c["tools"]["blind_view"].handler({"func": 2})
        script = [assistant("m1", [sdk.ToolUseBlock(id="t1", name="mcp__ffelab__blind_view", input={"func": 2})],
                            {"input_tokens": 100, "output_tokens": 20}),
                  call_tool,
                  assistant("m2", [sdk.TextBlock(text="It's maths.")], {"input_tokens": 200, "output_tokens": 60}),
                  result({"label": "math", "confidence": 0.6, "reasoning": "fmul everywhere"})]
        res, cap = self.run_worker(script)
        self.assertIsNone(res.error)
        self.assertEqual(res.output.label, "math")
        self.assertEqual(res.usage.input_tokens, 1300)
        self.assertEqual(res.usage.output_tokens, 80)
        self.assertEqual(res.usage.cost_usd, 0.0123)
        self.assertEqual(res.tool_calls, ["blind_view"])
        o = cap["options"]
        self.assertEqual(o.tools, [])
        self.assertEqual(o.setting_sources, [])
        self.assertEqual(o.permission_mode, "dontAsk")
        self.assertEqual(set(o.allowed_tools), {"mcp__ffelab__blind_view", "mcp__ffelab__ask_human"})
        self.assertEqual((o.max_turns, o.max_budget_usd), (7, 0.25))
        self.assertEqual(o.output_format["schema"]["properties"]["label"]["enum"][0], "render")
        self.assertIn("orchestrator", o.system_prompt)
        self.assertIn("FUNC 2", cap["prompt"])

    def test_invalid_answer_and_error_results(self):
        res, _ = self.run_worker([result({"label": "graphics", "confidence": 0.6, "reasoning": "x"})])
        self.assertTrue(res.error.startswith("invalid answer"))
        res, _ = self.run_worker([result(None, subtype="error_max_turns", is_error=True,
                                         errors=["Reached maximum number of turns (7)"])])
        self.assertIn("error_max_turns", res.error)
        self.assertEqual(res.usage.cost_usd, 0.0123)            # still counted against the budget
        plan = {"p_claim_wrong": 0.8, "attack": True, "rival": "ai", "rival_predicts": "hit",
                "reasoning": "x"}                               # attack without a scenario
        task = RedTeamTask(target={"claim_bet": 1, "func": 2}, scenarios={})
        res, _ = self.run_worker([result(plan)], task)
        self.assertIsNone(res.output)
        self.assertIn("scenario", res.error)

    def test_sdk_exception_is_a_failed_worker(self):
        async def boom(c):
            raise sdk.CLINotFoundError("claude not found")
        res, _ = self.run_worker([boom])
        self.assertIn("CLINotFoundError", res.error)


@unittest.skipIf(TracerProvider is None or sdk is None, "tracing or agent sdk not installed")
class TestTracing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(cls.exporter))
        tracing.setup("none", provider=provider)

    def test_span_tree(self):
        self.exporter.clear()
        L = setup()
        L.call(1, "render", 0.9, source="human")
        claim = L.con.execute("SELECT id FROM bets WHERE kind='claim'").fetchone()["id"]
        plan = RedTeamPlan(p_claim_wrong=0.7, attack=True, rival="ai", rival_predicts="hit",
                           scenario="combat", reasoning="r")
        asyncio.run(orch(L, ScriptedWorker(plans={claim: plan}), budget=Budget(max_rounds=1),
                         blind_per_round=1).run())
        spans = {s.name: s for s in self.exporter.get_finished_spans()}
        self.assertIn("agents.run", spans)
        self.assertIn("round 1", spans)
        exp = spans["experiment logging_pass"]
        self.assertEqual(exp.parent.span_id, spans["round 1"].context.span_id)
        self.assertEqual(spans["round 1"].parent.span_id, spans["agents.run"].context.span_id)
        self.assertEqual(spans["agents.run"].attributes["ffelab.stop"], "max_rounds")
        self.assertEqual(spans["agents.run"].attributes["gen_ai.usage.input_tokens"], 2000)

    def test_claude_worker_spans(self):
        self.exporter.clear()
        L = setup()

        async def call_tool(c):
            await c["tools"]["blind_view"].handler({"func": 2})
        captured = {"tools": {}}
        real = sdk.create_sdk_mcp_server

        def capture(name, tools=None):
            captured["tools"].update({t.name: t for t in tools})
            return real(name, tools=tools)
        script = [assistant("m1", [sdk.ToolUseBlock(id="t1", name="mcp__ffelab__blind_view", input={"func": 2})],
                            {"input_tokens": 100, "output_tokens": 20}),
                  call_tool,
                  assistant("m2", [sdk.TextBlock(text="maths")], {"input_tokens": 200, "output_tokens": 60}),
                  result({"label": "math", "confidence": 0.6, "reasoning": "r"})]
        with mock.patch.object(sdk, "create_sdk_mcp_server", side_effect=capture):
            w = ClaudeWorker(L, query_fn=fake_query(script, captured))
            with tracing.span("parent") as p:
                asyncio.run(w.run(BlindTask(func=2), 5, None, p))
        spans = self.exporter.get_finished_spans()
        names = [s.name for s in spans]
        self.assertEqual(names.count("chat claude-opus-5-5"), 2)
        agent = next(s for s in spans if s.name == "invoke_agent blind-labeller")
        tool = next(s for s in spans if s.name == "execute_tool blind_view")
        self.assertEqual(tool.parent.span_id, agent.context.span_id)
        self.assertEqual(agent.attributes["gen_ai.usage.output_tokens"], 80)
        self.assertEqual(agent.attributes["gen_ai.usage.cost"], 0.0123)
        chat = next(s for s in spans if s.name == "chat claude-opus-5-5")
        self.assertEqual(chat.attributes["openinference.span.kind"], "LLM")
        self.assertEqual(chat.attributes["llm.token_count.prompt"], 100)


class TestCli(unittest.TestCase):
    def test_agents_commands(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path
        from ffelab.cli import main
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            L = setup()
            L.con.execute(f"VACUUM INTO '{db}'")
            buf = io.StringIO()
            with redirect_stdout(buf):
                main(["agents", "run", "--db", str(db), "--worker", "heuristic", "--rounds", "2",
                      "--out", str(Path(d) / "rep.json")])
                L2 = Ledger(dbmod.connect(db))
                qid = qs.ask_draft(L2.con, "w", qs.QuestionDraft(question="is FUNC 3 the HUD?", func=3))
                main(["agents", "questions", "--db", str(db)])
                main(["agents", "answer", str(qid), "yes", "--db", str(db)])
                main(["agents", "questions", "--db", str(db), "--all"])
            out = buf.getvalue()
            self.assertIn("stopped:", out)
            self.assertIn("is FUNC 3 the HUD?", out)
            self.assertIn("answer: yes", out)
            rep = json.loads((Path(d) / "rep.json").read_text())
            self.assertEqual(rep["worker"], "heuristic")


if __name__ == "__main__":
    unittest.main()
