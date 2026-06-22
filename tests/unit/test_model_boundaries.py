from __future__ import annotations

import json

import pytest

from agent_libos import Runtime
from agent_libos.models import AgentImage
from agent_libos.models.exceptions import ValidationError


def test_store_reports_invalid_persisted_process_status_with_context() -> None:
    runtime = Runtime.open("local")
    try:
        pid = runtime.process.spawn(image="base-agent:v0", goal="bad persisted process status")
        runtime.store.conn.execute("UPDATE processes SET status = ? WHERE pid = ?", ("definitely_bad", pid))
        runtime.store.conn.commit()

        with pytest.raises(ValidationError, match=f"invalid persisted process {pid}"):
            runtime.store.get_process(pid)
    finally:
        runtime.close()


def test_image_registry_rejects_invalid_persisted_image_manifest() -> None:
    runtime = Runtime.open("local")
    try:
        image_id = "persisted-invalid:v0"
        runtime.register_image(AgentImage(image_id=image_id, name="persisted-invalid"), actor="test")
        runtime.store.conn.execute(
            "UPDATE images SET manifest_json = ? WHERE image_id = ?",
            (json.dumps({"image_id": image_id, "name": "persisted-invalid", "prompt_mode": "ambient"}), image_id),
        )
        runtime.store.conn.commit()

        with pytest.raises(ValidationError, match="invalid persisted agent image persisted-invalid:v0"):
            runtime.image_registry.list_images()
    finally:
        runtime.close()


def test_jsonrpc_registry_revalidates_persisted_endpoint_models() -> None:
    runtime = Runtime.open("local")
    try:
        endpoint_id = "persisted-jsonrpc"
        runtime.jsonrpc.register_endpoint_from_yaml_text(
            f"""
schema_version: 1
endpoint_id: {endpoint_id}
url: https://api.example.test/jsonrpc
methods:
  - method_id: echo
    rpc_method: demo.echo
    right: read
    rollback_class: no_rollback_required
    state_mutation: false
    information_flow: true
timeout_s: 5
max_request_bytes: 65536
max_response_bytes: 1048576
""".lstrip(),
            actor="test",
            require_capability=False,
        )
        bad_spec = {
            "schema_version": 1,
            "endpoint_id": endpoint_id,
            "url": "https://api.example.test/jsonrpc",
            "headers": {},
            "methods": [
                {
                    "method_id": "echo",
                    "rpc_method": "demo.echo",
                    "right": "read",
                    "rollback_class": "no_rollback_required",
                    "state_mutation": "false",
                    "information_flow": True,
                }
            ],
            "timeout_s": 5,
            "max_request_bytes": 65536,
            "max_response_bytes": 1048576,
        }
        runtime.store.conn.execute(
            "UPDATE jsonrpc_endpoints SET spec_json = ? WHERE endpoint_id = ?",
            (json.dumps(bad_spec), endpoint_id),
        )
        runtime.store.conn.commit()

        with pytest.raises(ValidationError, match="invalid persisted JSON-RPC endpoint persisted-jsonrpc"):
            runtime.jsonrpc.list_endpoints(require_capability=False)
    finally:
        runtime.close()
