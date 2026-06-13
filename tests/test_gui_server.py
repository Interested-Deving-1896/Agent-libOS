from __future__ import annotations

import http.client
import json
import threading
import time
import unittest
import urllib.request
from typing import Any

from agent_libos.api.gui.server import create_gui_http_server


class GuiServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_gui_http_server(db="local", port=0, token="test-token", auto_run=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.service.shutdown()
        self.server.server_close()

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        token: str = "test-token",
    ) -> tuple[int, Any]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        headers = {"Authorization": f"Bearer {token}"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        decoded = json.loads(data.decode("utf-8")) if data else None
        return response.status, decoded

    def test_auth_health_snapshot_and_process_flow(self) -> None:
        status, _body = self.request("GET", "/api/health", token="wrong")
        self.assertEqual(status, 401)

        status, health = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        self.assertFalse(health["scheduler"]["auto_run"])

        status, spawned = self.request("POST", "/api/processes", {"goal": "inspect README", "auto_run": False})
        self.assertEqual(status, 200)
        pid = spawned["pid"]

        status, message = self.request("POST", f"/api/processes/{pid}/message", {"body": "hello", "auto_run": False})
        self.assertEqual(status, 200)
        self.assertEqual(message["message"]["body"], "hello")

        status, interrupt = self.request("POST", f"/api/processes/{pid}/interrupt", {"body": "stop", "auto_run": False})
        self.assertEqual(status, 200)
        self.assertEqual(interrupt["message"]["kind"], "interrupt")

        status, snapshot = self.request("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(len(snapshot["processes"]), 1)
        self.assertGreaterEqual(snapshot["processes"][0]["unread_message_count"], 2)
        self.assertIn("tools", snapshot)

    def test_sse_replays_snapshot_event(self) -> None:
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}/api/events/stream?cursor=0",
            headers={"Authorization": "Bearer test-token"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            frame_lines: list[str] = []
            while len(frame_lines) < 3:
                line = response.readline().decode("utf-8").strip()
                if line:
                    frame_lines.append(line)
            self.assertTrue(frame_lines[0].startswith("id: "))
            self.assertEqual(frame_lines[1], "event: snapshot")
            self.assertTrue(frame_lines[2].startswith("data: "))

    def test_high_risk_exec_requires_confirmation(self) -> None:
        _status, spawned = self.request("POST", "/api/processes", {"goal": "goal", "auto_run": False})
        pid = spawned["pid"]

        status, denied = self.request("POST", f"/api/processes/{pid}/exec", {"image": "base-agent:v0", "goal": "new"})
        self.assertEqual(status, 409)
        self.assertTrue(denied["error"]["confirmation_required"])

        status, allowed = self.request(
            "POST",
            f"/api/processes/{pid}/exec",
            {"image": "base-agent:v0", "goal": "new", "confirmed": True, "auto_run": False},
        )
        self.assertEqual(status, 200)
        self.assertEqual(allowed["process"]["image_id"], "base-agent:v0")

    def test_high_risk_image_commit_requires_confirmation(self) -> None:
        _status, spawned = self.request("POST", "/api/processes", {"goal": "commit source", "auto_run": False})
        pid = spawned["pid"]
        status, created = self.request("POST", "/api/checkpoints/create", {"pid": pid, "reason": "commit"})
        self.assertEqual(status, 200)

        status, denied = self.request(
            "POST",
            "/api/images/commit",
            {
                "checkpoint_id": created["checkpoint_id"],
                "image_id": "gui-committed:v0",
                "name": "gui-committed",
            },
        )

        self.assertEqual(status, 409)
        self.assertTrue(denied["error"]["confirmation_required"])

        status, forbidden = self.request(
            "POST",
            "/api/images/commit",
            {
                "checkpoint_id": created["checkpoint_id"],
                "image_id": "gui-committed:v0",
                "name": "gui-committed",
                "actor": pid,
                "confirmed": True,
            },
        )
        self.assertEqual(status, 403)
        self.assertIn("lacks write", forbidden["error"]["message"])

    def test_scheduler_requests_are_serialized(self) -> None:
        first_status, first = self.request("POST", "/api/processes", {"goal": "goal", "auto_run": False})
        self.assertEqual(first_status, 200)
        pid = first["pid"]

        self.server.service.scheduler.running = True
        status, duplicate = self.request("POST", f"/api/processes/{pid}/run", {"max_quanta": 1})
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["running"])
        self.server.service.scheduler.running = False

    def test_process_run_targets_selected_process(self) -> None:
        _first_status, first = self.request("POST", "/api/processes", {"goal": "first", "auto_run": False})
        _second_status, second = self.request("POST", "/api/processes", {"goal": "second", "auto_run": False})
        seen: list[str] = []

        async def fake_quantum(pid: str) -> dict[str, str]:
            seen.append(pid)
            self.server.service.runtime.process.pause(pid, "fake quantum completed")
            return {"pid": pid}

        self.server.service.runtime.arun_process_once = fake_quantum  # type: ignore[method-assign]

        status, body = self.request("POST", f"/api/processes/{second['pid']}/run", {"max_quanta": 1})
        deadline = time.time() + 2.0
        while not seen and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(status, 200)
        self.assertEqual(body["reason"], f"run:{second['pid']}")
        self.assertEqual(seen, [second["pid"]])
        records = self.server.service.runtime.audit.trace()
        self.assertFalse(any(record.target == f"process:{first['pid']}" and record.action == "scheduler.run_quantum" for record in records))
        self.assertTrue(any(record.target == f"process:{second['pid']}" and record.action == "scheduler.run_quantum" for record in records))

    def test_jsonrpc_register_rejects_host_file_path(self) -> None:
        status, body = self.request("POST", "/api/jsonrpc/register", {"path": "secrets.yaml", "confirmed": True})

        self.assertEqual(status, 400)
        self.assertIn("manifest_text", body["error"]["message"])

    def test_request_body_size_is_bounded(self) -> None:
        status, body = self.request("POST", "/api/processes", {"goal": "x" * 1_100_000})

        self.assertEqual(status, 413)
        self.assertIn("exceeds", body["error"]["message"])

    def test_shutdown_endpoint_stops_http_server(self) -> None:
        try:
            status, body = self.request("POST", "/api/shutdown", {})
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "shutting_down")
        except ConnectionResetError:
            # Windows can reset the active HTTP connection when the standard
            # library server shuts itself down. The contract that matters for
            # the GUI main process is bounded server termination.
            pass
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())
        self.server.service.shutdown()


if __name__ == "__main__":
    unittest.main()
