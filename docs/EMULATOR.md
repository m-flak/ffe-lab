# Running FFE in an emulator on the Mac

This is the step-by-step for emulator bring-up (PLAN "Emulator bring-up",
GETTING_STARTED Stage 2): get `FIRSTENC.EXE` running, find `code_base`, make
the tagged save states, and point ffelab at the real game. Nothing here has
run against the real game yet, so expect to adjust a timing or a key here and
there, and tell Claude what broke.

## Which emulator: QEMU + FreeDOS

Use **QEMU** for now. It is the only emulator ffelab can drive end to end today.

| What ffelab needs | QEMU | DOSBox-X |
|---|---|---|
| Breakpoints, registers, memory (GDB stub) | built in (`-gdb`) | **not in DOSBox-X itself**; only third-party forks you build from source |
| Save states by name | `savevm NAME` / `loadvm NAME` on the monitor | 100 numbered slots behind hotkeys |
| Key presses | monitor `sendkey`, works with the window in the background | macOS keystrokes to the focused window (needs Accessibility permission) |
| Screenshots | monitor `screendump` of the game screen only | `screencapture` of the whole Mac screen |

Save states are actually easier in QEMU than in DOSBox-X once ffelab is
involved: you play to a spot and run one command,
`ffelab scenario save docked --tags docked`, which saves the snapshot under
that name and tags it in `ffe.db` in one go.

DOSBox-X's own debugger is interactive only (you type commands into its debug
window). It has no network port a program can talk to, so every experiment
(logging pass, halt, knockout) would fail. The `dosbox-x` backend in
`harness/emulator.py` assumes a GDB stub that stock DOSBox-X doesn't have. The
end of this page says what adding DOSBox-X would take, if QEMU can't run the game.

The one real risk with QEMU is compatibility: FFE has never been tried under
FreeDOS in QEMU here. If it won't start or sound is broken, see
[Troubleshooting](#troubleshooting).

## What you need

- A Mac (Apple Silicon is fine: QEMU emulates the x86 CPU in software, and a
  1995 DOS game is light work for it).
- Homebrew, and the ffelab venv from GETTING_STARTED (`pip install -e .`).
- Your FFE game folder with `FIRSTENC.EXE` in it, and `ffe.db` built.
- About 1 GB of disk.

Everything below lives in one folder. These steps use `~/ffe-emu`:

```
~/ffe-emu/
  FD14LIVE.iso      FreeDOS installer CD
  freedos.qcow2     the virtual hard disk (FreeDOS, the game, and all save states)
  game/             a copy of your FFE folder (only used once, to copy it in)
  run-ffe.sh        the one command line you always start QEMU with
```

## 1. Install QEMU

```bash
brew install qemu
qemu-system-i386 --version
```

## 2. Install FreeDOS on a virtual disk

1. Download the FreeDOS 1.4 **LiveCD** zip from https://www.freedos.org/download/
   and unzip it into `~/ffe-emu`. You want `FD14LIVE.iso`.
2. Create the disk and boot the installer:

   ```bash
   cd ~/ffe-emu
   qemu-img create -f qcow2 freedos.qcow2 500M
   qemu-system-i386 -m 32 -cpu pentium -hda freedos.qcow2 -cdrom FD14LIVE.iso -boot d
   ```

   The disk must be **qcow2**: QEMU keeps its save states inside that file.
3. In the installer: pick your language and keyboard, choose "Yes, continue
   with the installation", let it partition drive C: (it reboots; choose
   install again), and choose the plain package set. Unpacking can look stuck
   for a minute; it isn't.
4. When it says it's finished, choose "Return to DOS" and type `shutdown`
   (or close the window).

## 3. Copy the game onto the virtual disk

QEMU can show a Mac folder to DOS as a read-only drive. Use that once to copy
the game onto C:.

```bash
mkdir -p ~/ffe-emu/game
cp -R /path/to/your/FFE/* ~/ffe-emu/game/
cd ~/ffe-emu
qemu-system-i386 -m 32 -cpu pentium -hda freedos.qcow2 \
  -drive file=fat:$HOME/ffe-emu/game,format=raw,if=ide,index=1 -boot c
```

In DOS (pick the default boot menu entry):

```
D:
DIR
XCOPY D:\*.* C:\FFE\ /S /E
ATTRIB -R C:\FFE\*.* /S
SHUTDOWN
```

If `D:` doesn't show your files, run `DIR` on `E:` and `F:` too; the letter
depends on what FreeDOS mounts. Fallback if the folder drive won't mount: make
a CD image with `hdiutil makehybrid -iso -joliet -o game.iso ~/ffe-emu/game`,
attach it with `-cdrom game.iso`, and copy from the CD drive instead.

