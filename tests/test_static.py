import os
import tempfile
import unittest
from pathlib import Path

from ffelab.asm import parse_asm, address_ordered
from ffelab.tables import find_tables
from ffelab import segment as S

ASM = """BITS 32
GLOBAL FUNC_000001
FUNC_000001:\t\t; Pos = 1000
\t\tpush ebp
\t\tmov ebp,esp
\t\tcall FUNC_000002
\t\tmov eax,[DATA_000010]
\t\tpop ebp
\t\tret
\t\tnop
\t\tnop

FUNC_000002:
_FUNC_000002_SysMenuInit:\t\t; Pos = 1010
\t\tpush ebp
\t\tmov ebp,esp
\t\tcall near [DATA_000020]    ; FUNC_000003_GetModelPtr
\t\tjmp FUNC_000001
\t\tret

_FUNC_000003_GetModelPtr:\t\t; Pos = 1023
\t\tmov eax,[eax*4+DATA_000030]
\t\tret
"""

DAT = """GLOBAL _DATA_X
DATA_000100:\t\t; Event function table for Demo
\tdd FUNC_000002_SysMenuInit
\tdd FUNC_000001
\tdb 0x0, 0x0, 0x0, 0x0
\tdd FUNC_000003
\tdb 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0
\tdb 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0
\tdb 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0
\tdb 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0
DATA_000101:
\tdb 'hello', 0x0
DATA_000102:
\tdd FUNC_000001
\tdd FUNC_000003
"""


def write(tmp, name, text):
    p = Path(tmp) / name
    p.write_text(text)
    return p


class TestAsm(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.funcs = parse_asm(write(self.tmp, "ffe.asm", ASM))

    def test_functions_and_aliases(self):
        self.assertEqual([f.num for f in self.funcs], [1, 2, 3])
        f2 = self.funcs[1]
        self.assertEqual(f2.name, "SysMenuInit")
        self.assertEqual(f2.pos, 0x1010)
        self.assertEqual(f2.prefix(), "SysMenu")

    def test_calls_data_and_shape(self):
        f1, f2, f3 = self.funcs
        self.assertEqual(f1.calls, [2])
        self.assertEqual(f1.data_refs, [10])
        self.assertTrue(f1.has_frame)
        self.assertEqual(f1.filler_tail, 2)
        self.assertEqual(f2.calls, [3])              # resolved from the indirect-call comment
        self.assertEqual(f2.indirect_calls, [20])
        self.assertEqual(f2.tail_jumps, [1])
        self.assertFalse(f3.has_frame)
        self.assertFalse(f3.aligned4)               # 0x1023

    def test_address_order(self):
        self.assertEqual([f.num for f in address_ordered(self.funcs)], [1, 2, 3])


class TestTables(unittest.TestCase):
    def test_event_table_slots(self):
        tmp = tempfile.mkdtemp()
        tables = find_tables(write(tmp, "ffedat.asm", DAT))
        ev = [t for t in tables if t.kind == "event_table"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0].slots, [2, 1, None, 3] + [None] * 8)
        self.assertIn("Demo", ev[0].comment)
        plain = [t for t in tables if t.kind == "func_table"]
        self.assertEqual([t.functions for t in plain], [[1, 3]])


class TestSegmenter(unittest.TestCase):
    def _module(self, start, n, prefix, base_pos):
        """n functions that call each other and share data, as one 'file'."""
        lines = []
        for i in range(n):
            num = start + i
            callee = start + (i + 1) % n
            lines += [f"_FUNC_{num:06d}_{prefix}{i}:\t\t; Pos = {base_pos + 16 * i:x}",
                      "\t\tpush ebp", "\t\tmov ebp,esp",
                      f"\t\tcall FUNC_{callee:06d}",
                      f"\t\tmov eax,[DATA_{start * 10 + i % 3:06d}]",
                      "\t\tpop ebp", "\t\tret", ""]
        return lines

    def test_boundary_between_modules(self):
        lines = (self._module(1, 12, "SysMenu", 0x1000) + self._module(13, 12, "Gui", 0x2000)
                 + self._module(25, 12, "Sound", 0x3000))
        tmp = tempfile.mkdtemp()
        funcs = parse_asm(write(tmp, "ffe.asm", "\n".join(lines)))
        seg = S.segment(funcs, [])
        # the true cuts are at ordered index 12 and 24
        top = sorted(range(1, len(seg.ordered)), key=lambda c: -seg.cut_prob[c - 1])[:2]
        self.assertEqual(sorted(top), [12, 24])
        self.assertIn(12, seg.boundaries)
        self.assertIn(24, seg.boundaries)

    def test_decode_respects_anchor_veto_and_gaps(self):
        import numpy as np
        probs = np.array([0.9, 0.1, 0.1, 0.2, 0.1, 0.1])        # cuts 1..6
        anchors = [(0, 2, None), (5, 6, None)]                   # cut 1,2 inside A; cut 6 inside B
        bounds = S.decode(probs, anchors, threshold=0.5)
        self.assertNotIn(1, bounds)                               # vetoed despite p=0.9
        self.assertTrue(any(3 <= c <= 5 for c in bounds))         # gap between tables gets one
        self.assertIn(4, bounds)                                  # the best cut in that gap


@unittest.skipUnless(os.environ.get("FFED3D_DIR"), "set FFED3D_DIR to a clone of ffed3d")
class TestRealData(unittest.TestCase):
    def test_build(self):
        src = Path(os.environ["FFED3D_DIR"])
        funcs = parse_asm(src / "ffe.asm")
        tables = find_tables(src / "ffedat.asm")
        self.assertGreater(len(funcs), 1500)
        self.assertGreaterEqual(sum(t.kind == "event_table" for t in tables), 13)
        seg = S.segment(funcs, tables)
        self.assertGreater(seg.report["model_cv_auc"], 0.7)


if __name__ == "__main__":
    unittest.main()
