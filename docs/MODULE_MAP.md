# FFE module map (generated)

Recovered source-file segments of `ffe.asm`, in original link order. Regenerate with `ffelab build`. Boundary confidence is the model's probability that a file boundary sits immediately before the segment.

## Model quality

- Functions in address order: 1610
- Module event tables used as anchors: 13
- Labelled name pairs: 242 (26 straddle a family change)
- Cross-validated AUC on held-out name pairs: **0.805**
- In-sample pair fit at threshold 0.1404: {'precision': 0.545, 'recall': 0.462, 'f1': 0.5}
- Boundaries chosen: 48

Per-feature AUC (how well each signal alone separates boundaries):

| feature | AUC | weight |
|---|---|---|
| pad | 0.555 | -0.305 |
| align_shift | 0.552 | -0.017 |
| frame_shift | 0.674 | 0.385 |
| call_clean | 0.817 | 0.953 |
| data_shift | 0.687 | 0.179 |
| in_anchor | 0.488 | -0.854 |

## Event tables (module anchors)

| table | comment | slots (event → FUNC) |
|---|---|---|
| DATA_001700 | Mission event function table | 0→57, 2→59, 10→60, 11→61 |
| DATA_002285 | Journal event function table | 0→91, 1→83, 2→90, 3→81, 8→72 |
| DATA_004962 | Event function table for SysMenu | 0→218, 1→224, 3→209, 4→216, 8→227, 10→232 |
| DATA_005038 | — | 2→233, 3→240, 10→238 |
| DATA_005054 | — | 0→242, 2→243, 5→254, 11→256 |
| DATA_005091 | — | 0→259, 2→258, 3→273 |
| DATA_005105 | — | 0→280, 1→285, 2→296, 3→292, 6→278, 8→281, 10→302 |
| DATA_006163 | Starport module event functions | 0→616, 2→648, 5→647, 6→660 |
| DATA_006254 | — | 0→687, 2→673, 5→705, 6→707, 11→779 |
| DATA_006798 | Event function table for system stuff | 0→829, 1→834, 2→839, 3→840, 4→828, 8→864 |
| DATA_007104 | — | 5→866, 6→867 |
| DATA_007230 | — | 0→959, 4→962 |
| DATA_007239 | — | 0→963, 1→966, 3→982, 4→977, 5→985, 8→964 |

## Segments

