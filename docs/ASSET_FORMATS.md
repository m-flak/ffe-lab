# FFE asset formats (reverse-engineered)

How the game's art lives in the ffed3d asm, and how to get it out. All of this
was recovered from `ffe.asm` / `ffebmp.asm` / `ffedat.asm` plus the C++ side in
`win/` — no game copy needed. Regenerate everything with
`python3 tools/meshwalk.py all` (meshes) and `python3 tools/texdump.py all`
(bitmaps/textures).

## Bitmaps (sprites, panels, FX, textures)

- Table: `DATA_008240` (96 entries) immediately followed by `DATA_008241`
  (261 entries) in `ffebmp.asm` — one contiguous 357-entry pointer table,
  indices 0–356. `BmpIndexToPtr(i) = [i*4 + DATA_008240]`.
- Record: byte0 = flags?, byte1 = height, word@+2 = width, words @+4/@+6 =
  x/y offsets?, pixels @+8. 8-bit indexed, 0 = transparent.
  (From `BlitClipInternal` asm + `C_BlitClipInternal` in `win/gfx.cpp`.)
- Index 96 is the 320×158 cockpit backsprite; sizes cluster at 64×64 (×121),
  25×25, 30×30, 128×128 — sprites plus `Tex64`/`Tex128` textures.
- 127 textures use only values 0–7: they are 3-bit shade texels, remapped
  per material through the 256-byte runtime LUT at `DATA_009252`
  (`Tex64` inner loop: `final = LUT[texel]`). The LUT is filled by
  `FUNC_001805`/`FUNC_001807` (unidentified "material-palette builders").
- Palette is set at runtime through the VGA DAC (`DPalSet`); static dumps
  are grayscale. True color needs palette recovery.
- `tools/texdump.py` exports all 357 as PNGs: texel 0 is magenta (the
  mask) on masked (flags&1) images and the opaque darkest shade
  otherwise, and values are scaled per image to 0–255 so the 3-bit shade
  texels read as visible gray levels instead of near-black. Records are
  dword-padded; indices 94/95 are 64×512 planet strips (height byte
  reads 0).

## Meshes (465 models — not all ships)

- Table: `DATA_004681` ("Ship object table"), 465 entries. `GetModelPtr(i)`
  returns the `Model_t` for model `i`. `doc/srctools/shipidx.txt` names only
  0–71 (fighters, freighters, missiles, shuttle, capsule, 6 Thargoid
  entries); the rest are space stations, starport / planet-side-base
  buildings, and unidentified models. Largest decodable so far: #271
  (124 verts), #396/#302 (112 verts), #79 (94 verts, 62 faces) — station
  candidates. Exact station/building indices are still unknown (chosen at
  runtime from `StarportArray`).
- `Model_t`: +0 Mesh_ptr (display-list stream), +4 Vertices_ptr,
  +8 NumVertices, +12 Normals_ptr, ... (see `Model_t` in `win/ffetypes.h`).
