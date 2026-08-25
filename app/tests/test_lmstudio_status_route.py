import unittest
from unittest.mock import patch

from routers import system


class LmStudioStatusRouteTest(unittest.IsolatedAsyncioTestCase):
    async def test_status_uses_the_active_profile_not_the_stale_mirror(self):
        config = {
            "llm_mode": "remote", "llm_remote_ssh": "tnr-0",
            "llm": {"base_url": "http://local:8090/v1", "model_name": "local"},
            "llm_remote": {"base_url": "http://remote:8090/v1",
                           "model_name": "remote"},
        }
        with patch.object(system, "load_app_config", return_value=config), \
             patch.object(system, "get_current_status",
                          return_value={"available": True}) as current:
            result = await system.lmstudio_status()
        self.assertEqual("remote", result["model"])
        self.assertEqual("http://remote:8090/v1", current.call_args.args[1])
        self.assertTrue(result["remote"])
