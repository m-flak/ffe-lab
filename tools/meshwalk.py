"""Walk FFE ship display-lists, dump OBJs. Stdlib only, no install.

Usage:
  python3 tools/meshwalk.py 461                  # one ship -> ./meshwalk_out/
  python3 tools/meshwalk.py all                  # every decodable ship
  python3 tools/meshwalk.py all --src ~/src/ffed3d --out /tmp/mesh

Format (see docs/ASSET_FORMATS.md for the full story):
  Model_t: +0 Mesh_ptr (u16-tag command stream), +4 Vertices_ptr,
           +8 NumVertices. Pool = 6-byte records per vertex PAIR:
           [type, ?, x, y, z, ?]; odd verts mirror x. Index 255 = origin.
  Stream: u16 tags, op = tag & 0x1f, dispatch per DATA_007816.
  Tri 0x03 (6B): color word + idx bytes (v1,v2,v3,norm) at +3,+2,+5,+4.
  Quad 0x04 (8B): color word + idx bytes (v1..v4) at +4,+2,+5,+3 + word.
  0x07/0x08 are mirror variants (all indices XOR 1, order kept). 0x06 is a variable-length
  bounds/cull op (no faces). Skips fork both branches; count-0 ends a branch.
  The engine culls by stored per-face normal, so data winding is mixed;
  faces are re-oriented by edge propagation for viewing.
"""
import argparse
import os
import re
import struct
import sys

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


# opcode -> fixed payload bytes (0x06 and 0x00 handled specially)
FIXED = {
    0x01: 6, 0x02: 4, 0x03: 6, 0x04: 8, 0x07: 6, 0x08: 8,
    0x0B: 2, 0x0C: 2, 0x13: 2, 0x14: 2, 0x11: 12, 0x12: 2, 0x16: 8,
    0x18: 2, 0x1C: 0xB4, 0x0D: 2, 0x1D: 2, 0x09: 6,
}
NAMES = {0x00: "END", 0x01: "Ball", 0x02: "Line", 0x03: "Tri", 0x04: "Quad",
         0x05: "Polygon", 0x06: "UniZ", 0x07: "MirrorTri", 0x08: "MirrorQuad",
         0x09: "Thruster", 0x0A: "Text", 0x0B: "SkipNVis", 0x0C: "SkipVis",
         0x0D: "Math", 0x0E: "Subobj", 0x0F: "NeverUsed", 0x10: "OpenCone",
         0x11: "Cone", 0x12: "DispText", 0x13: "SkipBitClr", 0x14: "SkipBitSet",
         0x15: "Nested", 0x16: "Spline", 0x17: "Unused", 0x18: "BallArray",
         0x19: "ViewMat?", 0x1A: "SetColor", 0x1B: "ScaledSub",
         0x1C: "Rotate", 0x1D: "Math", 0x1E: "Unknown", 0x1F: "Planet?"}
SKIPS = (0x0B, 0x0C, 0x13, 0x14)

# payload-byte positions of vertex indices within a face record
TRI_ORDER = (3, 2, 5)
QUAD_ORDER = (4, 2, 5, 3)


def uniz_advance(flat, p, tag):
    """0x06 payload length (bounds/cull op, emits no faces). Sub-case = tag>>13:
    1,3: word list, words while high byte == 0xff, plus final word;
    2: tag only; 7: (tag>>5) 0x7fd/else -> +2, 0x7fe/0x7ff -> +0;
    0,4,5,6: tag only."""
    sub = tag >> 13
    if sub in (1, 3):
        n = 2
        while n < 512 and flat[p + n - 1] == 0xFF:
            n += 2
        return n
    if sub == 7:
        return 0 if (tag >> 5) in (0x7FE, 0x7FF) else 2
    return 0


def load(ffebmp):
    labels, order = parse_asm(ffebmp)
    flat = bytearray()
    off = {}
    for lab in order:
        off[lab] = len(flat)
        flat += labels[lab]["bytes"]
    return labels, off, bytes(flat)


def s8(b):
    return struct.unpack("b", bytes([b]))[0]


