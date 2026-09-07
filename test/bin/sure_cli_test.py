import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
BRIDGE = runpy.run_path(str(ROOT / "bin/sure-mcp"))


class SureMcpTest(unittest.TestCase):
    def exchange(self, messages, *, failure=None):
        output = io.StringIO()
        response = {"jsonrpc": "2.0", "id": 7, "result": {"tools": []}}
        with (
            patch.dict(BRIDGE["main"].__globals__, get_token=lambda: "test-token"),
            patch("sys.stdin", io.StringIO(messages)),
            patch("sys.stdout", output),
            patch.dict(BRIDGE["main"].__globals__, post=unittest.mock.Mock(
                return_value=response, side_effect=failure
            )),
        ):
            BRIDGE["main"]()
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_invalid_input_does_not_break_following_request(self):
        invalid = [None, 1, [], "hello", {}, {"jsonrpc": "2.0", "method": 3},
                   {"jsonrpc": "2.0", "method": "ping", "id": True}]
        ping = {"jsonrpc": "2.0", "id": "after-invalid", "method": "ping"}
        for value in invalid:
            with self.subTest(value=value):
                replies = self.exchange(json.dumps(value) + "\n" + json.dumps(ping) + "\n")
                self.assertEqual(replies[0]["error"]["code"], -32600)
                self.assertIsNone(replies[0]["id"])
                self.assertEqual(replies[1], {"jsonrpc": "2.0", "id": "after-invalid", "result": {}})

    def test_parse_error_and_blank_lines(self):
        replies = self.exchange('\n{broken\n{"jsonrpc":"2.0","id":0,"method":"ping"}\n')
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["id"], 0)
        self.assertEqual(replies[1]["result"], {})

    def test_forwarded_request(self):
        replies = self.exchange('{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n')
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 7, "result": {"tools": []}}])

    def test_notifications_never_reply(self):
        messages = '\n'.join(json.dumps({"jsonrpc": "2.0", "method": method})
                             for method in ["notifications/initialized", "ping"])
        for failure in [None, urllib.error.URLError("unavailable")]:
            with self.subTest(failure=failure):
                self.assertEqual(self.exchange(messages, failure=failure), [])

    def test_http_authentication_and_server_errors_are_distinct(self):
        for status in [401, 403, 404, 500]:
            with self.subTest(status=status):
                failure = urllib.error.HTTPError("http://localhost/mcp", status, "error", {}, None)
                reply = self.exchange('{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n', failure=failure)[0]
                self.assertEqual(reply["id"], 7)
                self.assertEqual(reply["error"]["code"], -32000)
                message = reply["error"]["message"]
                self.assertIn(f"HTTP {status}", message)
                self.assertEqual("authentication failed" in message, status in [401, 403])
                self.assertNotIn("connection failed", message)

    def test_connection_and_timeout_errors(self):
        for failure, expected in [(urllib.error.URLError("refused"), "Cannot connect"),
                                  (TimeoutError(), "timed out")]:
            with self.subTest(failure=failure):
                reply = self.exchange('{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n', failure=failure)[0]
                self.assertIn(expected, reply["error"]["message"])

    def test_missing_token_is_reported_without_crashing(self):
        output = io.StringIO()
        with (
            patch.dict(BRIDGE["main"].__globals__, get_token=lambda: ""),
            patch("sys.stdin", io.StringIO('{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n')),
            contextlib.redirect_stdout(output),
        ):
            BRIDGE["main"]()
        self.assertIn("MCP_API_TOKEN not found", json.loads(output.getvalue())["error"]["message"])

    def test_token_environment_precedence(self):
        with patch.dict(os.environ, {"SURE_MCP_TOKEN": "preferred", "MCP_API_TOKEN": "fallback"}, clear=True):
            self.assertEqual(BRIDGE["get_token"](), "preferred")
            del os.environ["SURE_MCP_TOKEN"]
            self.assertEqual(BRIDGE["get_token"](), "fallback")


class SureRunnerTest(unittest.TestCase):
    def test_minimal_path_symlink_arguments_cwd_and_exit_status(self):
        with tempfile.TemporaryDirectory(prefix="sure runner ") as directory:
            home = Path(directory)
            mise = home / ".local/bin/mise"
            mise.parent.mkdir(parents=True)
            mise.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@"\nexit 23\n')
            mise.chmod(0o700)
            runner = home / "sure-runner"
            runner.symlink_to(ROOT / "bin/sure-runner")
            ruby = 'puts "argument with spaces"'
            result = subprocess.run(
                [str(runner), ruby], cwd=home, capture_output=True, text=True,
                env={**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin"}, timeout=10,
            )
            self.assertEqual(result.returncode, 23)
            self.assertEqual(result.stderr, "")
            self.assertEqual(result.stdout.splitlines(), [str(ROOT), "exec", "--", "bin/rails", "runner", ruby])


if __name__ == "__main__":
    unittest.main()
