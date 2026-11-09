import asyncio
import importlib.util
import unittest


@unittest.skipUnless(importlib.util.find_spec("mcp"), "optional MCP SDK not installed")
class MCPTests(unittest.TestCase):
    def test_real_client_server_exchange(self):
        from verify_mcp import probe
        self.assertEqual(asyncio.run(probe())["status"], "passed")