- Vertex pool: **6-byte records per vertex pair**:
  `[type, ?, x, y, z, ?]` (signed bytes). **Odd vertices mirror x.**
  Index 255 = origin; 251–254 are runtime-computed (sub-object attach points).
  Record types: 0/1/2 = plain; 5 = negate of a vertex; 11 = midpoint of two;
  15 = parallelogram (`B+A-C`); 17 = vertex + offset; 23 = mixed components;
  7 = vertex + random jitter; 19 = var-driven morph between two vertices
  (latter two need runtime state; the dumper uses the deterministic part).
  Engine reference: `FUNC_001478` (`doc/ffed3ddb.c`); C++ mirror:
  `getTransVertex` (`win/gfx.cpp`, from Jongware's notes).
- Mesh stream: u16 tags, `op = tag & 0x1f`, dispatched through `DATA_007816`
  (32 handlers, commented in `ffedat.asm`): points, lines, triangles, quads
  + mirror variants, complex polygons, cones, splines, thruster FX, text,
  sub-objects, `SkipIf` LOD culling, SetColor, Rotate, even a Planet opcode.
  `FUNC_001772` is the walker; each handler returns the next stream position.
- Face records: tri `0x03` = color word + index bytes
  (v1,v2,v3,norm) at payload +3,+2,+5,+4 (confirmed by JJ's comments in
  `FUNC_001706`); quad `0x04` = color word + (v1..v4) at +4,+2,+5,+3 +
  trailing word. Mirror `0x07`/`0x08` = same layout with **all indices
  XOR 1** (even↔odd vertex swap = reflected twin), order kept — no
  winding flip. `0x06` ("Unified-z") is a variable-length bounds/cull
  op (sub-case in tag>>13; cases 1/3 scan a `0xff`-terminated word
  list) that emits no faces. Skips jump `tag>>5` words forward
  (signed); count 0 ends the walk.
- Nothing is stored pre-culled: `SkipIf` ops are runtime LOD/visibility
  switches, and the dumper unions all branches. Skips are forward-only,
  so shared trailing geometry is duplicated per branch in the stream —
  the dumper drops exact-duplicate faces (they otherwise poison winding
  propagation). Meshes that look open (cockpit openings, engine bells)
  are authored that way. The engine culls single-sided by stored
  per-face normal, so data winding is mixed (mirror pairs are opposite
  by construction); the dumper re-orients by edge propagation plus a
  per-component outward majority vote. `0x02` lines are emitted as OBJ
  `l` records.
- Big models are assemblies: the top stream holds LOD switches, thruster
  mounts and glow sprites, with hull pieces attached via sub-model opcodes
  through a runtime-built directory.
- Status: 114 OBJs (98 with faces), 0 bad refs, 5 degenerate tris in
  1,977; the rest need `0x05` (complex polygon), `0x0E`/`0x1B`
  (sub-objects), `0x1A` (set color), `0x0A` (text) and a few rarer opcodes.
- Verification caveat: this dump code may be wrong. Nothing here has been
  checked against the game itself yet. Before trusting any model, play the
  game and compare the OBJ against what is actually rendered.

## Fonts and text (four systems, not one)

- Small UI font — the workhorse for all 2D text (panels, menus, dialogs,
  HUD): 1bpp proportional, 10 bytes/char (`[9 rows, MSB-left][advance]`)
  at `DATA_007750` in `ffedat.asm`, 116 chars covering 0x20–0x93 (full
  ASCII plus accented lowercase for localization and a few symbols).
  Rendered by `FUNC_001598_DrawChar` via the `TextWrite*` / `StringDraw*` /
  `DrawShadowedChar` families. String control codes: `0x1` color,
  `0xd` newline, `0x1e`/`0x1f` position, `0x0` inline bitmap-by-index.
  Hand quirks: rectangular `A` (flat top, vertical sides), `4`/`7` with
  vertical stems — no standard font reproduces these.
- Big display glyphs: bitmap indices 292–326 = `A`–`Z` + `1`–`9`
  (labels run in reverse, `DATA_008032`–`DATA_007998`); 64×64 masked 3-bit
  shaded sprites, no zero (`O` doubles for it). Every glyph fills its
  square tile (~1.0em wide, all 35). Referenced only through the index
  table via computed indices — exact call site untraced, most likely
  title/logo/header lettering.
- Mesh vector fonts: models 0/1/2 = `VectorFont` / `GraphicsFont` /
  `TimesFont` (Jongware's `ShowMesh.txt`), per-character sub-models drawn
  by the `0x0A` Text opcode (`[ebx + char*4 - 0x40]`). 153 vector-font
  vs 57 Times-font uses in the dumped models; `GraphicsFont` unused there.
- Floating 3D labels use the *small* font: `0x12 DisplayText` defers to
  `FUNC_001812`, which just calls `FUNC_001583_TextWriteDefaultColor`.
- FOSS lookalikes (pixel-compared against installed references): small →
  **DejaVu Sans** (Verdana won on overlap, shape and width correlation);
  big → **Liberation Sans Bold** (Arial skeleton: pointed `A`, plain arms,
  straight-leg `R`) stretched ~120–135% horizontally with a top-lit bevel
  (the stretch also reproduces the vertical-heavy stroke contrast).
  Unmatched anywhere: the small font's rectangular `A`/`4`/`7`, the big
  face's slab feet on stems. Compare at a 5:6 horizontal squeeze — the art
  is drawn ~1.2× wide for 320×200-on-4:3 pixels.

## For the rewrite

Decode verts + faces at load (this tool), then stripify/fan-ify with modern
tooling. Do not port the software rasterizer — the display-list walker is the
only part worth understanding (~15 instructions), everything downstream of it
is 320×200-era scaffolding.