| # | FUNC range | Pos range | size | boundary conf | label | named examples |
|---|---|---|---|---|---|---|
| 0 | 20–67 | 0x11464–0x148b4 | 48 | — | Mission event function table | Unknown, GetSpecialShips, BeginEvents |
| 1 | 68–126 | 0x149ac–0x18dec | 59 | 0.089 | Journal event function table | ExpandBracketCodes |
| 2 | 127–129 | 0x18f30–0x18fcc | 3 | 0.37 | ? | — |
| 3 | 130–131 | 0x19244–0x19254 | 2 | 0.594 | ? | — |
| 4 | 132–133 | 0x1926c–0x1927c | 2 | 0.395 | ? | — |
| 5 | 134–143 | 0x19284–0x192f0 | 10 | 0.223 | ? | — |
| 6 | 144–232 | 0x1934c–0x1c010 | 74 | 0.375 | Event function table for SysMenu | Unknown, CdrFileStrChrNon, CdrFileExpandZeros, CdrFileCompressZeros |
| 7 | 233–240 | 0x1c04c–0x1c721 | 8 | 0.121 | DATA_005038 | — |
| 8 | 241–257 | 0x1c79d–0x1d224 | 17 | 0.044 | DATA_005054 | — |
| 9 | 258–275 | 0x1d248–0x1dfb0 | 18 | 0.133 | DATA_005091 | ConsoleDrawButtons, ConsoleSetButtonImage, ConsoleShowButton, ConsoleHideButton |
| 10 | 276–567 | 0x1e234–0x27414 | 292 | 0.121 | DATA_005105 | AddFederalRank, AddImperialRank, ShowCommMessage, GetEquipmentAmount |
| 11 | 568–661 | 0x27438–0x2cd2c | 94 | 0.12 | Starport module event functions | FindOppositionSystem, AddToShipyard |
| 12 | 662–803 | 0x2cd3e–0x314b4 | 142 | 0.096 | DATA_006254 | SpawnTraders, SpawnHSTrader, SpawnHSTrader2, SpawnDockedTrader |
| 13 | 804–864 | 0x31500–0x3716c | 61 | 0.121 | Event function table for system stuff | GetNumstars, GetNeighborSystem, GetSysGenParams |
| 14 | 865–897 | 0x371d4–0x3b6b4 | 33 | 0.04 | DATA_007104 | NoOverride, GetSystemDataExt, GetSystemDataExt2, GetSysObjectData |
| 15 | 898–953 | 0x3b964–0x3e288 | 55 | 0.147 | Spawn | DestroyObject, SpawnCargo, DestroyEquip, TakeDamage |
| 16 | 954–962 | 0x3e43c–0x3e88c | 9 | 0.149 | DATA_007230 | — |
| 17 | 963–1125 | 0x3ec44–0x462a8 | 163 | 0.075 | DATA_007239 | nothing |
| 18 | 1326–1331 | 0x4e384–0x4ea8c | 6 | 0.183 | DPal | DPal32to16, DPal16toPal, DPalClear, DPalBuild |
| 19 | 1332–1401 | 0x4eb18–0x4f83c | 70 | 0.142 | String | Int64Abs, Int64Add64, Int64Add32, Int64Sub64 |
| 20 | 1402–1406 | 0x4f860–0x4f974 | 2 | 0.148 | String | StringDrawWrapPersist, KeybGetKeyState |
| 21 | 1407–1408 | 0x4f9a0–0x4fa8c | 2 | 0.405 | Keyb | KeybInit, KeybCleanup |
| 22 | 1409–1438 | 0x4fb44–0x509e4 | 10 | 0.212 | Gui | GuiGetLastActionInternal, GuiSetLastActionInternal, GuiGetLastAction, GuiSetLastAction |
| 23 | 1439–1440 | 0x509f0–0x50a2c | 2 | 0.202 | Gui | GuiSetConsoleHotAreas, GuiAddHotAreas |
| 24 | 1441–1442 | 0x50a68–0x50ac8 | 2 | 0.24 | Gui | GuiAddHotRect, GuiAddHotSquare |
| 25 | 1443–1449 | 0x50ae8–0x50c8c | 7 | 0.142 | ScrnShot | GuiRemoveHotArea, GuiSetXYAccum, GuiGetXYAccum, ScrnShotSave |
| 26 | 1456–1467 | 0x50f38–0x5133f | 7 | 0.149 | Gui | FileDelete, GuiAddHotRect2 |
| 27 | 1468–1521 | 0x513f1–0x537b0 | 54 | 0.148 | ? | getVertex, getVar, getRadius, fp |
| 28 | 1522–1523 | 0x537c1–0x537cd | 2 | 0.236 | ? | — |
| 29 | 1524–1525 | 0x537f4–0x53814 | 2 | 0.449 | ? | TogglePtr, nothing |
| 30 | 1526–1528 | 0x53818–0x5397c | 3 | 0.198 | ? | nothing, SysInit, SysCleanup |
| 31 | 1529–1530 | 0x539e4–0x539f4 | 2 | 0.142 | ? | SysExit |
| 32 | 1531–1568 | 0x53a34–0x550a4 | 37 | 0.424 | ? | GetModelInstancePtr, GetModelPtr, GetModelPtr2 |
| 33 | 1569–1571 | 0x550d0–0x551ac | 3 | 0.22 | Draw | DrawScannerStalk, DrawUpStalk, DrawDownStalk |
| 34 | 1572–1578 | 0x551f0–0x552f4 | 7 | 0.34 | ? | ClearBuf |
| 35 | 1579–1580 | 0x55340–0x553b4 | 2 | 0.16 | ? | PersistBlit |
| 36 | 1581–1610 | 0x554a4–0x5723c | 23 | 0.438 | Draw | PersistTextWrite, TextWriteStringOnly, TextWriteDefaultColor, TextWriteDefaultClip |
| 37 | 1611–1616 | 0x572ec–0x57abc | 4 | 0.166 | Draw | DrawParticle, DrawCircle, DrawSpans, DrawLine |
| 38 | 1617–1619 | 0x57d34–0x57da4 | 3 | 0.182 | Blit | BlitVidToBuf, ClearUpperBuf, ZeroUpperBuf |
| 39 | 1620–1627 | 0x57dbc–0x58010 | 8 | 0.189 | Blit | BmpIndexToPtr, BlitIndexToBuf, BlitClipIndexToBuf, BlitIndex |
| 40 | 1628–1629 | 0x58058–0x580b8 | 2 | 0.302 | Draw | DrawBoxToBufMasked, DrawPixelToBuf |
| 41 | 1630–1641 | 0x580d8–0x58614 | 8 | 0.463 | Video | EnablePtr, DisablePtr, UpdatePtr, SetPtrImage |
| 42 | 1642–1776 | 0x586c8–0x6125c | 132 | 0.614 | Vec64 | GenerateTexSteps, SelectTexFunc, DrawTexTriangle, DrawTexTriangleDet |
| 43 | 1777–1787 | 0x61430–0x61674 | 11 | 0.189 | ? | — |
| 44 | 1788–1790 | 0x616b0–0x617f4 | 3 | 0.178 | ? | — |
| 45 | 1791–1873 | 0x61818–0x6c600 | 83 | 0.164 | ? | — |
| 46 | 1874–1879 | 0x6d43c–0x6f8f4 | 6 | 0.141 | ? | — |
| 47 | 1880–1881 | 0x6fb7c–0x6fc7a | 2 | 0.391 | Video | VideoInit, VideoBlitBuffer |
| 48 | 1883–2095 | 0x6fcce–0x76414 | 15 | 0.175 | Sound | VideoCleanup, SoundInit, SoundPlaySong, SoundStopSong |

## Named-block integrity

Contiguous runs of same-family names and how many chosen boundaries fall inside them (ideally 0; some families, e.g. Gui/Keyb/Mouse, may genuinely share or split files).

| family | named | span | boundaries inside |
|---|---|---|---|
| CdrFile | 14 | 14 | 0 |
| CdrFile | 5 | 5 | 0 |
| SysMenu | 43 | 43 | 0 |
| Console | 4 | 4 | 0 |
| Spawn | 6 | 238 | 3 |
| DPal | 5 | 6 | 0 |
| Int64 | 10 | 10 | 0 |
| String | 61 | 61 | 1 |
| Keyb | 3 | 3 | 1 |
| Gui | 4 | 4 | 0 |
| Gui | 10 | 12 | 3 |
| ScrnShot | 4 | 4 | 0 |
| Draw | 3 | 3 | 0 |
| Tex | 6 | 6 | 0 |
| Draw | 16 | 20 | 1 |
| Blit | 7 | 10 | 1 |
| Draw | 3 | 3 | 1 |
| Draw | 6 | 6 | 0 |
| Tex | 4 | 4 | 0 |
| Vec64 | 7 | 8 | 0 |
| Mat | 6 | 12 | 0 |
| Video | 3 | 3 | 1 |
| Sound | 9 | 10 | 0 |
