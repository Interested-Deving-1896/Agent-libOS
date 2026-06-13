import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from agent_libos.models import EventType, ProcessStatus
from agent_libos.runtime.runtime import Runtime


class RuntimeShutdownTests(unittest.TestCase):
    def test_shutdown_is_host_lifecycle_not_process_exit(self) -> None:
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "runtime.sqlite")
            runtime = Runtime.open(db)
            pid = runtime.process.spawn(goal="stay runnable")

            result = runtime.shutdown(actor="test", reason="unit-test")

            self.assertTrue(result["ok"])
            self.assertFalse(result["already_shutdown"])
            self.assertTrue(runtime.shutdown()["already_shutdown"])

            reopened = Runtime.open(db)
            try:
                process = reopened.store.get_process(pid)
                self.assertIsNotNone(process)
                self.assertEqual(process.status, ProcessStatus.RUNNABLE)
                self.assertTrue(any(record.action == "runtime.shutdown" for record in reopened.audit.trace()))
                self.assertTrue(any(event.type == EventType.RUNTIME_SHUTDOWN for event in reopened.events.list()))
            finally:
                reopened.shutdown(actor="test", reason="reopen-cleanup")


if __name__ == "__main__":
    unittest.main()
