import json
import random
import tempfile
import unittest
from pathlib import Path

from ffelab import db as dbmod, exe, export
from ffelab.ledger import Ledger


def framed_funcs(n=60, start=0x11000, seed=7):
    """Irregular sizes like real code; every 9th function has no push ebp/mov ebp,esp."""
    rnd = random.Random(seed)
    out, pos = [], start
    for i in range(n):
        out.append((i + 1, pos, i % 9 != 0))
        pos += 4 * rnd.randint(8, 200)
    return out


def make_exe(funcs, shifts=(), seed=1):
    rnd = random.Random(seed)
    b = bytearray(rnd.getrandbits(8) for _ in range(exe.FCODEOFF + 0x20000))
    for i in range(len(b) - 2):
        if b[i] == 0x55 and b[i + 1] in (0x8B, 0x89):
            b[i] = 0x90
    b[:2] = b"MZ"
    for n, p, f in funcs:
        if f:
            off = exe.to_offset(p) + sum(e for t, e in shifts if p >= t)
            b[off:off + 3] = b"\x55\x8b\xec"
    return bytes(b)


class TestExeCheck(unittest.TestCase):
    def test_same_build(self):
        funcs = framed_funcs()
        r = exe.check(make_exe(funcs), funcs)
        self.assertEqual(r["verdict"], "same_build")
        self.assertEqual(r["match_rate"], 1.0)
        self.assertIsNone(r["diverges_at"])

    def test_patched_build_detected_and_aligned(self):
        funcs = framed_funcs()
        shifts = [(funcs[30][1], 0x18)]
        data = make_exe(funcs, shifts)
        r = exe.check(data, funcs)
        self.assertNotEqual(r["verdict"], "same_build")
        self.assertEqual(r["diverges_at"]["func"], 31)
        rows = {x["func"]: x for x in exe.align(data, funcs)}
        for n, p, f in funcs:
            want = exe.to_offset(p) + sum(e for t, e in shifts if p >= t)
            self.assertEqual(rows[n]["offset"], want, f"func {n}")
        self.assertEqual(rows[10]["method"], "interpolated")   # funcs 1, 10, 19... have no frame

    def test_run_stores_identity_map(self):
        con = dbmod.connect(":memory:")
        funcs = framed_funcs()
        con.executemany("INSERT INTO functions (num, pos, has_frame) VALUES (?,?,?)",
                        [(n, p, int(f)) for n, p, f in funcs])
        path = Path(tempfile.mkdtemp()) / "FIRSTENC.EXE"
        path.write_bytes(make_exe(funcs))
        r = exe.run(con, path)
        m = exe.load_map(con, r["binary_id"])
        self.assertEqual(m, {n: p for n, p, _ in funcs})


ASM = """FUNC_000001:\t\t; Pos = 11000
\t\tret
FUNC_000002:
_FUNC_000002_SysMenuInit:\t\t; Pos = 11010
\t\tret
FUNC_000003:\t\t; Pos = 11020
\t\tret
"""


class TestExport(unittest.TestCase):
    def setUp(self):
        self.con = dbmod.connect(":memory:")
        self.con.executemany("INSERT INTO functions (num, pos, name) VALUES (?,?,?)",
                             [(1, 0x11000, None), (2, 0x11010, "SysMenuInit"), (3, 0x11020, None)])
        self.L = Ledger(self.con)
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "ffe.asm").write_text(ASM)

    def test_alias_annotation_and_diff(self):
        exp = self.L.record_experiment("knockout", "s", {"func": 1})
        for _ in range(5):
            self.L.update(1, {"render": 5.0}, "knockout", "knockout", experiment_id=exp)
        self.L.commit_name(1, "render/planet/atmosphere/clouds", 0.95)
        self.L.commit_name(2, "ui/menu/init", 0.92)
        r = export.export(self.con, self.dir, self.dir / "out")
        new = (self.dir / "out" / "ffe.asm").read_text()
        self.assertIn("_FUNC_000001_RenderPlanetAtmosphereClouds:", new)
        self.assertIn("ffelab: render/planet/atmosphere/clouds p=0.95", new)
        self.assertNotIn("_FUNC_000002_UiMenuInit", new)          # inherited name kept, no second name
        self.assertIn("_FUNC_000002_SysMenuInit:\t\t; Pos = 11010  ; ffelab: ui/menu/init", new)
        self.assertIn("FUNC_000001:", new)                         # original label untouched
        self.assertEqual(r["aliases_added"], 1)
        self.assertEqual(r["annotated"], 1)
        self.assertTrue((self.dir / "out" / "ffe.asm.diff").read_text().startswith("--- a/ffe.asm"))
        self.assertEqual((self.dir / "ffe.asm").read_text(), ASM)  # source never modified

    def test_threshold_and_contradictions_block_export(self):
        self.L.commit_name(3, "math/fixed", 0.6)
        self.assertEqual(export.export(self.con, self.dir, self.dir / "o1")["aliases_added"], 0)
        self.con.execute("DELETE FROM names")
        self.L.commit_name(3, "math/fixed", 0.95)
        self.con.execute("INSERT INTO contradictions (func, claimed, favoured, status) VALUES (3,'math','ui','open')")
        self.assertEqual(export.export(self.con, self.dir, self.dir / "o2")["aliases_added"], 0)

    def test_auto_uses_experiment_backed_paths_only(self):
        exp = self.L.record_experiment("knockout", "s", {"func": 3})
        for _ in range(6):
            self.L.update(3, {"render": 6.0}, "knockout", "knockout", experiment_id=exp)
        self.con.execute("INSERT INTO func_labels (func, path, source, confidence, experiment_id) VALUES (3,'render/stars',"
                         "'ai',0.9,?)", (exp,))
        self.con.execute("INSERT INTO func_labels (func, path, source, confidence) VALUES (1,'render/x','human',0.9)")
        r = export.export(self.con, self.dir, self.dir / "o3", auto=True)
        labels = [n["label"] for n in r["names"]]
        self.assertEqual(labels, ["_FUNC_000003_RenderStars"])     # func 1 has no experiment behind it


if __name__ == "__main__":
    unittest.main()
