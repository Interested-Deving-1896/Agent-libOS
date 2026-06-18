from __future__ import annotations
import pytest
import tempfile
from pathlib import Path
from agent_libos import Runtime

class TestPersistentRuntime:

    def test_static_tool_ids_survive_runtime_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'runtime.sqlite'
            runtime = Runtime.open(db_path)
            try:
                pid = runtime.process.spawn(image='review-agent:v0', goal='persistent tool table')
                tool_id = runtime.process.get(pid).tool_table['get_current_time']
                assert runtime.tools.call(pid, 'get_current_time', {}).ok
            finally:
                runtime.close()
            reopened = Runtime.open(db_path)
            try:
                resolved = reopened.tools.resolve('get_current_time', pid=pid)
                result = reopened.tools.call(pid, 'get_current_time', {})
                assert resolved.tool_id == tool_id
                assert result.ok, result.error
                assert len([row for row in reopened.tools.list() if row['name'] == 'get_current_time']) == 1
            finally:
                reopened.close()
