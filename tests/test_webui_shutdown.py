from __future__ import annotations

import unittest
from unittest.mock import Mock

from launch_webui import PathPickerHandler, PathPickerServer, UnavailableExportCoordinator


class WebUIShutdownTests(unittest.TestCase):
    def test_close_attempts_every_coordinator_and_preserves_failure(self) -> None:
        server = PathPickerServer(("127.0.0.1", 0), PathPickerHandler)
        components = [Mock() for _ in range(4)]
        failure = RuntimeError("owned worker still running")
        components[0].shutdown.side_effect = failure
        for name, component in zip(("first_run", "character", "instanced", "coordinator"), components):
            setattr(server, name, component)
        with self.assertRaises(ExceptionGroup) as raised:
            server.server_close()
        self.assertEqual(raised.exception.exceptions, (failure,))
        for component in components:
            component.shutdown.assert_called_once_with()
        self.assertEqual(server.socket.fileno(), -1)

    def test_unprepared_backend_closes_without_workers(self) -> None:
        server = PathPickerServer(("127.0.0.1", 0), PathPickerHandler)
        server.coordinator = UnavailableExportCoordinator()
        server.server_close()
        self.assertEqual(server.socket.fileno(), -1)


if __name__ == "__main__":
    unittest.main()