## 4. The run script (use it every time)

A QEMU save state only loads into the *same* virtual hardware it was saved
from. So always start QEMU with this script, and don't change its device lines
once you've made save states.

`~/ffe-emu/run-ffe.sh`:

```bash
#!/bin/sh
cd "$(dirname "$0")"
exec qemu-system-i386 \
  -m 32 -cpu pentium -rtc base=localtime \
  -hda freedos.qcow2 -boot c \
  -audiodev coreaudio,id=snd -machine pcspk-audiodev=snd \
  -device sb16,audiodev=snd -device adlib,audiodev=snd \
  -gdb tcp:127.0.0.1:1234 \
  -monitor tcp:127.0.0.1:4444,server,nowait \
  -display cocoa
```

```bash
chmod +x ~/ffe-emu/run-ffe.sh
~/ffe-emu/run-ffe.sh
```

What the lines do:

- `-gdb tcp:127.0.0.1:1234` is the debugger port ffelab uses for breakpoints,
  registers and memory. While something is attached to it, the game is paused
  between ffelab's commands.
- `-monitor tcp:127.0.0.1:4444,...` is QEMU's control console on a local port.
  ffelab uses it to save and load states, press keys and take screenshots.
  `ffelab emu monitor "COMMAND"` sends one command to it by hand.
- `sb16` and `adlib` are Sound Blaster 16 and AdLib cards. If QEMU says a device
  isn't found, delete that `-device` line (before you make any save states).

Mouse: click in the window to capture the mouse; **Ctrl+Option+G** releases it.

## 5. Start the game

In DOS:

```
CD \FFE
FIRSTENC
```

