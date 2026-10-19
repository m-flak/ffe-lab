import tempfile
import unittest
from pathlib import Path

from ffelab import fe2ref
from ffelab.asm import parse_asm

from store import fresh_db

# A tiny fe2.s: a5 jump table, a named RNG that shares its magic with FFE, a caller,
# and a word table that FFE stores little-endian.
FE2 = """A5_RandInt\t\tequ\t6
L100_a5_jumptab:
:\t\tjmp\tL200_Sqrt
:\t\tjmp\tL300_RandInt
L200_Sqrt:
\t\tcmp.l\t#$4000000,d0
\t\tmove.w\tL900_sqrt_table(pc,d0.w),d0
\t\trts
L300_RandInt:
\t\teori.l\t#$1234567f,d1
\t\tror.l\t#7,d1
\t\tswap\td0
\t\trts
L400:
\t\tjsr\tA5_RandInt(a5)
\t\tbsr.w\tL200_Sqrt
\t\tmove.w\t#$abcd,d2
\t\trts
L900_sqrt_table:
\t\tdc.b\t$0,$0,$5,$a8,$8,$0,$9,$cc,$b,$50,$c,$a6,$d,$db,$e,$f7
\t\tdc.b\t$10,$0,$10,$f8,$11,$e3,$12,$c2,$13,$98,$14,$65,$15,$2a,$15,$e8
"""

FFE = """FUNC_000001:\t\t; Pos = 1000
\t\tpush ebp
\t\tmov ebp,esp
\t\tcmp eax,0x4000000
\t\tmovzx eax,word [eax*2+DATA_000077]
\t\tpop ebp
\t\tret

FUNC_000002:\t\t; Pos = 1010
\t\tmov eax,[DATA_000088]
\t\txor eax,0x1234567f
\t\tshr eax,0x7
\t\tret

FUNC_000003:\t\t; Pos = 1020
\t\tpush ebp
\t\tmov ebp,esp
\t\tcall FUNC_000002
\t\tcall FUNC_000001
\t\tpop ebp
\t\tret

FUNC_000004:\t\t; Pos = 1030
\t\tmov eax,0x3333
\t\tret
"""

FFEDAT = """DATA_000076:
\tdb 0x1, 0x2
DATA_000077:
\tdb 0x0, 0x0, 0xa8, 0x5, 0x0, 0x8, 0xcc, 0x9, 0x50, 0xb, 0xa6, 0xc, 0xdb, 0xd, 0xf7, 0xe
\tdb 0x0, 0x10, 0xf8, 0x10, 0xe3, 0x11, 0xc2, 0x12, 0x98, 0x13, 0x65, 0x14, 0x2a, 0x15, 0xe8, 0x15
DATA_000088:
\tdd 0x0
"""


def matcher():
    d = Path(tempfile.mkdtemp())
    (d / "ffe.asm").write_text(FFE); (d / "ffedat.asm").write_text(FFEDAT); (d / "fe2.s").write_text(FE2)
    return fe2ref.matcher(d / "fe2.s", d)


class TestParse(unittest.TestCase):
    def test_fe2_blocks_names_and_a5_calls(self):
        by = {b.label: b for b in fe2ref.parse_fe2(FE2)}
        self.assertEqual(by["L300_RandInt"].name, "RandInt")
        self.assertEqual(by["L300_RandInt"].addr, 0x300)
        self.assertIsNone(by["L400"].name)
        self.assertEqual(sorted(by["L400"].calls), ["L200_Sqrt", "L300_RandInt"])   # a5 slot 6 resolved
        self.assertFalse(by["L900_sqrt_table"].is_code)
        self.assertEqual(bytes(by["L900_sqrt_table"].data[:4]), b"\x00\x00\x05\xa8")

    def test_table_found_after_endian_swap(self):
        blocks = fe2ref.parse_fe2(FE2)
        blob, starts = fe2ref.parse_ffedat(FFEDAT)
        self.assertEqual(fe2ref.match_tables(blocks, blob, starts), {"L900_sqrt_table": 77})

    def test_word_constants_fold_alike(self):
        self.assertEqual(fe2ref._canon(0xfffe, word=True), -2)
        self.assertEqual(fe2ref._canon(0xfffffffe), -2)


class TestMatch(unittest.TestCase):
    def test_anchors_and_propagation(self):
        m = matcher()
        self.assertEqual(m.data_anchors(), {1: "L200_Sqrt"})
        self.assertEqual(m.const_anchors(min_strength=0.1).get(2), "L300_RandInt")
        M = m.propagate({1: "L200_Sqrt", 2: "L300_RandInt"})
        self.assertEqual(M.get(3), "L400")                 # caller of both anchors
        self.assertNotIn(4, M)                             # nothing links it

    def test_store_is_facts_not_evidence(self):
        con = fresh_db()
        rows = [{"func": 2, "ref": "glfrontier", "ref_label": "L300_RandInt", "ref_name": "RandInt",
                 "ref_addr": 0x300, "how": "const", "csim": 0.9, "confidence": 0.8}]
        self.assertEqual(fe2ref.store(con, rows), 1)
        self.assertEqual(fe2ref.store(con, rows), 1)      # a re-run replaces, doesn't add
        self.assertEqual(fe2ref.matches_for(con, 2)[0]["ref_name"], "RandInt")
        self.assertEqual(con.execute("SELECT COUNT(*) AS n FROM evidence").fetchone()["n"], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) AS n FROM bets").fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
