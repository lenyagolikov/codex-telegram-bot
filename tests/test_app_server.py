from __future__ import annotations

import asyncio
import unittest

from codex_telegram_bot.app_server import APP_SERVER_DISABLED_FEATURES, CodexAppServer


class AppServerConfigurationTests(unittest.TestCase):
    def test_uses_native_shell_instead_of_programmable_exec(self) -> None:
        self.assertIn("unified_exec", APP_SERVER_DISABLED_FEATURES)
        self.assertIn("code_mode", APP_SERVER_DISABLED_FEATURES)


class AppServerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_waiter_survives_controlled_restart(self) -> None:
        app_server = CodexAppServer("codex")
        first_reader = asyncio.create_task(asyncio.sleep(60))
        app_server._reader_task = first_reader
        app_server._generation = 1

        health_waiter = asyncio.create_task(app_server.wait_until_stopped())
        await asyncio.sleep(0)

        app_server._restart_target_generation = 2
        app_server._restart_finished.clear()
        first_reader.cancel()
        await asyncio.sleep(0)

        second_reader = asyncio.create_task(asyncio.sleep(60))
        app_server._reader_task = second_reader
        app_server._generation = 2
        app_server._process = _RunningProcess()  # type: ignore[assignment]
        app_server._restart_target_generation = None
        app_server._restart_finished.set()
        await asyncio.sleep(0)

        self.assertFalse(health_waiter.done())

        health_waiter.cancel()
        second_reader.cancel()
        await asyncio.gather(health_waiter, second_reader, return_exceptions=True)


class _RunningProcess:
    returncode = None


if __name__ == "__main__":
    unittest.main()
