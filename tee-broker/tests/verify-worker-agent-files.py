#!/usr/bin/env python3
"""Verify worker-agent.py includes staged INPUT_DIR files in the LLM prompt."""
from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import tempfile
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "worker" / "worker-agent.py"


class _FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({
            "choices": [{"message": {"content": "review ok"}}],
            "model": "test-model",
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }).encode("utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="worker-agent-files-") as td:
        root = Path(td)
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir()
        (input_dir / "BUGS.md").write_text("# Bug list\ncritical routing note", encoding="utf-8")
        (input_dir / "deploy.sh").write_text("#!/bin/sh\necho deploy-marker", encoding="utf-8")

        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["headers"] = dict(req.header_items())
            return _FakeResp()

        env = {
            "JOB_ID": "job-test-files",
            "SKILL_PROMPT": "Review these files:\n<<INPUT>>",
            "INPUT_DATA": "User note: please inspect attached files.",
            "INPUT_DIR": str(input_dir),
            "OUTPUT_DIR": str(output_dir),
            "NEMOCLAW_ENDPOINT_URL": "https://inference.local/v1",
            "NEMOCLAW_MODEL": "test-model",
            "COMPATIBLE_API_KEY": "jobtok_test",
        }
        old_env = os.environ.copy()
        buf = io.StringIO()
        try:
            os.environ.clear()
            os.environ.update(env)
            with mock.patch.object(urllib.request, "urlopen", fake_urlopen), contextlib.redirect_stdout(buf):
                try:
                    runpy.run_path(str(AGENT), run_name="__main__")
                except SystemExit as e:
                    if e.code not in (0, None):
                        raise AssertionError(f"worker-agent exited {e.code}: {buf.getvalue()}")
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        prompt = captured["body"]["messages"][0]["content"]
        assert "Attached input files (2 file(s))" in prompt, prompt
        assert "--- FILE: BUGS.md" in prompt, prompt
        assert "critical routing note" in prompt, prompt
        assert "--- FILE: deploy.sh" in prompt, prompt
        assert "deploy-marker" in prompt, prompt
        assert (output_dir / "output.txt").read_text(encoding="utf-8") == "review ok"
        result = json.loads(buf.getvalue())
        assert result["execution_mode"] if "execution_mode" in result else True
    print("OK worker-agent includes INPUT_DIR files in LLM prompt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
