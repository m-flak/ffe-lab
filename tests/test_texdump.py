import importlib.util
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

TD = Path(__file__).resolve().parent.parent / "tools" / "texdump.py"
spec = importlib.util.spec_from_file_location("texdump", TD)
texdump = importlib.util.module_from_spec(spec)
spec.loader.exec_module(texdump)


def labels_with(blob, raw):
    return {blob: {"bytes": bytearray(raw), "refs": []}}


class TestDecodeBitmap(unittest.TestCase):
    def test_header_and_pixels(self):
        labels = labels_with("B", bytes([1, 2, 3, 0, 0, 0, 0, 0] + [9] * 6))
        d = texdump.decode_bitmap(labels, "B")
        self.assertEqual((d["flags"], d["w"], d["h"]), (1, 3, 2))
        self.assertEqual(d["pixels"], bytes([9] * 6))
        self.assertEqual(d["pad"], 0)

    def test_dword_pad_ignored(self):
        # 3x2 = 6 pixels + 2 pad bytes to a multiple of 4
        labels = labels_with("B", bytes([1, 2, 3, 0, 0, 0, 0, 0] + [9] * 6 + [0] * 2))
        d = texdump.decode_bitmap(labels, "B")
        self.assertEqual(d["pixels"], bytes([9] * 6))
        self.assertEqual(d["pad"], 2)

    def test_zero_height_derived_from_length(self):
        # like idx 94/95: height byte 0, true height from record length
        labels = labels_with("B", bytes([0, 0, 4, 0, 0, 0, 0, 0] + [5] * 12))
        d = texdump.decode_bitmap(labels, "B")
        self.assertEqual((d["w"], d["h"]), (4, 3))
        self.assertEqual(d["pixels"], bytes([5] * 12))

    def test_short_record_raises(self):
        labels = labels_with("B", bytes([1, 2, 3, 0, 0, 0, 0, 0] + [9] * 5))
        with self.assertRaises(ValueError):
            texdump.decode_bitmap(labels, "B")


class TestRenderRgba(unittest.TestCase):
    def test_masked_zero_is_magenta(self):
        rgba = texdump.render_rgba(bytes([0, 1, 2, 3]), masked=True)
        self.assertEqual(rgba[0:4], bytes([255, 0, 255, 255]))
        # values scaled to full range: 1..3 -> 85, 170, 255
        self.assertEqual(rgba[4], 85)
        self.assertEqual(rgba[8], 170)
        self.assertEqual(rgba[12], 255)

    def test_opaque_zero_is_black(self):
        rgba = texdump.render_rgba(bytes([0, 1, 2]), masked=False)
        self.assertEqual(rgba[3::4], bytes([255, 255, 255]))
        self.assertEqual((rgba[0], rgba[4], rgba[8]), (0, 128, 255))

    def test_all_zero(self):
        self.assertEqual(texdump.render_rgba(bytes([0, 0]), masked=True),
                         b"\xff\x00\xff\xff" * 2)
        self.assertEqual(texdump.render_rgba(bytes([0, 0]), masked=False),
                         b"\x00\x00\x00\xff" * 2)

    def test_shade_texels_scaled_visible(self):
        # 3-bit texels 0-6 must spread across the gray range, not sit at 0-6
        rgba = texdump.render_rgba(bytes(range(7)), masked=False)
        grays = [rgba[i] for i in range(0, len(rgba), 4)]
        self.assertEqual(grays, [0, 42, 85, 128, 170, 212, 255])


class TestWritePng(unittest.TestCase):
    def read_chunks(self, data):
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        pos, out = 8, []
        while pos < len(data):
            (ln,) = struct.unpack(">I", data[pos:pos + 4])
            typ = data[pos + 4:pos + 8]
            body = data[pos + 8:pos + 8 + ln]
            (crc,) = struct.unpack(">I", data[pos + 8 + ln:pos + 12 + ln])
            self.assertEqual(crc, zlib.crc32(typ + body) & 0xFFFFFFFF)
            out.append((typ, body))
            pos += 12 + ln
        return out

    def test_roundtrip_pixels(self):
        rgba = texdump.render_rgba(bytes([0, 1, 2, 3]), masked=True)  # 2x2
        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "t.png")
            texdump.write_png(p, 2, 2, rgba)
            chunks = self.read_chunks(Path(p).read_bytes())
        types = [t for t, _ in chunks]
        self.assertEqual(types[0], b"IHDR")
        self.assertEqual(types[-1], b"IEND")
        w, h, depth, ctype, _, _, _ = struct.unpack(">IIBBBBB", chunks[0][1])
        self.assertEqual((w, h, depth, ctype), (2, 2, 8, 6))
        raw = zlib.decompress(b"".join(b for t, b in chunks if t == b"IDAT"))
        # each scanline: filter byte 0 + w*4 bytes
        self.assertEqual(raw, b"\x00" + rgba[0:8] + b"\x00" + rgba[8:16])


if __name__ == "__main__":
    unittest.main()
