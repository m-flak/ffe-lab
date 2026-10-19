"""Run every MCP tool body on one dedicated thread.

mcp 2.x runs sync tools in a pool of worker threads, but a SQLite connection may
only be used by the thread that opened it, and the emulator backend is one
object too. So the servers open their store on this thread (``on_db_thread``)
and register tools with ``tool(mcp)``, which hands each call to it. Calls run
one at a time, in order, as they did under mcp 1.x, without blocking the
server's event loop.
"""
from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor

_db_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ffelab-db")


def on_db_thread(fn, *args, **kwargs):
    """Call ``fn`` on the tool thread and wait for it (e.g. to open the store there)."""
    return _db_thread.submit(fn, *args, **kwargs).result()


def tool(mcp):
    """``@tool(mcp)`` in place of ``@mcp.tool()``: same name, docstring and parameters."""
    def register(fn):
        @functools.wraps(fn)
        async def run(*args, **kwargs):
            return await asyncio.get_running_loop().run_in_executor(
                _db_thread, functools.partial(fn, *args, **kwargs))
        return mcp.tool()(run)
    return register
