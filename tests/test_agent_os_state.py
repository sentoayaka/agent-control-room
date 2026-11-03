from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "agent_os_state.py"
SPEC = importlib.util.spec_from_file_location("agent_os_state", MODULE_PATH)
assert SPEC and SPEC.loader
agent_os_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent_os_state)


class RegistryTests(unittest.TestCase):
    def test_conflicts_detect_workspace_and_exclusive_resource(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = agent_os_state.empty_state()
            for task_id in ("alpha", "beta"):
                state["tasks"].append(
                    {
                        "id": task_id,
                        "status": "running",
                        "workspace": "/tmp/shared",
                        "exclusive_resources": ["host:local/gpu:0"],
                    }
                )
            agent_os_state.save_state(path, state)
            loaded = agent_os_state.load_state(path)
            owners = {}
            for task in loaded["tasks"]:
                for value in [task["workspace"], *task["exclusive_resources"]]:
                    owners.setdefault(value, []).append(task["id"])
            self.assertEqual(owners["/tmp/shared"], ["alpha", "beta"])
            self.assertEqual(owners["host:local/gpu:0"], ["alpha", "beta"])

    def test_state_round_trip_preserves_heartbeat_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = agent_os_state.empty_state()
            state["tasks"].append(
                {
                    "id": "example",
                    "status": "running",
                    "thread_status": "active",
                    "last_turn_status": "inProgress",
                    "last_summary": "Running checks",
                }
            )
            agent_os_state.save_state(path, state)
            loaded = agent_os_state.load_state(path)
            self.assertEqual(loaded["tasks"][0]["thread_status"], "active")
            self.assertEqual(loaded["tasks"][0]["last_turn_status"], "inProgress")


if __name__ == "__main__":
    unittest.main()
