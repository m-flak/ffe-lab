"""Minimal GDB Remote Serial Protocol client.

Talks to any emulator that exposes a GDB stub (QEMU's ``-gdb tcp::1234``
always does; some DOSBox builds and forks do too). Only what the harness
needs: breakpoints, continue, interrupt, registers, memory.
"""
from __future__ import annotations

import socket
import struct

I386_REGS = ["eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi", "eip", "eflags",
             "cs", "ss", "ds", "es", "fs", "gs"]


def checksum(payload: bytes) -> bytes:
    return b"%02x" % (sum(payload) & 0xFF)


def frame(payload: str) -> bytes:
    p = payload.encode()
    return b"$" + p + b"#" + checksum(p)


class GdbRemote:
    def __init__(self, host: str = "127.0.0.1", port: int = 1234, timeout: float = 5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        # Each request is a small write right after our '+' ack; without this, Nagle's
        # algorithm holds it for the peer's delayed ACK (~40 ms or more per round trip).
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buf = b""

    def close(self) -> None:
        self.sock.close()

    # ---------------------------------------------------------- transport
    def _recv_packet(self) -> str:
        while True:
            start = self.buf.find(b"$")
            end = self.buf.find(b"#", start + 1) if start >= 0 else -1
            if start >= 0 and end >= 0 and len(self.buf) >= end + 3:
                payload = self.buf[start + 1:end]
                self.buf = self.buf[end + 3:]
                self.sock.sendall(b"+")
                return payload.decode(errors="replace")
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("gdb stub closed connection")
            self.buf += chunk          # stray '+' acks before '$' are skipped by find()

    def command(self, payload: str) -> str:
        self.sock.sendall(frame(payload))
        return self._recv_packet()

    # ---------------------------------------------------------- operations
    def set_breakpoint(self, addr: int) -> bool:
        return self.command(f"Z0,{addr:x},1") == "OK"

    def remove_breakpoint(self, addr: int) -> bool:
        return self.command(f"z0,{addr:x},1") == "OK"

    def cont(self, timeout: float | None = None) -> str:
        """Continue; returns the stop reply (e.g. 'T05...') or '' on timeout."""
        self.sock.sendall(frame("c"))
        old = self.sock.gettimeout()
        self.sock.settimeout(timeout)
        try:
            return self._recv_packet()
        except socket.timeout:
            return ""
        finally:
            self.sock.settimeout(old)

    def interrupt(self) -> str:
        self.sock.sendall(b"\x03")
        return self._recv_packet()

    def registers(self) -> dict[str, int]:
        raw = bytes.fromhex(self.command("g"))
        n = min(len(raw) // 4, len(I386_REGS))
        vals = struct.unpack("<%dI" % n, raw[:4 * n])
        return dict(zip(I386_REGS, vals))

    def write_mem(self, addr: int, data: bytes) -> None:
        reply = self.command(f"M{addr:x},{len(data):x}:{data.hex()}")
        if reply != "OK":
            raise IOError(f"memory write failed at {addr:#x}: {reply}")

    def read_mem(self, addr: int, length: int) -> bytes:
        reply = self.command(f"m{addr:x},{length:x}")
        if reply.startswith("E"):
            raise IOError(f"memory read failed at {addr:#x}: {reply}")
        return bytes.fromhex(reply)
