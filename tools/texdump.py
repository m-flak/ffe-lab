"""Dump FFE bitmaps/textures as PNGs. Stdlib only, no install.

Usage:
  python3 tools/texdump.py 50                  # one bitmap -> ./texdump_out/
  python3 tools/texdump.py all                 # all 357 bitmaps
  python3 tools/texdump.py all --src ~/src/ffed3d --out /tmp/tex

Format (see docs/ASSET_FORMATS.md for the full story):
  Table: DATA_008240 (96 entries) + DATA_008241 (261 entries) in ffebmp.asm,
  one contiguous 357-entry pointer table, indices 0-356.
  Record: byte0 = flags (bit0 = masked/transparent), byte1 = height,
  word@+2 = width, pixels @+8, 8-bit indexed; the record is padded with
  zeros so its total length is a multiple of 4. Indices 94/95 are 64x512
  planet strips: height 512 overflows the height byte, so it reads 0 and
  the true height is derived from the record length.
  Engine reference: FUNC_001620_BmpIndexToPtr, FUNC_001625_BlitClipInternal
  (texel 0 skipped = transparent), FUNC_001643_SelectTexFunc (width 64 ->
  Tex64 else Tex128; flags&1 -> Trans variant), Tex inner loops
  (final = LUT[texel], LUT = runtime DATA_009252, resb 256).

Rendering (static dump; the true palette lives in the VGA DAC at runtime):
  grayscale; texel 0 -> magenta (the mask) on masked (flags&1) images,
  opaque darkest shade otherwise; values scaled per image to 0-255 so the
  3-bit shade texels (raw values 0-7, nearly black unscaled) are visible.
"""
import argparse
import os
import re
import struct
import sys
import zlib
from collections import Counter

LABEL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_$.]*):")


def parse_value(tok):
    tok = tok.strip()
    if tok.lower().startswith("0x"):
        return int(tok, 16)
    if tok.isdigit():
        return int(tok)
    if tok.lower().endswith("h") and all(c in "0123456789abcdefABCDEF" for c in tok[:-1]):
        return int(tok[:-1], 16)
    return None  # a label ref or expression


def parse_asm(path):
    """label -> {'bytes': bytearray of db/dw/dd literals, 'refs': [(offset, token)]}."""
    labels, order = {}, []
    cur = None
    with open(path, "r", encoding="latin-1") as f:
        for raw in f:
            line = raw.split(";", 1)[0].strip()
            if not line:
                continue
            m = LABEL_RE.match(line)
            if m:
                cur = m.group(1)
                labels[cur] = {"bytes": bytearray(), "refs": []}
                order.append(cur)
                line = line[m.end():].strip()
                if not line:
                    continue
            if cur is None:
                continue
            low = line.lower()
            if low.startswith("db "):
                for tok in line[3:].split(","):
                    v = parse_value(tok)
                    if v is None:
                        raise ValueError(f"non-literal db token: {tok!r} in {cur}")
                    labels[cur]["bytes"].append(v & 0xFF)
            elif low.startswith("dw "):
                for tok in line[3:].split(","):
                    v = parse_value(tok)
                    labels[cur]["bytes"] += struct.pack("<H", (v or 0) & 0xFFFF)
                    if v is None:
                        labels[cur]["refs"].append((len(labels[cur]["bytes"]) - 2, tok.strip()))
            elif low.startswith("dd "):
                for tok in line[3:].split(","):
                    v = parse_value(tok)
                    off = len(labels[cur]["bytes"])
                    labels[cur]["bytes"] += struct.pack("<I", (v or 0) & 0xFFFFFFFF)
                    if v is None:
                        labels[cur]["refs"].append((off, tok.strip()))
            elif low.startswith(("global", "extern", "section", "bits")):
                continue
            elif (m2 := re.match(r"res([bwd])\s+(\d+)$", low)):
                labels[cur]["bytes"] += b"\x00" * (int(m2.group(2)) * {"b": 1, "w": 2, "d": 4}[m2.group(1)])
                continue
            else:
                raise ValueError(f"unparsed line in {cur}: {line!r}")
    return labels, order


