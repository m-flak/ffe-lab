"""The cold-start case: breaking in a region of unnamed functions.

Story: FUNC 10 draws the clouds on landable planets. Nothing names it, nothing
hints at it. The only clue in the code is that it calls a translucent-texture
routine whose inherited name is Tex64Trans. These tests walk the whole path:
fingerprint -> controlled contrast -> behaviour group -> callee propagation ->
knockout -> observation -> label path.
"""
import json
import shutil
import tempfile
import unittest

from ffelab import behavior, db as dbmod, knockout as ko, labels as lbl, propagate as prop
from ffelab import adversary as adv
from store import fresh_db
from ffelab.harness.fake import FakeBackend
from ffelab.ledger import Ledger
from ffelab.loop import logging_pass

CLOUDS, SHADOWS, TERRAIN, DOCKUI, TEX, CORE = 10, 12, 11, 30, 20, 40

SCENARIOS = {
    "deep_space": ["space"],
    "orbit_atmo": ["near_planet", "atmosphere"],
    "orbit_airless": ["near_planet"],
    "docked": ["docked"],
}
TRUTH = {
    "deep_space": {CORE: 1.0},
    "orbit_atmo": {CLOUDS: 1.0, SHADOWS: 1.0, TERRAIN: 1.0, TEX: 3.0, CORE: 1.0},
    "orbit_airless": {TERRAIN: 1.0, TEX: 1.0, CORE: 1.0},
    "docked": {DOCKUI: 0.02, CORE: 1.0},
}
VISUALS = {"orbit_atmo": [(TERRAIN, (0, 120, 319, 199), (40, 140, 40)),
                          (CLOUDS, (40, 60, 200, 100), (235, 235, 235))]}


def world():
    con = fresh_db()
    rows = [(CLOUDS, None, None), (SHADOWS, None, None), (TERRAIN, None, None),
            (DOCKUI, None, None), (TEX, "Tex64Trans", "Tex"), (CORE, None, None)]
    con.executemany("INSERT INTO functions (num, name, prefix, asm_text) VALUES (?,?,?,'ret')", rows)
    con.executemany("INSERT INTO calls VALUES (?,?,'direct')",
                    [(CLOUDS, TEX), (SHADOWS, TEX), (TERRAIN, TEX)])
    con.commit()
    L = Ledger(con)
    for name, tags in SCENARIOS.items():
        behavior.register_scenario(L, name, tags)
    fb = FakeBackend(TRUTH, context=SCENARIOS, visuals=VISUALS, crashers={CORE},
                     animated=(250, 10, 300, 40))
    return L, fb


def run_all(L, fb, funcs):
    for sc, tags in SCENARIOS.items():
        logging_pass(fb, L, sc, funcs, tags)


class TestFingerprints(unittest.TestCase):
    def setUp(self):
        self.L, self.fb = world()
        run_all(self.L, self.fb, [CLOUDS, SHADOWS, TERRAIN, DOCKUI])

    def test_profile_without_knowing_anything(self):
        p = behavior.profile(self.L, CLOUDS)
        self.assertEqual({k: v["state"] for k, v in p.items()},
                         {"deep_space": "silent", "orbit_atmo": "per_frame",
                          "orbit_airless": "silent", "docked": "silent"})

    def test_single_tag_contrast_finds_atmosphere(self):
        effects = behavior.tag_effects(self.L, CLOUDS)
        atmo = [e for e in effects if e["tag"] == "atmosphere"]
        self.assertEqual(len(atmo), 1)
        self.assertTrue(atmo[0]["responds"])
        terrain = [e for e in behavior.tag_effects(self.L, TERRAIN) if e["tag"] == "atmosphere"]
        self.assertFalse(terrain[0]["responds"])         # terrain doesn't care about air

    def test_behaviour_group_and_description(self):
        g = behavior.behaviour_groups(self.L)
        cloudish = [x for x in g["groups"] if CLOUDS in x["members"]]
        self.assertEqual(cloudish[0]["members"], [CLOUDS, SHADOWS])
        self.assertIn("per-frame in orbit_atmo", cloudish[0]["description"])
        self.assertNotIn(DOCKUI, cloudish[0]["members"])
        self.assertEqual(behavior.similar(self.L, CLOUDS)[0]["func"], SHADOWS)

    def test_contrast_suggestions(self):
        L, fb = world()
        logging_pass(fb, L, "orbit_atmo", [CLOUDS], SCENARIOS["orbit_atmo"])
        s = behavior.suggest_contrasts(L, CLOUDS)
        self.assertEqual(s[0]["run"], "orbit_airless")
        self.assertEqual(s[0]["varies"], "atmosphere")


class TestPropagation(unittest.TestCase):
    def test_callee_name_pushes_render_and_is_idempotent(self):
        L, _ = world()
        prop.propagate_all(L)
        prop.propagate_all(L)
        b = L.belief(CLOUDS)
        self.assertEqual(max(b, key=b.get), "render")
        n = L.con.execute("SELECT COUNT(*) AS n FROM evidence WHERE func=? AND kind='static_callee'",
                          (CLOUDS,)).fetchone()["n"]
        self.assertEqual(n, 1)
        _, n_exp = L.experimental_belief(CLOUDS)
        self.assertEqual(n_exp, 0)                      # static evidence never counts as experiment

    def test_experiment_backed_callee_outweighs_inherited_name(self):
        L, _ = world()
        exp = L.record_experiment("halt", "x", {"func": TEX})
        for _ in range(3):
            L.update(TEX, {"ui": 5.0}, "halt", "halt", experiment_id=exp)   # experiments say ui
        prop.propagate_one(L, CLOUDS)
        detail = json.loads(L.con.execute(
            "SELECT detail FROM evidence WHERE func=? AND kind='static_callee'", (CLOUDS,)).fetchone()["detail"])
        self.assertEqual(detail["callees"][0][1:], ["ui", "experiment"])


