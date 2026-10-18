import importlib.util
import struct
import unittest
from pathlib import Path

MW = Path(__file__).resolve().parent.parent / "tools" / "meshwalk.py"
spec = importlib.util.spec_from_file_location("meshwalk", MW)
meshwalk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(meshwalk)


def tag(op, count=0):
    return struct.pack("<H", op | (count << 5))


class TestDecodeVert(unittest.TestCase):
    def test_plain_pair_and_odd_mirror(self):
        # one 6-byte record, type 1, coords (10, 20, 30)
        vb = bytes([1, 0, 10, 20, 30, 0])
        self.assertEqual(meshwalk.decode_vert(vb, 2, 0), (10.0, 20.0, 30.0))
        self.assertEqual(meshwalk.decode_vert(vb, 2, 1), (-10.0, 20.0, 30.0))

    def test_midpoint_type(self):
        # type 11: midpoint of verts 0 and 2 (coord bytes double as operands)
        vb = bytes([1, 0, 0, 0, 0, 0,
                    1, 0, 10, 20, 30, 0,
                    11, 0, 0, 0, 2, 0])
        self.assertEqual(meshwalk.decode_vert(vb, 6, 4), (5.0, 10.0, 15.0))

    def test_origin_and_runtime(self):
        vb = bytes([1, 0, 10, 20, 30, 0])
        self.assertEqual(meshwalk.decode_vert(vb, 2, 255), (0.0, 0.0, 0.0))
        self.assertIsNone(meshwalk.decode_vert(vb, 2, 251))


class TestWalk(unittest.TestCase):
    def test_tri_mirror_line_end(self):
        flat = bytearray()
        flat += tag(0x03) + bytes([0, 0, 0x0A, 0x0B, 0x0C, 0x0D])
        flat += tag(0x07) + bytes([0, 0, 0x0A, 0x0B, 0x0C, 0x0D])
        flat += tag(0x02) + bytes([0, 0, 0x01, 0x02])
        flat += tag(0x00)
        r = meshwalk.walk(bytes(flat), 0, 32)
        self.assertEqual(r["status"], "END")
        # TRI_ORDER=(3,2,5): (0x0B, 0x0A, 0x0D); mirror XORs each with 1
        self.assertEqual(r["tris"], [(11, 10, 13, False), (10, 11, 12, True)])
        self.assertEqual(r["lines"], [(1, 2)])

    def test_skip_forks_both_branches(self):
        # skip count 1 word: taken branch jumps over the tri to END;
        # no-skip branch emits the tri. Union has the tri; walk ends.
        flat = bytearray()
        flat += tag(0x14, 1) + bytes([0, 0])          # SkipIfBitSet +2B payload
        flat += tag(0x03) + bytes([0, 0, 1, 2, 3, 4])  # tri (skipped when taken)
        flat += tag(0x00)
        r = meshwalk.walk(bytes(flat), 0, 32)
        self.assertEqual(r["status"], "END")
        self.assertEqual(len(r["tris"]), 1)

    def test_unknown_op_aborts(self):
        flat = tag(0x0E) + bytes([0, 0]) + tag(0x00)
        r = meshwalk.walk(bytes(flat), 0, 32)
        self.assertTrue(r["status"].startswith("UNKNOWN-OP 0x0E"))


class TestUnizAdvance(unittest.TestCase):
    def test_case2_tag_only(self):
        self.assertEqual(meshwalk.uniz_advance(bytes(8), 0, 0x4006), 0)

    def test_case1_word_list(self):
        # words while high byte == 0xff, plus final word
        flat = bytes([1, 0xFF, 2, 0xFF, 3, 0x00])
        self.assertEqual(meshwalk.uniz_advance(flat, 0, 0x2006), 6)


class TestDedupeOrient(unittest.TestCase):
    def test_dedupe_keeps_first(self):
        out, n = meshwalk.dedupe([(1, 2, 3), (4, 5, 6), (1, 2, 3)])
        self.assertEqual(out, [(1, 2, 3), (4, 5, 6)])
        self.assertEqual(n, 1)

    def test_orient_shared_edge(self):
        # two tris sharing edge (1,2) in the same direction: one must flip
        faces = [(0, 1, 2), (1, 2, 3)]
        out, st = meshwalk.orient_faces(faces)
        self.assertEqual(out, [(0, 1, 2), (1, 3, 2)])


if __name__ == "__main__":
    unittest.main()
