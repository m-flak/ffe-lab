import socket
import struct
import tempfile
import threading
import unittest

from ffelab import db as dbmod
from ffelab.halt import HaltSnapshot, score_halt, score_never_hit
from ffelab.harness.fake import FakeBackend
from ffelab.harness.gdb import GdbRemote, checksum
from ffelab.ledger import Ledger, entropy
from ffelab.loop import logging_pass, rank_targets, halt_and_score


def ledger():
    con = dbmod.connect(":memory:")
    con.executemany("INSERT INTO functions (num) VALUES (?)", [(i,) for i in range(1, 6)])
    return Ledger(con)


class TestLedger(unittest.TestCase):
    def test_bayes_update(self):
        L = ledger()
        post = L.update(1, {"render": 4.0}, "test", "unit")
        self.assertGreater(post["render"], post["ui"])
        self.assertAlmostEqual(sum(post.values()), 1.0)

    def test_human_call_outweighs_ai_call(self):
        L = ledger()
        h = L.call(1, "economy", 0.9, source="human")["economy"]
        a = L.call(2, "economy", 0.9, source="ai:claude")["economy"]
        self.assertGreater(h, a)

    def test_brier_and_reliability(self):
        L = ledger()
        r0 = L.reliability("ai:x")
        exp = L.record_experiment("logging_pass", "s", {"funcs": [1]})
        for _ in range(10):
            L.resolve_bet(L.place_bet("ai:x", 1, "c", "p", 0.9), False, exp)
        self.assertLess(L.reliability("ai:x"), r0)
        self.assertAlmostEqual(L.resolve_bet(L.place_bet("human", 1, "c", "p", 1.0), True, exp), 0.0)
        cal = L.calibration("ai:x")
        self.assertEqual(cal["n"], 10)
        self.assertAlmostEqual(cal["mean_brier"], 0.81)

    def test_domain_reliability(self):
        L = ledger()
        exp = L.record_experiment("logging_pass", "s", {"funcs": [1]})
        for _ in range(8):
            L.resolve_bet(L.place_bet("human", 1, "c", "p", 0.9, domain="economy"), False, exp)
            L.resolve_bet(L.place_bet("human", 1, "c", "p", 0.9, domain="render"), True, exp)
        self.assertGreater(L.reliability("human", "render"), L.reliability("human", "economy"))

    def test_eig_and_names(self):
        L = ledger()
        informative = {"hit": {"render": 0.9, "ui": 0.1}, "miss": {"render": 0.1, "ui": 0.9}}
        useless = {"hit": {lab: 0.5 for lab in L.labels}, "miss": {lab: 0.5 for lab in L.labels}}
        self.assertGreater(L.expected_info_gain(1, informative), L.expected_info_gain(1, useless))
        self.assertIsNone(L.propose_name(1))
        for _ in range(6):
            L.update(1, {"render": 5.0}, "t", "unit")
        lab, p = L.propose_name(1)
        self.assertEqual(lab, "render")
        L.commit_name(1, "render_Something", p)
        row = L.con.execute("SELECT * FROM names WHERE func=1").fetchone()
        self.assertIn("evidence", row["provenance"])


class TestHalt(unittest.TestCase):
    def test_per_frame_favours_render(self):
        like = score_halt(HaltSnapshot(func=1, scenario="s", hits=100, frames=100))
        self.assertGreater(like["render"], like["ui"])

    def test_menu_esc_favours_ui(self):
        like = score_halt(HaltSnapshot(func=1, scenario="s", hits=1, frames=100,
                                       context=["menu"], trigger="Esc"))
        self.assertEqual(max(like, key=like.get), "ui")

    def test_stack_module_label(self):
        like = score_halt(HaltSnapshot(func=1, scenario="s", stack=[9, 7]), module_label={7: "mission"})
        self.assertGreater(like["mission"], 1.0)

    def test_never_hit_lowers(self):
        like = score_never_hit(["combat"])
        self.assertLess(like["ai"], 1.0)

    def test_roundtrip(self):
        h = HaltSnapshot(func=3, scenario="docked", stack=[1, 2], context=["docked"])
        self.assertEqual(HaltSnapshot.from_json(h.to_json()), h)


class TestLoop(unittest.TestCase):
    def test_logging_pass_moves_beliefs(self):
        L = ledger()
        truth = {"combat": {1: 1.0, 2: 0.0}, "docked": {2: 0.02}}
        fb = FakeBackend(truth, context={"combat": ["combat"], "docked": ["docked"]})
        h0 = entropy(L.belief(1))
        res = logging_pass(fb, L, "combat", [1, 2], ["combat"])
        self.assertEqual(res["counts"], {1: 100, 2: 0})
        self.assertLess(entropy(L.belief(1)), h0)
        self.assertGreater(L.belief(1)["physics"], L.belief(2)["physics"])
        self.assertTrue(rank_targets(L, [1, 2, 3], ["combat"]))

    def test_halt_and_score(self):
        L = ledger()
        fb = FakeBackend({"menu": {4: 0.01}}, context={"menu": ["menu"]})
        snap = halt_and_score(fb, L, "menu", 4, ["menu"], keys=["Esc"])
        self.assertEqual(snap.func, 4)
        self.assertEqual(max(L.belief(4), key=L.belief(4).get), "ui")


class FakeStub(threading.Thread):
    """Tiny GDB stub: answers g, m, Z0/z0 and c with canned replies."""

    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.mem = {0x1000: bytes(range(16))}

    def reply(self, conn, payload):
        p = payload.encode(); conn.sendall(b"+$" + p + b"#" + checksum(p))

    def run(self):
        conn, _ = self.srv.accept(); buf = b""
        while True:
            data = conn.recv(4096)
            if not data:
                return
            buf += data
            while b"#" in buf and len(buf) >= buf.index(b"#") + 3:
                start = buf.index(b"$"); end = buf.index(b"#")
                cmd = buf[start + 1:end].decode(); buf = buf[end + 3:]
                if cmd == "g":
                    regs = list(range(16)); regs[8] = 0x1234
                    self.reply(conn, struct.pack("<16I", *regs).hex())
                elif cmd.startswith("m"):
                    addr, n = (int(x, 16) for x in cmd[1:].split(","))
                    self.reply(conn, self.mem.get(addr, bytes(n))[:n].hex())
                elif cmd.startswith(("Z0", "z0")):
                    self.reply(conn, "OK")
                elif cmd == "c":
                    self.reply(conn, "T05")


class TestGdb(unittest.TestCase):
    def test_protocol(self):
        stub = FakeStub(); stub.start()
        g = GdbRemote("127.0.0.1", stub.port)
        self.assertTrue(g.set_breakpoint(0x1000))
        self.assertEqual(g.cont(timeout=2), "T05")
        regs = g.registers()
        self.assertEqual(regs["eip"], 0x1234)
        self.assertEqual(regs["ecx"], 1)
        self.assertEqual(g.read_mem(0x1000, 4), bytes([0, 1, 2, 3]))
        g.close()


if __name__ == "__main__":
    unittest.main()