If the game has a setup or install program for sound, pick **Sound Blaster 16,
port 220, IRQ 5, DMA 1, high DMA 5** (the `sb16` defaults). If it crashes on
start, see [Troubleshooting](#troubleshooting).

## 6. Check the exe (if you haven't)

On the Mac, from the ffe-lab folder:

```bash
ffelab exe-check ~/ffe-emu/game/FIRSTENC.EXE
```

Note the `binary_id` it prints. It must be the same file you copied into the
VM. See GETTING_STARTED step 0 for what the verdicts mean.

## 7. Find `code_base`

`code_base` is where the game's code ends up in the emulated machine's memory.
The asm gives each function a `Pos`; the function runs at `code_base + Pos`.
ffelab finds it for you: it takes a few byte runs from your exe, searches the
running game's memory for them, then checks that dozens of functions really
start with `push ebp; mov ebp,esp` at that address.

1. In QEMU, get into the game proper (in flight or docked, not the DOS prompt).
2. On the Mac:

   ```bash
   ffelab emu calibrate --exe ~/ffe-emu/game/FIRSTENC.EXE --binary <binary_id>
   ```

   The game freezes while it searches (seconds, up to a minute), then carries on.
   You should see something like this (your numbers will differ):

   ```
   { "ok": true, "code_base": 1048576, "code_base_hex": "0x100000",
     "probes_agree": "3/3", "prologues_ok": "60/60", "eip": "0x...", "eip_in_code": true }

   FFELAB_EMU='{"binary": "<binary_id>", "code_base": 1048576, "gdb_port": 1234, "monitor_port": 4444}'
   ```

3. Read it:
   - `ok: true` means `code_base` is right. Keep the `FFELAB_EMU` line.
   - `eip_in_code: false` can just mean the game was in DOS or BIOS code at that
     instant: run calibrate again. If it stays `false`, run
     `ffelab emu monitor "info registers"` and look at the `CS` line. If CS's
     base (the second number) isn't 0, tell Claude: the harness assumes a flat
     code segment and needs a small fix before halts name the right function.
   - `ok: false` with a reason: follow the reason. A different exe from the one
     in the VM, or the game not running, are the usual causes.

## 8. Point ffelab at QEMU

In `.mcp.json`, change the `env` block of the ffelab server:

```json
"env": {
  "FFELAB_DB": "/Users/YOU/re/ffe-lab/ffe.db",
  "FFELAB_BACKEND": "qemu",
  "FFELAB_EMU": "{\"binary\": \"<binary_id>\", \"code_base\": 1048576, \"gdb_port\": 1234, \"monitor_port\": 4444}"
}
```

Use your own numbers from step 7. Restart Claude Code after editing it. For
`ffelab agents run --backend qemu` and the Temporal worker, export the same
value in your shell: `export FFELAB_EMU='...'`.

## 9. Make the save states

Make these while **nothing else is attached to the game**: close Claude Code,
or at least don't ask it to touch the game, because ffelab's debugger
connection pauses the game while it's attached and you won't be able to play.

For each row: play to that situation, then run the command on the Mac. It
saves a QEMU snapshot with that name inside `freedos.qcow2` and registers the
same name with its tags in `ffe.db` (the "import and tag" step from the docs).
Saving takes a second or two; the game pauses and carries on.

| # | Get the game to... | Command |
|---|---|---|
| 1 | The main menu (title screen with the game options) | `ffelab scenario save main_menu --tags menu` |
| 2 | Docked at a station, at the station services screen | `ffelab scenario save docked --tags docked` |
| 3 | Just after requesting launch, while the ship is leaving the dock | `ffelab scenario save launching --tags docked,launching` |
| 4 | In open space, far from any planet (well out from the station, or after a jump) | `ffelab scenario save deep_space --tags space` |
| 5 | High orbit over a planet **with** an atmosphere (a habitable or gas world) | `ffelab scenario save orbit_atmo --tags near_planet,atmosphere,altitude_high` |
| 6 | High orbit over a planet **without** one (a moon or rock), at a similar height to #5 | `ffelab scenario save orbit_airless --tags near_planet,altitude_high` |
| 7 | Low over the atmosphere planet from #5, inside the atmosphere | `ffelab scenario save low_atmo --tags near_planet,atmosphere,altitude_low` |
| 8 | Landed (or skimming the ground) on that planet | `ffelab scenario save surface --tags near_planet,atmosphere,planet_surface` |
| 9 | In space with an enemy ship close and fighting | `ffelab scenario save combat --tags space,combat` |
| 10 | In the hyperspace jump itself | `ffelab scenario save hyperspace --tags hyperspace` |

Tips:

- **The pairs are the point.** #5 and #6 differ only in `atmosphere`; #5 and #7
  only in altitude; #4 and #9 only in `combat`; #2 and #3 only in `launching`.
  Make each pair as alike as you can otherwise (same system, same ship, similar
  view), so a function that fires in one and not the other points at that one
  difference.
- **Short moments** (#3, #10): freeze the game first with
  `ffelab emu monitor stop` at the right instant, run the save command, then
  `ffelab emu monitor cont`.
- **Use the external or forward view consistently** across states; a different
  view changes what's drawn and muddies the comparisons.
- **Redo a state** by running the same command again: `savevm` replaces a
  snapshot with the same name, and the tags are overwritten.
- **Retag without saving** (or tag a state you made some other way):
  `ffelab scenario tag orbit_atmo --tags near_planet,atmosphere,altitude_high`.
- **Back up `freedos.qcow2`** once the set is done (`cp freedos.qcow2 freedos-states.qcow2`).
  Every save state lives in it.

Check the library:

```bash
ffelab scenario list
```

Each line shows a scenario, its tags, and `snapshot ok` when QEMU has a save
state of that name. Load one by hand to check it:
`ffelab emu monitor "loadvm orbit_atmo"`.

You can also do the tagging in Claude Code instead of with `scenario tag`
("Register scenario orbit_atmo with tags near_planet, atmosphere,
altitude_high"); it writes the same row. The snapshot itself still has to be
saved on QEMU's side, which is what `scenario save` adds.

## 10. First live test

With QEMU running, open Claude Code in the ffe-lab folder and ask, one at a time:

> "Load state docked and take a screenshot."

> "Run a logging pass in deep_space on the 20 most uncertain functions."

> "Knock out one function that fired every frame in deep_space and show me the diff."

If all three work, bring-up is done; carry on with GETTING_STARTED Stage 3. If
one fails, paste the error to Claude. The likely first fixes are key names
and timings in `harness/emulator.py`.

## 11. Beyond screenshots: memory, sound and files

Screenshots can't tell apart code that draws nothing: music, file loading,
state nobody displays. Three more observations cover that. None of them
changes `run-ffe.sh`, so your save states stay valid.

**Memory watchpoints.** "Who writes this address?" QEMU's debugger port
stops the game when watched memory is written, read or accessed (any number
of watchpoints, any length). Ask Claude for a watch pass:

> "Watch writes to 0x2A4F00, 16 bytes, in docked while pressing F3, region stock_data."

Each stop is credited to the function that touched the memory. If that was
unmapped code (see below), the nearest FFE caller on the stack gets it. A
`region` name from `halt.py`'s `RULES["region"]` (for example `sample_data`
or `stock_data`) lets the hits move beliefs. Without one, the pass only
records who touched what.

**Sound in knockouts.** The GDB protocol has no breakpoints on I/O ports.
Instead, during each knockout run ffelab switches on QEMU's
`memory_region_ops_write` trace from the monitor and counts writes to the
`sb16`, `adlib` and `pcspk` devices. The three runs (two baselines and the
knockout) are compared the way the screenshots are, so the difference
between the baselines is treated as noise. `audio_outcome` is
`audible`, `inaudible`, or `no_audio` (nothing was playing in the baselines,
so no verdict, and audio bets stay open). It says *that* the sound changed,
not who wrote the port. Music runs off a timer interrupt, so a knockout only
shows up if the music would have changed inside `settle_ms`: give music
knockouts keys that start or switch it (requesting launch, docking, a fight
starting). Set `"audio_trace": false` in `FFELAB_EMU` to turn the channel off.

**DOS file calls.** "Which function loads which file?" FFE runs under
DOS/4GW in a flat 32-bit model, and its C runtime still calls DOS with
`int 21h` (or DPMI `int 31h`, function 0300h, for a real-mode call), which
DOS/4GW handles. JJ replaced that runtime in `ffe.asm`, so ffelab scans your
`FIRSTENC.EXE` for those instructions *outside* the mapped functions, puts a
breakpoint on each, and decodes open/create (with the filename), read/write
(handle and size), seek and close. Set `FFELAB_EXE` to the exe that's in the
VM, then ask:

> "Run a file access pass in docked while pressing F8."

It records which FFE function asked for which file. It moves no beliefs: a
filename's subsystem is your call, made as an observation or a claim.

**Unmapped code.** The asm only covers FFE's own functions. The C runtime,
the HMI sound code and DOS/4GW aren't in it. A function can't be longer than
15 bytes (the longest x86 instruction) times its instruction count, so an
address beyond that is treated as unmapped. Halts and watch hits there are
credited to the nearest FFE caller found on the stack (`attributed_by:
"stack"`), or to nobody. Before this, they were silently blamed on whichever
function started just below.

**Which boot entry.** Use the same FreeDOS boot-menu entry for every save
state. The one without JEMMEX (HIMEM only, like the "XMS" setups FFE ran
under in Windows 98's DOS mode) is the closest to the original. `info
registers` on the monitor shows the game's privilege ring as the DPL on the
CS line. It doesn't matter to the debugger, which works at any ring.

## Troubleshooting

| Symptom | Try |
|---|---|
| FFE crashes or says it's out of memory at start | At the FreeDOS boot menu, pick the entry that doesn't load JEMMEX/EMM386; DOS extenders often clash with memory managers. Also try `-m 16`. |
| No sound, or the game hangs at the sound setup | Re-run the game's sound setup with the SB16 values in step 5, or choose "no sound" for now. Everything except the sound channel of knockouts (step 11) works without it. |
| Knockouts always say `audio_outcome: null` | Your QEMU was built without the `log` trace backend: `ffelab emu monitor "log trace:memory_region_ops_write"` prints a help line instead of nothing (if it prints nothing, it worked: run `ffelab emu monitor "log none"` to switch it off again). Upstream's default build has it; a build without it needs QEMU rebuilt with `--enable-trace-backends=log`. |
| `savevm` says a device "does not support snapshots" | A writable disk isn't qcow2 (often the game-folder drive from step 3 is still in the command line). Use only `run-ffe.sh`. |
| `loadvm` fails after editing `run-ffe.sh` | The hardware no longer matches. Undo the edit, or remake the save states. |
| `calibrate` can't connect | QEMU isn't running with `run-ffe.sh`, or Claude Code's ffelab server is attached to port 1234. Close it and retry. |
| `ffelab scenario save` hangs | Another program has the monitor port open (an old `nc` or `telnet`). Close it. |
| The game is frozen while Claude Code is open | Expected: ffelab's debugger connection pauses the game between commands. |

## Adding DOSBox-X later (if QEMU can't run FFE)

What it would take, roughly in order of effort:

1. **A debugger port.** Stock DOSBox-X has none. There are forks with a remote
   debugging server (the "remote debug" builds that MCP projects for DOSBox-X
   use), but you build them from source on the Mac and they're less proven
   than QEMU's stub. `gdb.py` needs no changes if the fork speaks the standard
   GDB protocol.
2. **Named save states.** DOSBox-X saves to numbered slots chosen with
   hotkeys. The backend would need a name-to-slot table and key sequences to
   select a slot before save/load (`save_state_keys` / `load_state_keys` in
   `EmulatorConfig` are the start of this), sent with the window focused.
3. **Window-only screenshots.** `screencapture -x` grabs the whole Mac screen,
   which breaks knockout diffs whenever anything else on screen changes. It
   needs `screencapture -l <window id>` or DOSBox-X's own capture.
4. **Keys without stealing focus.** Keystrokes go through macOS System Events
   to the frontmost window, so you can't use the Mac during a run, and macOS
   asks for Accessibility permission for the terminal.

Item 1 is the blocker; the rest are a day of harness work once it exists.
