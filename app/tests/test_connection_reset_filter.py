"""The Windows proactor's benign reset log is dropped, nothing else is (#1909).

A browser that drops a connection makes asyncio's Windows transport call
socket.shutdown on an already-reset socket and log
"Exception in callback _ProactorBasePipeTransport._call_connection_lost".
The filter must hide exactly that and keep every other loop error visible.
These run real asyncio exception contexts through a real event loop.
"""
import asyncio
import unittest

import app as app_module

BENIGN_MESSAGE = "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)"


class ConnectionResetFilterTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        app_module.apply_connection_reset_filter(self.loop)

    def _logged(self, context):
        with self.assertNoLogs("asyncio") if context.get("_expect_silent") else \
                self.assertLogs("asyncio", level="ERROR") as captured:
            self.loop.call_exception_handler(
                {key: value for key, value in context.items() if key != "_expect_silent"})
        return None if context.get("_expect_silent") else captured.output

    def test_the_proactor_shutdown_reset_is_dropped(self):
        error = ConnectionResetError(10054, "An existing connection was forcibly closed")
        self._logged({"message": BENIGN_MESSAGE, "exception": error, "_expect_silent": True})

    def test_a_reset_from_anywhere_else_is_still_logged(self):
        error = ConnectionResetError(10054, "reset")
        output = self._logged({"message": "Exception in callback some_other_callback()",
                               "exception": error})
        self.assertIn("some_other_callback", output[0])

    def test_another_error_in_the_same_callback_is_still_logged(self):
        output = self._logged({"message": BENIGN_MESSAGE, "exception": OSError("disk full")})
        self.assertIn("_call_connection_lost", output[0])

    def test_a_previously_installed_handler_still_receives_other_errors(self):
        seen = []
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        loop.set_exception_handler(lambda _loop, context: seen.append(context["message"]))
        app_module.apply_connection_reset_filter(loop)
        loop.call_exception_handler({"message": BENIGN_MESSAGE,
                                     "exception": ConnectionResetError()})
        loop.call_exception_handler({"message": "real problem", "exception": ValueError()})
        self.assertEqual(["real problem"], seen)

    def test_the_app_lifespan_installs_the_filter_on_the_running_loop(self):
        source = app_module.lifespan.__wrapped__.__code__.co_names
        self.assertIn("apply_connection_reset_filter", source)


if __name__ == "__main__":
    unittest.main()
