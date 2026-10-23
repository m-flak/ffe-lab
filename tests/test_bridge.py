import os
import tempfile
import unittest
from pathlib import Path

from ffelab import bridge as br
from ffelab.ledger import Ledger

from store import fresh_db

ENGINE = {
    "build.zig": "const std = @import(\"std\");\npub fn build(b: *std.Build) void {}\n",
    "src/gfx.zig": ("const sg = @import(\"sokol\").gfx;\n"
                    "pub const Vertex = extern struct { x: f32, y: f32 };\n"
                    "pub fn drawTri(v: [3]Vertex) void {}\n"
                    "fn helper() void {}\n"
                    "test \"tri\" {}\n"),
    ".zig-cache/o/junk.zig": "pub fn cached() void {}\n",
}


def make_engine() -> Path:
    root = Path(tempfile.mkdtemp())
    for rel, text in ENGINE.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def make_ledger():
    """FUNCs 1-3 are render (2 and 3 share DATA_000010 with 4); 4 is ui and calls 2; 3 calls 5 (math)."""
    con = fresh_db()
    con.executemany("INSERT INTO functions (num,pos,ord,name,n_ins,asm_text) VALUES (?,?,?,?,?,?)",
                    [(n, 0x1000 + 16 * n, n - 1, None, 3, "ret") for n in range(1, 6)])
    con.executemany("INSERT INTO calls VALUES (?,?,?)",
                    [(1, 2, "direct"), (4, 2, "direct"), (3, 5, "direct")])
    con.executemany("INSERT INTO data_refs VALUES (?,?)", [(2, 10), (3, 10), (4, 10), (1, 11)])
    con.execute("INSERT INTO segments (seg, first_ord, last_ord, first_num, last_num) VALUES (0,0,2,1,3)")
    led = Ledger(con)
    for f, lab, p in [(1, "render", 0.9), (2, "render", 0.8), (3, "render", 0.6), (4, "ui", 0.9), (5, "math", 0.9)]:
        dist = {lab2: (1 - p) / (len(led.labels) - 1) for lab2 in led.labels}
        dist[lab] = p
        led._store(f, dist)
    con.execute("INSERT INTO func_labels (func, path, source, confidence) VALUES (2,'render/planet','human',0.8)")
    con.commit()
    return led


class TestZigRepo(unittest.TestCase):
    def setUp(self):
        self.repo = br.ZigRepo(make_engine())

    def test_tree_skips_caches(self):
        paths = [t["path"] for t in self.repo.tree()]
        self.assertEqual(paths, ["build.zig", "src/gfx.zig"])

    def test_read_numbers_lines(self):
        r = self.repo.read("src/gfx.zig", 3, 3)
        self.assertIn("pub fn drawTri", r["text"])
        self.assertTrue(r["text"].lstrip().startswith("3"))

    def test_grep_and_outline(self):
        self.assertEqual([(g["path"], g["line"]) for g in self.repo.grep(r"fn drawTri")], [("src/gfx.zig", 3)])
        decls = {(d["kind"], d["name"], d["pub"]) for d in self.repo.outline("src/gfx.zig")}
        self.assertTrue({("type", "Vertex", True), ("fn", "drawTri", True), ("fn", "helper", False),
                         ("test", "tri", False)} <= decls)

    def test_refuses_paths_outside_the_root(self):
        with self.assertRaises(ValueError):
            self.repo.read("../etc/passwd")
        with self.assertRaises(ValueError):
            self.repo.read("/etc/passwd")
        outside = Path(tempfile.mkdtemp()) / "secret.zig"
        outside.write_text("pub fn secret() void {}\n")
        os.symlink(outside, self.repo.root / "src" / "link.zig")
        with self.assertRaises(ValueError):
            self.repo.read("src/link.zig")
        self.assertNotIn("src/link.zig", [t["path"] for t in self.repo.tree()])
        self.assertEqual(self.repo.grep("secret"), [])


class TestSubsystem(unittest.TestCase):
    def test_label_box(self):
        s = br.subsystem(make_ledger(), "render")
        self.assertEqual(s["members"], 3)
        self.assertEqual(s["weak_members"], ["FUNC_000003"])               # belief 0.6
        self.assertEqual([e["func"] for e in s["entry_points"]], ["FUNC_000002"])   # called by ui FUNC 4
        self.assertEqual([c["func"] for c in s["calls_out"]], ["FUNC_000005"])
        self.assertEqual(s["shared_data"], [{"data": "DATA_000010", "members": 2, "outside_funcs": 1}])
        self.assertEqual(s["private_data"], ["DATA_000011"])

    def test_deeper_path_and_segment(self):
        led = make_ledger()
        self.assertEqual(list(br.members(led, "render/planet")), [2])
        self.assertEqual(list(br.members(led, "seg:0")), [1, 2, 3])
        with self.assertRaises(ValueError):
            br.members(led, "nonsense")


