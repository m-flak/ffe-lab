"""Observation beyond the screen: watchpoints, func_at bounds, DOS file calls, sound in knockouts."""
import shutil
import socket
import struct
import tempfile
import threading
import unittest

from store import fresh_db

from ffelab import exe, observe
from ffelab import knockout as ko
from ffelab.harness.emulator import EmulatorBackend, EmulatorConfig, count_port_writes
from ffelab.harness.fake import FakeBackend
from ffelab.harness.gdb import GdbRemote, checksum, parse_stop
from ffelab.ledger import Ledger
from ffelab.models import BetSpec, Watch

BASE = 0x1000                       # code_base: Pos p runs at BASE + p
POS = {1: 0x100, 2: 0x200}
SIZES = {1: 0x20, 2: 0x40}


class Stub(threading.Thread):
    """GDB stub over a 64 KB flat memory: g, m, Z/z, s, c (replies from a queue), ^C."""

    def __init__(self, stops=(), regs=None):
        super().__init__(daemon=True)
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.mem = bytearray(0x10000)
        self.stops = list(stops)                   # each: (reply, regs) for one 'c'
        self.regs = dict(regs or {})
        self.packets: list[str] = []

    def poke(self, addr, data):
        self.mem[addr:addr + len(data)] = data

    def reply(self, conn, payload):
        p = payload.encode(); conn.sendall(b"+$" + p + b"#" + checksum(p))

    def run(self):
        order = ["eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi", "eip", "eflags",
                 "cs", "ss", "ds", "es", "fs", "gs"]
        conn, _ = self.srv.accept(); buf = b""
        while True:
            data = conn.recv(4096)
            if not data:
                return
            buf += data
            if b"\x03" in buf:
                buf = buf.replace(b"\x03", b""); self.reply(conn, "T02")
            while b"$" in buf and b"#" in buf[buf.index(b"$"):] and \
                    len(buf) >= buf.index(b"#", buf.index(b"$")) + 3:
                start = buf.index(b"$"); end = buf.index(b"#", start)
                cmd = buf[start + 1:end].decode(); buf = buf[end + 3:]
                self.packets.append(cmd)
                if cmd == "g":
                    self.reply(conn, struct.pack("<16I", *[self.regs.get(r, 0) for r in order]).hex())
                elif cmd.startswith("m"):
                    addr, n = (int(x, 16) for x in cmd[1:].split(","))
                    self.reply(conn, bytes(self.mem[addr:addr + n]).hex())
                elif cmd[:1] in ("Z", "z"):
                    self.reply(conn, "OK")
                elif cmd == "s":
                    self.reply(conn, "T05")
                elif cmd == "c" and self.stops:
                    reply, regs = self.stops.pop(0)
                    self.regs.update(regs)
                    self.reply(conn, reply)
                # 'c' with nothing queued: stay silent, as a running game would


def emu(stub):
    stub.start()
    cfg = EmulatorConfig(kind="qemu", gdb_port=stub.port, code_base=BASE, audio_trace=False)
    return EmulatorBackend(cfg, POS, SIZES)


class TestStopParsing(unittest.TestCase):
    def test_watch_kinds(self):
        self.assertEqual((parse_stop("T05watch:2000;thread:01;").watch_kind,
                          parse_stop("T05watch:2000;thread:01;").watch_addr), ("write", 0x2000))
        self.assertEqual(parse_stop("T05rwatch:10;").watch_kind, "read")
        self.assertEqual(parse_stop("T05awatch:10;").watch_kind, "access")
        plain = parse_stop("T05thread:01;")
        self.assertTrue(plain.is_trap)
        self.assertIsNone(plain.watch_addr)
        self.assertFalse(parse_stop("T0b").is_trap)            # SIGSEGV
        self.assertIsNone(parse_stop("W00").signal)

    def test_watchpoint_packets(self):
        stub = Stub(); stub.start()
        g = GdbRemote("127.0.0.1", stub.port)
        self.assertTrue(g.set_watchpoint(0x2000, 4, "write"))
        self.assertTrue(g.remove_watchpoint(0x2000, 4, "access"))
        self.assertEqual(stub.packets[-2:], ["Z2,2000,4", "z4,2000,4"])
        g.close()


class TestFuncAt(unittest.TestCase):
    def test_bounds(self):
        b = emu(Stub())
        self.assertEqual(b.func_at(BASE + 0x110), 1)
        self.assertIsNone(b.func_at(BASE + 0x130))          # past F1's longest possible end
        self.assertEqual(b.func_at(BASE + 0x23f), 2)
        self.assertIsNone(b.func_at(BASE + 0x240))
        self.assertIsNone(b.func_at(BASE + 0xff))           # below the first function
        b.gdb.close()