class TestKnockout(unittest.TestCase):
    def setUp(self):
        self.L, self.fb = world()
        self.out = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def test_clouds_vanish(self):
        bet = self.L.place_bet("human", CLOUDS, "visible", "knocking it out changes the picture", 0.7,
                               spec={"knockout": True, "func": CLOUDS, "scenario": "orbit_atmo",
                                     "expect": "visible"})
        r = ko.knockout(self.fb, self.L, "orbit_atmo", CLOUDS, outdir=self.out)
        self.assertEqual((r["health"], r["outcome"]), ("ok", "visible"))
        self.assertEqual(r["bbox"], [40, 60, 201, 101])           # exactly the cloud layer
        self.assertEqual(r["resolved"][0]["bet"], bet)
        self.assertTrue(r["resolved"][0]["outcome"])
        self.assertEqual(self.fb.knocked, set())                   # restored afterwards
        # the animated corner differed between baselines and must not count
        self.assertLess(r["changed_frac"], 0.15)

        obs = ko.record_observation(self.L, r["experiment"], CLOUDS, "render/planet/atmosphere/clouds",
                                    0.9, "white cloud layer above terrain disappears", "ai:vision")
        self.assertEqual(obs["belief_top"], "render")
        vocab = {v["path"] for v in lbl.vocabulary(self.L.con)}
        self.assertTrue({"render", "render/planet", "render/planet/atmosphere",
                         "render/planet/atmosphere/clouds"} <= vocab)
        deepest = lbl.deepest_label(self.L.con, CLOUDS)
        self.assertEqual((deepest["path"], deepest["experiment_id"]),
                         ("render/planet/atmosphere/clouds", r["experiment"]))

    def test_invisible_and_crash_outcomes(self):
        r = ko.knockout(self.fb, self.L, "orbit_atmo", DOCKUI, outdir=self.out)
        self.assertEqual(r["outcome"], "invisible")
        self.assertLess(self.L.belief(DOCKUI)["render"], 1 / 13)
        c = ko.knockout(self.fb, self.L, "orbit_atmo", CORE, outdir=self.out)
        self.assertEqual((c["health"], c["outcome"]), ("crashed", "crash"))
        self.assertGreater(self.L.belief(CORE)["platform"], self.L.belief(CORE)["render"])

    def test_observation_guards(self):
        r = ko.knockout(self.fb, self.L, "orbit_atmo", DOCKUI, outdir=self.out)
        with self.assertRaises(ValueError):                         # nothing visible to describe
            ko.record_observation(self.L, r["experiment"], DOCKUI, "ui/dock", 0.8, "x", "ai:vision")
        v = ko.knockout(self.fb, self.L, "orbit_atmo", CLOUDS, outdir=self.out)
        with self.assertRaises(ValueError):                         # wrong function for that knockout
            ko.record_observation(self.L, v["experiment"], TERRAIN, "render/x", 0.8, "x", "ai:vision")
        exp = self.L.record_experiment("halt", "orbit_atmo", {"func": CLOUDS})
        with self.assertRaises(ValueError):                         # not a knockout
            ko.record_observation(self.L, exp, CLOUDS, "render/x", 0.8, "x", "ai:vision")


class TestLabelPaths(unittest.TestCase):
    def test_paths_in_claims(self):
        L, _ = world()
        L.call(CLOUDS, "Render/Planet/Atmosphere/Clouds", 0.8, source="human")
        b = L.belief(CLOUDS)
        self.assertEqual(max(b, key=b.get), "render")
        claim = L.con.execute("SELECT label FROM bets WHERE func=? AND kind='claim'", (CLOUDS,)).fetchone()
        self.assertEqual(claim["label"], "render/planet/atmosphere/clouds")
        with self.assertRaises(ValueError):
            L.call(CLOUDS, "weather/clouds", 0.8)            # must hang under a top-level subsystem
        with self.assertRaises(ValueError):
            L.call(CLOUDS, "render//bad path!", 0.8)

    def test_claim_path_settles_on_top_level(self):
        L, _ = world()
        L.call(CLOUDS, "render/planet/atmosphere/clouds", 0.9, source="human")
        exp = L.record_experiment("halt", "orbit_atmo", {"func": CLOUDS})
        for _ in range(5):
            L.update(CLOUDS, {"render": 5.0}, "halt", "halt", experiment_id=exp)
        settled = L.settle_claims()
        self.assertTrue(settled[0]["outcome"])

    def test_rival_must_differ_at_top_level(self):
        L, _ = world()
        L.call(CLOUDS, "render/planet/atmosphere/clouds", 0.9, source="human")
        claim = L.con.execute("SELECT id FROM bets WHERE kind='claim'").fetchone()["id"]
        with self.assertRaises(ValueError):
            adv.place_attack(L, claim, "orbit_atmo", "render/planet/terrain", "miss", 0.6)
        a = adv.place_attack(L, claim, "docked", "ui/dock", "hit", 0.3)
        self.assertIn("prediction_bet", a)


if __name__ == "__main__":
    unittest.main()