def make_cut_ledger():
    """6 draws a screen and stores a price that economy FUNC 8 reads -> split.
    7 only draws and writes a UI-only flag -> gut. 8 is economy logic -> keep."""
    con = fresh_db()
    asm = {
        5: "call _C_DrawQuad\nret",                                                  # render helper
        6: "call FUNC_000005\nmov [DATA_000020],eax\nmov eax,[DATA_000021]\nret",
        7: "call FUNC_000005\nnot byte [DATA_000022]\nret",
        8: "mov eax,[DATA_000020]\nret",
    }
    con.executemany("INSERT INTO functions (num,pos,ord,name,n_ins,asm_text) VALUES (?,?,?,?,?,?)",
                    [(n, 0x1000 + 16 * n, n, None, 3, a) for n, a in asm.items()])
    con.executemany("INSERT INTO calls VALUES (?,?,?)", [(6, 5, "direct"), (7, 5, "direct"), (9, 6, "direct")])
    con.executemany("INSERT INTO data_refs VALUES (?,?)", [(6, 20), (6, 21), (7, 22), (8, 20)])
    led = Ledger(con)
    for f, lab in [(5, "render"), (8, "economy")]:
        dist = {lab2: 0.01 for lab2 in led.labels}
        dist[lab] = 0.9
        led._store(f, dist)
    con.commit()
    return led


class TestCutCheck(unittest.TestCase):
    def test_data_access(self):
        acc = br.data_access("mov [DATA_000001],eax\nmov eax,[DATA_000002]\nnot byte [DATA_000003]\n"
                             "mov [edx*4+DATA_000004],ecx\ncmp dword [DATA_000005],0\npush dword DATA_000006\n"
                             "lea esi,[DATA_000007+4]\ncall near [DATA_000008]    ; FUNC_001440_X")
        self.assertEqual(acc, {1: "write", 2: "read", 3: "write", 4: "write", 5: "read",
                               6: "addr", 7: "addr", 8: "read"})

    def test_verdicts(self):
        led = make_cut_ledger()
        split = br.cut_check(led, 6)
        self.assertEqual(split["verdict"], "split")
        self.assertEqual(split["data_writes"][0]["logic_readers"], ["FUNC_000008"])
        self.assertEqual(split["data_reads"], ["DATA_000021"])
        self.assertEqual(br.cut_check(led, 7)["verdict"], "gut")
        self.assertEqual(br.cut_check(led, 8)["verdict"], "keep")
        self.assertEqual(br.cut_check(led, 5)["externs"], ["_C_DrawQuad"])

    def test_unlabelled_callees_make_it_unsure(self):
        led = make_cut_ledger()
        led.con.execute("DELETE FROM beliefs WHERE func=5")
        self.assertEqual(br.cut_check(led, 7)["verdict"], "unsure")


class TestLinks(unittest.TestCase):
    def test_link_is_a_proposal_outside_the_ledger(self):
        led = make_ledger()
        before = led.belief(2)
        n_ev = led.con.execute("SELECT COUNT(*) AS n FROM evidence").fetchone()["n"]
        repo = br.ZigRepo(make_engine())
        r = br.link(led, "FUNC_000002", "src/gfx.zig:drawTri", "replaces", "same triangle, hi-res", "ai:muse", repo)
        self.assertEqual((r["status"], r["zig_file_exists"]), ("proposed", True))
        self.assertEqual(led.belief(2), before)
        self.assertEqual(led.con.execute("SELECT COUNT(*) AS n FROM evidence").fetchone()["n"], n_ev)
        self.assertEqual(len(br.links(led.con, ffe="FUNC_000002")), 1)
        self.assertEqual(br.set_status(led.con, r["id"], "accepted")["status"], "accepted")

    def test_refusals(self):
        led = make_ledger()
        with self.assertRaises(ValueError):
            br.link(led, "FUNC_000002", "src/gfx.zig", "eats", "x", "ai:muse")       # unknown relation
        with self.assertRaises(ValueError):
            br.link(led, "FUNC_000002", "src/gfx.zig", "replaces", " ", "ai:muse")   # no note
        with self.assertRaises(KeyError):
            br.link(led, "FUNC_000099", "src/gfx.zig", "replaces", "x", "ai:muse")   # no such FUNC
        with self.assertRaises(ValueError):
            br.link(led, "FUNC_000002", "../x.zig", "replaces", "x", "ai:muse", br.ZigRepo(make_engine()))
        with self.assertRaises(ValueError):
            br.set_status(led.con, 1, "maybe")


if __name__ == "__main__":
    unittest.main()