class TestWatchHits(unittest.TestCase):
    def test_mapped_and_unmapped(self):
        stub = Stub(stops=[("T05watch:8000;thread:01;", {"eip": BASE + 0x110, "esp": 0x3000}),
                           ("T05watch:8004;thread:01;", {"eip": 0x9000, "esp": 0x3000})])
        # stack for the second stop: junk, then a return address into F2 right after a call
        ret = BASE + 0x210
        stub.poke(0x3000, struct.pack("<3I", 0x12345678, BASE + 0x500, ret))
        stub.poke(ret - 5, b"\xe8")
        b = emu(stub)
        b.set_watchpoints([Watch(addr=0x8000, length=8)])
        hits = b.watch_hits(300)
        self.assertEqual([(h.func, h.attributed_by) for h in hits], [(1, "pc"), (2, "stack")])
        self.assertEqual(hits[1].addr, 0x8004)
        b.clear_watchpoints()
        self.assertIn("z2,8000,8", stub.packets)
        b.gdb.close()


class TestFileAccess(unittest.TestCase):
    def test_int21_and_int31(self):
        site21, site31 = 0x300, 0x310                       # Pos, outside both functions
        ret = BASE + 0x108
        stub = Stub(stops=[
            ("T05", {"eip": BASE + site21, "eax": 0x3D00, "edx": 0x4000, "esp": 0x3000}),
            ("T05", {"eip": BASE + site31, "eax": 0x0300, "ebx": 0x21, "edi": 0x4100, "esp": 0x3000}),
            ("T05", {"eip": BASE + site21, "eax": 0x3F00, "ebx": 5, "ecx": 4096, "esp": 0x3000}),
        ])
        stub.poke(0x3000, struct.pack("<I", ret)); stub.poke(ret - 5, b"\xe8")
        stub.poke(0x4000, b"MUSIC\\SONG1.XMI\0")
        # DPMI real-mode register block: EAX=3D00h, EDX=0020h, DS=0500h -> linear 0x5020
        rm = bytearray(0x32)
        struct.pack_into("<I", rm, 28, 0x3D00); struct.pack_into("<I", rm, 20, 0x20)
        struct.pack_into("<H", rm, 0x24, 0x500)
        stub.poke(0x4100, bytes(rm)); stub.poke(0x5020, b"SAMPLES.DAT\0")
        b = emu(stub)
        calls = b.file_accesses(300, {site21: "int21", site31: "int31"})
        self.assertEqual([(c.via, c.dos_fn, c.filename) for c in calls],
                         [("int21", 0x3D, "MUSIC\\SONG1.XMI"), ("int31", 0x3D, "SAMPLES.DAT"),
                          ("int21", 0x3F, None)])
        self.assertEqual((calls[2].handle, calls[2].count), (5, 4096))
        self.assertTrue(all(c.func == 1 and c.attributed_by == "stack" for c in calls))
        b.gdb.close()

    def test_int_sites_skip_mapped_code(self):
        data = bytearray(exe.FCODEOFF + 0x2000)
        inside = exe.FCODEOFF + 0x10                         # in F1's span
        outside = exe.FCODEOFF + 0x800
        data[inside:inside + 2] = b"\xcd\x21"
        data[outside:outside + 2] = b"\xcd\x21"
        data[outside + 8:outside + 10] = b"\xcd\x31"
        pos = {1: exe.to_pos(exe.FCODEOFF)}
        sites = exe.int_sites(bytes(data), pos, {1: 0x40})
        self.assertEqual(sites, {exe.to_pos(outside): "int21", exe.to_pos(outside + 8): "int31"})


class TestPortTrace(unittest.TestCase):
    def test_counts(self):
        log = ["1@1.0:memory_region_ops_write cpu 0 mr 0x1 addr 0x388 value 0x20 size 1 name 'adlib'",
               "1@1.0:memory_region_ops_write cpu 0 mr 0x2 addr 0x22c value 0xd1 size 1 name 'sb16'",
               "1@1.0:memory_region_ops_write cpu 0 mr 0x3 addr 0x3d4 value 0x0 size 1 name 'vga'",
               "1@1.0:memory_region_ops_write cpu 0 mr 0x1 addr 0x389 value 0x0 size 1 name 'adlib'"]
        self.assertEqual(count_port_writes(log), {"sb16": 1, "adlib": 2, "pcspk": 0})