def decode_vert(vb, nverts, num, depth=0):
    """Model-space vertex (mirrors C++ getTransVertex / Jongware's notes).

    Pool = 6-byte records per vertex PAIR: [type, ?, x, y, z, ?].
    Odd vertices mirror x. Types 5/11/15/17/23 compute from other verts
    (coord bytes double as vertex-number operands); 7/19 need runtime
    state (jitter/var-morph) so we use the deterministic part.
    """
    if num == 255:
        return (0.0, 0.0, 0.0)
    # num == nverts allowed: mirror-overflow slot reads into the next label,
    # exactly as the engine does (labels are contiguous in the binary).
    if num > 250 or num > nverts or depth > 10:
        return None
    rec = vb[(num // 2) * 6:(num // 2) * 6 + 6]
    if len(rec) < 6:
        return None
    t = rec[0]
    x, y, z = float(s8(rec[2])), float(s8(rec[3])), float(s8(rec[4]))

    def ref(byte):
        return decode_vert(vb, nverts, byte & 0xFF, depth + 1)

    def need(*vs):
        return None if any(v is None for v in vs) else vs

    if t == 5:
        r = need(ref(rec[4]))
        if r is None:
            return None
        x, y, z = -r[0][0], -r[0][1], -r[0][2]
    elif t == 7:
        r = need(ref(rec[4]))  # + jitter at runtime
        if r is None:
            return None
        x, y, z = r[0]
    elif t == 11:
        r = need(ref(rec[3]), ref(rec[4]))
        if r is None:
            return None
        x = (r[0][0] + r[1][0]) / 2
        y = (r[0][1] + r[1][1]) / 2
        z = (r[0][2] + r[1][2]) / 2
    elif t == 15:
        r = need(ref(rec[2]), ref(rec[3]), ref(rec[4]))
        if r is None:
            return None
        x = r[1][0] + r[0][0] - r[2][0]
        y = r[1][1] + r[0][1] - r[2][1]
        z = r[1][2] + r[0][2] - r[2][2]
    elif t == 17:
        r = need(ref(rec[2]))
        if r is None:
            return None
        x, y, z = r[0][0] + y, r[0][1], r[0][2] + y
    elif t == 19:
        r = need(ref(rec[4]), ref(rec[3]))
        if r is None:
            return None
        x = (r[0][0] + r[1][0]) / 2  # var unknown: midpoint
        y = (r[0][1] + r[1][1]) / 2
        z = (r[0][2] + r[1][2]) / 2
    elif t == 23:
        r = need(ref(rec[3]), ref(rec[4]))
        if r is None:
            return None
        y, z = r[0][1], r[1][2]
    if num % 2:
        x = -x
    return (x, y, z)


def model_fields(labels, off, blob):
    b = labels[blob]["bytes"]
    refs = [t for _, t in labels[blob]["refs"]]
    u32 = lambda o: struct.unpack("<I", b[o:o + 4])[0]
    return {"mesh": refs[0], "mesh_off": off[refs[0]],
            "verts": refs[1], "verts_off": off[refs[1]],
            "nverts": u32(8), "normals": refs[2] if len(refs) > 2 else None}


def walk(flat, start, nverts, limit=100000):
    """Forking walk over all skip branches; faces unioned. Unknown op aborts."""
    ops = {}
    tris, quads, lines = [], [], []
    stack, visited = [start], set()
    status = "END"
    steps = 0
    while stack and steps < limit:
        pos = stack.pop()
        if pos in visited:
            continue
        visited.add(pos)
        tag = struct.unpack("<H", flat[pos:pos + 2])[0]
        op = tag & 0x1F
        ops[op] = ops.get(op, 0) + 1
        p = pos + 2
        if op == 0x00:
            continue
        if op == 0x02:
            lines.append((flat[p + 2], flat[p + 3]))
        if op in (0x03, 0x07):
            v = tuple(flat[p + o] for o in TRI_ORDER)
            if op == 0x07:  # mirror: XOR all indices with 1 (1706), order kept
                v = tuple(x ^ 1 for x in v)
            tris.append(v + (op == 0x07,))
        elif op in (0x04, 0x08):
            v = tuple(flat[p + o] for o in QUAD_ORDER)
            if op == 0x08:  # mirror: XOR all indices with 1 (1710), order kept
                v = tuple(x ^ 1 for x in v)
            quads.append(v + (op == 0x08,))
        if op == 0x06:
            nxt = p + uniz_advance(flat, p, tag)
        elif op in FIXED:
            nxt = p + FIXED[op]
        else:
            status = f"UNKNOWN-OP 0x{op:02X} ({NAMES.get(op)}) at +{pos - start}"
            break
        if op in SKIPS:
            count = struct.unpack("<h", struct.pack("<H", tag))[0] >> 5
            if count != 0:
                stack.append(nxt + count * 2)  # skip-taken branch
            # count == 0: skip-taken branch terminates (no enqueue)
        stack.append(nxt)  # no-skip branch
        steps += 1
    else:
        status = "END" if not stack else "LIMIT"
    return {"ops": ops, "tris": tris, "quads": quads, "lines": lines,
            "status": status, "states": len(visited)}


def orient_faces(faces, verts=None, center=None):
    """Consistent winding by propagation across shared edges, per component.

    The engine culls by stored per-face normal, so data winding is mixed
    (mirror pairs are opposite by construction). A neighbour across edge
    (u,v) must contain (v,u), else it is flipped; each component is biased
    outward by member majority vote when positions are given. Conflicts
    (interior walls sharing edges same-direction) are left as-is and counted.
    """
    edge_to = {}
    for fi, fa in enumerate(faces):
        for k in range(len(fa)):
            edge_to.setdefault((fa[k], fa[(k + 1) % len(fa)]), []).append(fi)
    flip = {}
    conflicts = 0
    for seed in range(len(faces)):
        if seed in flip:
            continue
        flip[seed] = False
        members = [seed]
        stack = [seed]
        while stack:
            fi = stack.pop()
            fa = faces[fi]
            if flip[fi]:
                fa = (fa[0],) + tuple(reversed(fa[1:]))
            for k in range(len(fa)):
                # (a,b) is fi's ORIENTED edge; nj must present (b,a):
                # original (b,a) -> no flip; original (a,b) -> flip.
                a, b = fa[k], fa[(k + 1) % len(fa)]
                for nj in edge_to.get((b, a), []):
                    if nj != fi and nj not in flip:
                        flip[nj] = False
                        members.append(nj)
                        stack.append(nj)
                    elif nj != fi:
                        conflicts += flip[nj] != False
                for nj in edge_to.get((a, b), []):
                    if nj != fi and nj not in flip:
                        flip[nj] = True
                        members.append(nj)
                        stack.append(nj)
                    elif nj != fi:
                        conflicts += flip[nj] != True
        if verts is not None and center is not None:
            # bias the whole component outward by member majority vote
            # (robust for open meshes where any single seed may mislead)
            votes = 0
            for m in members:
                mfa = faces[m]
                A, B, C = (verts[i] for i in mfa[:3])
                ux, uy, uz = B[0] - A[0], B[1] - A[1], B[2] - A[2]
                vx, vy, vz = C[0] - A[0], C[1] - A[1], C[2] - A[2]
                nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
                if flip[m]:
                    nx, ny, nz = -nx, -ny, -nz
                mx = (A[0] + B[0] + C[0]) / 3 - center[0]
                my = (A[1] + B[1] + C[1]) / 3 - center[1]
                mz = (A[2] + B[2] + C[2]) / 3 - center[2]
                votes += 1 if nx * mx + ny * my + nz * mz >= 0 else -1
            if votes < 0:
                for m in members:
                    flip[m] = not flip[m]
    out = []
    for fi, fa in enumerate(faces):
        out.append((fa[0],) + tuple(reversed(fa[1:])) if flip.get(fi) else fa)
    return out, {"flipped": sum(flip.values()), "conflicts": conflicts}


def dedupe(items):
    """Drop exact duplicates (skips are forward-only, so shared trailing
    geometry is duplicated per branch in the stream). Keeps first occurrence."""
    seen = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out, len(items) - len(out)


def dump_ship(labels, off, flat, out_dir, idx, blob):
    """Walk one ship, write OBJ if the walk completes. Index 255 = origin
    (appended); 251-254 need runtime state so faces using them are skipped."""
    os.makedirs(out_dir, exist_ok=True)
    f = model_fields(labels, off, blob)
    n = f["nverts"]
    # +6 bytes of flat space: the engine reads pair records contiguously,
    # so a mirror-overflow odd slot borrows the next label's first bytes.
    vb = flat[f["verts_off"]:f["verts_off"] + 3 * n + 6]
    verts = [decode_vert(vb, n, j) for j in range(n + 1)]
    r = walk(flat, f["mesh_off"], n)
    uses_origin = any(255 in t[:3] for t in r["tris"]) or any(255 in q[:4] for q in r["quads"])
    uses_over = any(n in t[:3] for t in r["tris"]) or any(n in q[:4] for q in r["quads"])
    origin_idx = n + (1 if uses_over else 0)  # 0-based slot appended below
    skipped_rt = 0

    def conv(face):
        nonlocal skipped_rt
        out = []
        for v in face:
            if v == 255:
                out.append(origin_idx)
            elif v > 250 or v > n or verts[v] is None:
                skipped_rt += 1
                return None
            else:
                out.append(v)
        return tuple(out)

    tris = [c for t in r["tris"] if (c := conv(t[:3]))]
    quads = [c for q in r["quads"] if (c := conv(q[:4]))]
    lines = [c for ln in r["lines"] if (c := conv(ln))]
    tris, dt = dedupe(tris)
    quads, dq = dedupe(quads)
    lines, dl = dedupe(lines)
    if r["status"] != "END" or not (tris or quads or lines):
        return {"idx": idx, "blob": blob, "status": r["status"], "ops": r["ops"],
                "tris": len(tris), "quads": len(quads), "lines": len(lines),
                "skipped_runtime": skipped_rt, "uses_origin": uses_origin,
                "orient": {}, "dupes": dt + dq + dl, "written": False}
    okv = [v if v else (0.0, 0.0, 0.0) for v in verts[:n]]
    if uses_over:
        okv.append(verts[n] if verts[n] else (0.0, 0.0, 0.0))
    okv += [(0.0, 0.0, 0.0)] if uses_origin else []
    center = (sum(v[0] for v in okv) / len(okv), sum(v[1] for v in okv) / len(okv),
              sum(v[2] for v in okv) / len(okv))
    oriented, ostats = orient_faces(tris + quads, okv, center)
    tris, quads = [fa for fa in oriented if len(fa) == 3], [fa for fa in oriented if len(fa) == 4]
    with open(f"{out_dir}/ship{idx:03d}_{blob}.obj", "w") as o:
        o.write(f"# FFE ship {idx} ({blob}), {n} verts, stream {f['mesh']}\n")
        for v in verts[:n]:
            o.write(f"v {v[0]} {v[1]} {v[2]}\n" if v else "v 0 0 0  # UNRESOLVED\n")
        if uses_over:
            v = verts[n]
            o.write(f"v {v[0]} {v[1]} {v[2]}\n" if v else "v 0 0 0  # UNRESOLVED\n")
        if uses_origin:
            o.write("v 0 0 0  # origin (index 255)\n")
        for a, b, c in tris:
            o.write(f"f {a + 1} {b + 1} {c + 1}\n")
        for a, b, c, d in quads:
            o.write(f"f {a + 1} {b + 1} {c + 1} {d + 1}\n")
        for a, b in lines:
            o.write(f"l {a + 1} {b + 1}\n")
    return {"idx": idx, "blob": blob, "status": r["status"], "ops": r["ops"],
            "tris": len(tris), "quads": len(quads), "lines": len(lines),
            "skipped_runtime": skipped_rt, "uses_origin": uses_origin,
            "orient": ostats, "dupes": dt + dq + dl, "written": True}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Dump FFE ship meshes as OBJ files.")
    ap.add_argument("ship", help="ship index (0-464) or 'all'")
    ap.add_argument("--src", default=os.path.expanduser("~/src/ffed3d"),
                    help="ffed3d checkout dir (default ~/src/ffed3d)")
    ap.add_argument("--out", default="meshwalk_out", help="output dir (default ./meshwalk_out)")
    a = ap.parse_args(argv)
    labels, off, flat = load(os.path.join(a.src, "ffebmp.asm"))
    print(f"flat space: {len(flat)} bytes, {len(off)} labels")
    trefs = [t for _, t in labels["DATA_004681"]["refs"]]
    if a.ship == "all":
        n = 0
        for i, blob in enumerate(trefs):
            try:
                st = dump_ship(labels, off, flat, a.out, i, blob)
            except Exception as e:
                print(f"ship {i} {blob}: ERROR {e}")
                continue
            n += st.get("written", False)
        print(f"wrote {n} OBJs to {a.out}")
        return
    idx = int(a.ship)
    st = dump_ship(labels, off, flat, a.out, idx, trefs[idx])
    print(f"ship {idx} {trefs[idx]}: status={st['status']} tris={st['tris']} quads={st['quads']} "
          f"lines={st.get('lines', 0)} dupes={st.get('dupes', 0)} skipped_rt={st['skipped_runtime']} "
          f"origin={st['uses_origin']} orient={st['orient']}")
    print("ops:", {f"0x{k:02X}({NAMES.get(k, '?')})": v for k, v in sorted(st["ops"].items())})


if __name__ == "__main__":
    main()
