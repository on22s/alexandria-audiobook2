import asyncio
import unittest
from unittest.mock import patch

from routers import system


class VersionEndpointTests(unittest.TestCase):
    def test_version_endpoint_returns_runtime_info(self):
        with patch.object(system, "get_runtime_info", return_value={
                "revision": "abc123", "short_revision": "abc123",
                "revision_source": "git"}) as runtime:
            result = asyncio.run(system.get_version())
        runtime.assert_called_once_with(system.ROOT_DIR)
        self.assertEqual("abc123", result["short_revision"])


if __name__ == "__main__":
    unittest.main()