MUSIC, SFX, QUIET = 1, 2, 3


def world():
    con = fresh_db()
    con.executemany("INSERT INTO functions (num) VALUES (?)", [(i,) for i in (MUSIC, SFX, QUIET)])
    con.commit()
    shapes = {"s": [(f, (0, 10 * f, 100, 10 * f + 5), (200, 200, 200)) for f in (MUSIC, SFX, QUIET)],
              "silent": [(QUIET, (0, 0, 10, 10), (200, 200, 200))]}
    fb = FakeBackend({"s": {MUSIC: 1.0, SFX: 0.1, QUIET: 1.0}, "silent": {QUIET: 1.0}}, visuals=shapes,
                     sound={"s": {MUSIC: {"adlib": 900}, SFX: {"sb16": 40}}},
                     writers={"s": {0x8000: [SFX], 0x9000: [QUIET]}},
                     files={"s": [(MUSIC, 0x3D, "SONG1.XMI")]})
    return Ledger(con), fb


class TestSoundKnockout(unittest.TestCase):
    def setUp(self):
        self.L, self.fb = world()
        self.out = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def test_audible_resolves_bet_and_moves_belief(self):
        bet = self.L.place_bet("human", MUSIC, "audible", "knocking it out changes the music", 0.7,
                               spec={"knockout": True, "func": MUSIC, "scenario": "s", "expect": "audible"})
        before = self.L.belief(MUSIC)["audio"]
        r = ko.knockout(self.fb, self.L, "s", MUSIC, outdir=self.out)
        self.assertEqual(r["audio_outcome"], "audible")
        self.assertTrue(r["audio"]["devices"]["adlib"]["changed"])
        self.assertEqual([(x["bet"], x["outcome"]) for x in r["resolved"]], [(bet, True)])
        self.assertGreater(self.L.belief(MUSIC)["audio"], before)

    def test_inaudible_and_no_audio(self):
        before = self.L.belief(QUIET)["audio"]
        r = ko.knockout(self.fb, self.L, "s", QUIET, outdir=self.out)
        self.assertEqual(r["audio_outcome"], "inaudible")
        self.assertLess(self.L.belief(QUIET)["audio"], before)
        bet = self.L.place_bet("human", QUIET, "audible", "x", 0.6,
                               spec={"knockout": True, "func": QUIET, "scenario": "silent", "expect": "audible"})
        r = ko.knockout(self.fb, self.L, "silent", QUIET, outdir=self.out)
        self.assertEqual(r["audio_outcome"], "no_audio")             # nothing playing: no verdict
        self.assertEqual(r["resolved"], [])
        self.assertIsNone(self.L.con.execute("SELECT outcome FROM bets WHERE id=?", (bet,)).fetchone()["outcome"])

    def test_small_change_is_noise(self):
        d = ko.audio_diff({"sb16": 1000}, {"sb16": 900}, {"sb16": 820})
        self.assertEqual(d["outcome"], "inaudible")
        self.assertEqual(ko.audio_diff({"sb16": 1000}, {"sb16": 990}, {"sb16": 0})["outcome"], "audible")

    def test_bet_spec(self):
        BetSpec(scenario="s", func=1, knockout=True, expect="inaudible")
        with self.assertRaises(ValueError):
            BetSpec(scenario="s", func=1, expect="audible")


class TestObservePasses(unittest.TestCase):
    def test_watch_pass_region(self):
        L, fb = world()
        before = L.belief(SFX)["audio"]
        r = observe.watch_pass(fb, L, "s", [Watch(addr=0x8000, length=4, region="sample_data"),
                                            Watch(addr=0x9000, length=4)])
        self.assertEqual(r["counts"], {SFX: 1, QUIET: 1})
        self.assertEqual(r["updated"], [SFX])                        # only the named region moves beliefs
        self.assertGreater(L.belief(SFX)["audio"], before)
        self.assertEqual(L.con.execute("SELECT kind FROM experiments WHERE id=?",
                                       (r["experiment"],)).fetchone()["kind"], "watch")

    def test_file_access_pass(self):
        L, fb = world()
        r = observe.file_access_pass(fb, L, "s", {0x300: "int21"})
        self.assertEqual(r["files"], [{"func": MUSIC, "filename": "SONG1.XMI"}])
        with self.assertRaises(ValueError):
            observe.file_access_pass(fb, L, "s", {})


if __name__ == "__main__":
    unittest.main()