def load(ffebmp):
    labels, order = parse_asm(ffebmp)
    flat = bytearray()
    off = {}
    for lab in order:
        off[lab] = len(flat)
        flat += labels[lab]["bytes"]
    return labels, off, bytes(flat)


def bmp_table(labels):
    """The 357 bitmap pointers: DATA_008240 ++ DATA_008241."""
    t1 = [t for _, t in labels["DATA_008240"]["refs"]]
    t2 = [t for _, t in labels["DATA_008241"]["refs"]]
    return t1 + t2


def decode_bitmap(labels, blob):
    """Header + pixels for one bitmap label. Trailing dword pad is ignored."""
    b = labels[blob]["bytes"]
    if len(b) < 8:
        raise ValueError(f"{blob}: record too short ({len(b)} bytes)")
    flags = b[0]
    w = struct.unpack("<H", b[2:4])[0]
    h = b[1]
    if h == 0:
        # Height overflowed the byte (the 64x512 planet strips at idx 94/95):
        # derive it from the record length.
        h = (len(b) - 8) // w
    if len(b) < 8 + w * h:
        raise ValueError(f"{blob}: {w}x{h} needs {w * h} pixels, has {len(b) - 8}")
    return {"flags": flags, "w": w, "h": h, "pixels": bytes(b[8:8 + w * h]),
            "pad": len(b) - (8 + w * h)}


def render_rgba(px, masked):
    """Grayscale RGBA: texel 0 magenta iff masked, values scaled to 0-255."""
    vmax = max(px) if px else 0
    out = bytearray()
    for v in px:
        if masked and v == 0:
            out += b"\xff\x00\xff\xff"
        else:
            g = round(v * 255 / vmax) if vmax else 0
            out += bytes((g, g, g, 255))
    return bytes(out)


def write_png(path, w, h, rgba):
    """Minimal stdlib PNG writer: 8-bit RGBA, one IDAT."""
    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data +
                struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", zlib.compress(raw)))
        f.write(chunk(b"IEND", b""))


def dump_bitmap(labels, out_dir, idx, blob):
    """Decode one bitmap, write its PNG, return stats (no print)."""
    os.makedirs(out_dir, exist_ok=True)
    d = decode_bitmap(labels, blob)
    w, h, px = d["w"], d["h"], d["pixels"]
    masked = bool(d["flags"] & 1)
    path = f"{out_dir}/bmp{idx:03d}_{w}x{h}_{blob}.png"
    write_png(path, w, h, render_rgba(px, masked))
    return {"idx": idx, "blob": blob, "flags": d["flags"], "w": w, "h": h,
            "masked": masked, "vmax": max(px), "zeros": px.count(0),
            "pad": d["pad"], "path": path}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Dump FFE bitmaps/textures as PNG files.")
    ap.add_argument("bmp", help="bitmap index (0-356) or 'all'")
    ap.add_argument("--src", default=os.path.expanduser("~/src/ffed3d"),
                    help="ffed3d checkout dir (default ~/src/ffed3d)")
    ap.add_argument("--out", default="texdump_out", help="output dir (default ./texdump_out)")
    a = ap.parse_args(argv)
    labels, _, _ = load(os.path.join(a.src, "ffebmp.asm"))
    trefs = bmp_table(labels)
    print(f"bitmap table: {len(trefs)} entries")

    if a.bmp == "all":
        n, sizes, flags = 0, Counter(), Counter()
        for i, blob in enumerate(trefs):
            try:
                st = dump_bitmap(labels, a.out, i, blob)
            except Exception as e:
                print(f"bmp {i} {blob}: ERROR {e}")
                continue
            n += 1
            sizes[(st["w"], st["h"])] += 1
            flags[st["flags"]] += 1
        print(f"wrote {n} PNGs to {a.out}")
        print("sizes:", dict(sizes.most_common(8)))
        print("flags:", dict(sorted(flags.items())))
        return

    idx = int(a.bmp)
    st = dump_bitmap(labels, a.out, idx, trefs[idx])
    print(f"bmp {idx} {st['blob']}: {st['w']}x{st['h']} flags={st['flags']} "
          f"masked={st['masked']} vmax={st['vmax']} zeros={st['zeros']} pad={st['pad']}")
    print("wrote", st["path"])


if __name__ == "__main__":
    sys.exit(main())
