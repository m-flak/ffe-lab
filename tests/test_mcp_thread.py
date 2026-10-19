import asyncio
import importlib
import os
import tempfile
import unittest


class TestToolThread(unittest.TestCase):
    """mcp 2.x runs sync tools in worker threads; the store must still answer."""

    def test_full_server_db_tools_answer_through_mcp(self):
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("mcp not installed")
        os.environ["FFELAB_DB"] = os.path.join(tempfile.mkdtemp(), "s.db")
        srv = importlib.import_module("ffelab.mcp_server")

        async def calls():
            return await asyncio.gather(*[srv.mcp.call_tool("list_scenarios", {}) for _ in range(4)])
        for r in asyncio.run(calls()):
            self.assertFalse(getattr(r, "is_error", getattr(r, "isError", False)), r)


if __name__ == "__main__":
    unittest.main()
