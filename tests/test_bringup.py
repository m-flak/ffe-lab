import os
import random
import socket
import struct
import tempfile
import threading
import unittest

from ffelab import exe
from ffelab.behavior import scenarios
from ffelab.cli import main
from ffelab.harness import bringup
from ffelab.harness.gdb import checksum
from ffelab.ledger import Ledger
from store import fresh_db

BASE = 0x00300000                     # runtime address of Pos 0 in the fake guest
EIP = BASE + 0x10000 + 0x480


class MemStub(threading.Thread):
    """GDB stub over a flat guest memory; records whether the client detached."""

    def __init__(self, mem: bytearray):
        super().__init__(daemon=True)
        self.mem = mem
        self.detached = False
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(1)
        self.port = self.srv.getsockname()[1]

    def reply(self, conn, payload):
        p = payload.encode(); conn.sendall(b"+$" + p + b"#" + checksum(p))

    def run(self):
        conn, _ = self.srv.accept(); buf = b""
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        while True:
            data = conn.recv(4096)
            if not data:
                return
            buf += data.lstrip(b"+")
            while b"#" in buf and len(buf) >= buf.index(b"#") + 3:
                start = buf.index(b"$"); end = buf.index(b"#")
                cmd = buf[start + 1:end].decode(); buf = buf[end + 3:].lstrip(b"+")
                if cmd == "g":
                    regs = [0] * 16; regs[8] = EIP
                    self.reply(conn, struct.pack("<16I", *regs).hex())
                elif cmd.startswith("m"):
                    addr, n = (int(x, 16) for x in cmd[1:].split(","))
                    if addr + n > len(self.mem):
                        self.reply(conn, "E14")
                    else:
                        self.reply(conn, bytes(self.mem[addr:addr + n]).hex())
                elif cmd == "D":
                    self.detached = True
                    self.reply(conn, "OK")


class MonitorStub(threading.Thread):
    """QEMU monitor over TCP: prompts, remembers savevm tags, answers info snapshots."""

    def __init__(self):
        super().__init__(daemon=True)
        self.tags: list[str] = []
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(4)
        self.port = self.srv.getsockname()[1]

    def run(self):
        while True:
            conn, _ = self.srv.accept()
            conn.sendall(b"QEMU 9.2.0 monitor - type 'help' for more information\r\n(qemu) ")
            line = b""
            while not line.endswith(b"\n"):
                line += conn.recv(1024)
            cmd = line.decode().strip()
            out = cmd + "\r\n"
            if cmd.startswith("savevm "):
                self.tags.append(cmd.split()[1])
            elif cmd == "info snapshots":
                out += "List of snapshots present on all disks:\r\nID        TAG               VM SIZE\r\n"
                out += "".join(f"--        {t}        4 MiB\r\n" for t in self.tags)
            conn.sendall(out.encode() + b"(qemu) ")
            conn.close()


def fake_game():
    """A fake exe, a db describing its framed functions, and guest memory holding its code."""
    rnd = random.Random(7)
    code = bytearray(rnd.randbytes(0x4000))
    con = fresh_db()
    for k in range(40):
        off = 0x80 * k
        code[off:off + 3] = exe.PROLOGUES[k % 2]
        con.execute("INSERT INTO functions (num, pos, has_frame) VALUES (?,?,1)", (k + 1, exe.VCODEOFF + off))
    con.commit()
    data = bytes(exe.FCODEOFF) + bytes(code)
    mem = bytearray(BASE + exe.VCODEOFF + len(code) + 0x1000)
    mem[BASE + exe.VCODEOFF:BASE + exe.VCODEOFF + len(code)] = code
    return con, data, mem


class TestCalibrate(unittest.TestCase):
    def test_finds_code_base_and_detaches(self):
        con, data, mem = fake_game()
        stub = MemStub(mem); stub.start()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "FIRSTENC.EXE")
            open(path, "wb").write(data)
            r = bringup.calibrate(con, path, port=stub.port, limit_mb=8)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["code_base"], BASE)
        self.assertTrue(r["eip_in_code"])
        stub.join(2)
        self.assertTrue(stub.detached)

    def test_reports_missing_code(self):
        con, data, _ = fake_game()
        stub = MemStub(bytearray(0x400000)); stub.start()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "FIRSTENC.EXE")
            open(path, "wb").write(data)
            r = bringup.calibrate(con, path, port=stub.port, limit_mb=4)
        self.assertFalse(r["ok"])
        self.assertIn("FFE running", r["reason"])


class TestScenarioCommands(unittest.TestCase):
    def test_save_registers_and_lists(self):
        mon = MonitorStub(); mon.start()
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "ffe.db")
            port = str(mon.port)
            main(["scenario", "save", "orbit_atmo", "--tags", "near_planet,atmosphere, altitude_high",
                  "--db", db, "--monitor-port", port])
            main(["scenario", "tag", "docked", "--tags", "docked", "--db", db])
            self.assertEqual(mon.tags, ["orbit_atmo"])
            self.assertEqual(bringup.snapshots(mon.port), {"orbit_atmo"})
            from ffelab import db as dbmod
            sc = scenarios(Ledger(dbmod.connect(db)))
        self.assertEqual(sc["orbit_atmo"], {"near_planet", "atmosphere", "altitude_high"})
        self.assertEqual(sc["docked"], {"docked"})


if __name__ == "__main__":
    unittest.main()
