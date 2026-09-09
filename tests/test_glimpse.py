"""Offline tests for glimpse. No network, no Portal, no paid model calls.

Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import glimpse  # noqa: E402


def make_file(dir_path, name, lines=0, content=None):
    p = Path(dir_path) / name
    if content is not None:
        p.write_text(content)
    elif lines == 0:
        p.touch()
    else:
        p.write_text("\n".join(f"line {i}" for i in range(lines)) + "\n")
    return p


class ReadHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for var in ("GLIMPSE_MIN_LINES", "GLIMPSE_MAX_BYTES"):
            os.environ.pop(var, None)

    def decision(self, **tool_input):
        return glimpse.check_read(tool_input)["hookSpecificOutput"]["permissionDecision"]

    def test_small_file_allowed(self):
        f = make_file(self.tmp.name, "small.txt", lines=100)
        self.assertEqual(self.decision(file_path=str(f)), "allow")

    def test_boundary_exact_threshold_allowed(self):
        f = make_file(self.tmp.name, "boundary.txt", lines=350)
        self.assertEqual(self.decision(file_path=str(f)), "allow")

    def test_just_over_threshold_denied(self):
        f = make_file(self.tmp.name, "over.txt", lines=351)
        self.assertEqual(self.decision(file_path=str(f)), "deny")

    def test_large_file_denied(self):
        f = make_file(self.tmp.name, "large.txt", lines=1200)
        self.assertEqual(self.decision(file_path=str(f)), "deny")

    def test_empty_file_allowed(self):
        f = make_file(self.tmp.name, "empty.txt", lines=0)
        self.assertEqual(self.decision(file_path=str(f)), "allow")

    def test_nonexistent_file_allowed(self):
        self.assertEqual(self.decision(file_path="/tmp/glimpse-does-not-exist.txt"), "allow")

    def test_empty_path_allowed(self):
        self.assertEqual(self.decision(file_path=""), "allow")

    def test_missing_file_path_field_allowed(self):
        self.assertEqual(glimpse.check_read({})["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_bounded_limit_allowed(self):
        f = make_file(self.tmp.name, "large.txt", lines=1200)
        self.assertEqual(self.decision(file_path=str(f), limit=50), "allow")

    def test_bounded_limit_at_threshold_allowed(self):
        f = make_file(self.tmp.name, "large.txt", lines=1200)
        self.assertEqual(self.decision(file_path=str(f), limit=350), "allow")

    def test_offset_alone_denied(self):
        # Deliberately stricter than upstream shunt: offset alone can still
        # stream to EOF, so it must not bypass the gate on its own.
        f = make_file(self.tmp.name, "large.txt", lines=1200)
        self.assertEqual(self.decision(file_path=str(f), offset=100), "deny")

    def test_oversized_limit_denied(self):
        # Closes the upstream bug where limit=999999 slipped through as
        # "targeted" on a file with far fewer lines than that.
        f = make_file(self.tmp.name, "large.txt", lines=1200)
        self.assertEqual(self.decision(file_path=str(f), limit=999999), "deny")

    def test_huge_single_line_file_denied(self):
        # Closes the upstream bug where a byte-huge but line-count-small file
        # (e.g. minified JS, one giant JSON line) passed the line-only gate.
        f = make_file(self.tmp.name, "huge.txt", content="x" * 500_000)
        self.assertEqual(self.decision(file_path=str(f)), "deny")

    def test_env_override_lower(self):
        os.environ["GLIMPSE_MIN_LINES"] = "200"
        self.addCleanup(os.environ.pop, "GLIMPSE_MIN_LINES", None)
        f = make_file(self.tmp.name, "medium.txt", lines=250)
        self.assertEqual(self.decision(file_path=str(f)), "deny")

    def test_env_override_higher(self):
        os.environ["GLIMPSE_MIN_LINES"] = "500"
        self.addCleanup(os.environ.pop, "GLIMPSE_MIN_LINES", None)
        f = make_file(self.tmp.name, "over.txt", lines=351)
        self.assertEqual(self.decision(file_path=str(f)), "allow")

    def test_env_non_numeric_falls_back(self):
        os.environ["GLIMPSE_MIN_LINES"] = "abc"
        self.addCleanup(os.environ.pop, "GLIMPSE_MIN_LINES", None)
        f = make_file(self.tmp.name, "over.txt", lines=351)
        self.assertEqual(self.decision(file_path=str(f)), "deny")


class BashHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for var in ("GLIMPSE_MIN_LINES", "GLIMPSE_MAX_BYTES"):
            os.environ.pop(var, None)

    def decision(self, command):
        return glimpse.check_bash({"command": command})["hookSpecificOutput"]["permissionDecision"]

    def test_cat_large_file_denied(self):
        f = make_file(self.tmp.name, "large.txt", lines=800)
        self.assertEqual(self.decision(f"cat {f}"), "deny")

    def test_cat_small_file_allowed(self):
        f = make_file(self.tmp.name, "small.txt", lines=100)
        self.assertEqual(self.decision(f"cat {f}"), "allow")

    def test_cat_with_flag_denied(self):
        f = make_file(self.tmp.name, "large.txt", lines=800)
        self.assertEqual(self.decision(f"cat -n {f}"), "deny")

    def test_head_large_file_denied(self):
        f = make_file(self.tmp.name, "large.txt", lines=800)
        self.assertEqual(self.decision(f"head {f}"), "deny")

    def test_head_dash_n_space_count_denied(self):
        # Upstream's parser mistook "800" for the file path here (separate
        # argv token after -n) and let it through. Fixed by treating -n as a
        # value-taking flag and skipping its argument.
        f = make_file(self.tmp.name, "large.txt", lines=800)
        self.assertEqual(self.decision(f"head -n 800 {f}"), "deny")

    def test_tail_large_file_denied(self):
        f = make_file(self.tmp.name, "large.txt", lines=800)
        self.assertEqual(self.decision(f"tail {f}"), "deny")

    def test_piped_command_allowed(self):
        f = make_file(self.tmp.name, "large.txt", lines=800)
        self.assertEqual(self.decision(f"cat {f} | grep export"), "allow")

    def test_redirect_allowed(self):
        f = make_file(self.tmp.name, "large.txt", lines=800)
        self.assertEqual(self.decision(f"cat {f} > /tmp/out.txt"), "allow")

    def test_non_read_command_allowed(self):
        self.assertEqual(self.decision("git status"), "allow")

    def test_quoted_path_with_space_denied(self):
        f = make_file(self.tmp.name, "large file.txt", lines=800)
        self.assertEqual(self.decision(f'cat "{f}"'), "deny")

    def test_multiple_operands_any_large_denied(self):
        # Closes the upstream bypass where `cat small.txt large.txt` only
        # inspected the first candidate path.
        small = make_file(self.tmp.name, "small.txt", lines=10)
        large = make_file(self.tmp.name, "large.txt", lines=800)
        self.assertEqual(self.decision(f"cat {small} {large}"), "deny")

    def test_nonexistent_file_allowed(self):
        self.assertEqual(self.decision("cat /tmp/glimpse-does-not-exist.txt"), "allow")

    def test_empty_command_allowed(self):
        self.assertEqual(self.decision(""), "allow")

    def test_unbalanced_quotes_does_not_crash(self):
        self.assertEqual(self.decision('cat "unterminated'), "allow")


class HookMainTests(unittest.TestCase):
    def run_hook(self, payload):
        import io
        stdin = io.StringIO(json.dumps(payload))
        stdout = io.StringIO()
        with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout):
            glimpse.hook_main()
        return json.loads(stdout.getvalue())

    def test_unknown_tool_allowed(self):
        out = self.run_hook({"tool_name": "Write", "tool_input": {}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_garbled_stdin_does_not_crash(self):
        import io
        stdin = io.StringIO("not json")
        stdout = io.StringIO()
        with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout):
            rc = glimpse.hook_main()
        self.assertEqual(rc, 0)
        out = json.loads(stdout.getvalue())
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "allow")


class WorkerInvocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def fake_popen(self, returncode=0, stdout="", stderr="", communicate_side_effect=None):
        proc = MagicMock()
        proc.returncode = returncode
        proc.pid = 999999  # unlikely to collide with a real pid
        if communicate_side_effect is not None:
            proc.communicate.side_effect = communicate_side_effect
        else:
            proc.communicate.return_value = (stdout, stderr)
        proc.poll.return_value = returncode  # already exited by the time we check
        return proc

    def test_successful_response_parsed(self):
        proc = self.fake_popen(stdout=json.dumps({"result": "- finding one", "total_cost_usd": 0.0025}))
        with patch("subprocess.Popen", return_value=proc):
            result, err = glimpse.invoke_worker("payload", "haiku", 30)
        self.assertIsNone(err)
        self.assertEqual(result["answer"], "- finding one")
        self.assertEqual(result["cost"], 0.0025)

    def test_is_error_response_rejected(self):
        proc = self.fake_popen(stdout=json.dumps({"result": "auth failed", "is_error": True}))
        with patch("subprocess.Popen", return_value=proc):
            result, err = glimpse.invoke_worker("payload", "haiku", 30)
        self.assertIsNone(result)
        self.assertIn("worker reported an error", err)

    def test_empty_answer_rejected(self):
        proc = self.fake_popen(stdout=json.dumps({"result": "  "}))
        with patch("subprocess.Popen", return_value=proc):
            result, err = glimpse.invoke_worker("payload", "haiku", 30)
        self.assertIsNone(result)
        self.assertIn("empty answer", err)

    def test_garbled_stdout_rejected(self):
        proc = self.fake_popen(stdout="not json at all")
        with patch("subprocess.Popen", return_value=proc):
            result, err = glimpse.invoke_worker("payload", "haiku", 30)
        self.assertIsNone(result)
        self.assertIn("unparseable", err)

    def test_nonzero_exit_rejected(self):
        proc = self.fake_popen(returncode=1, stderr="not authenticated")
        with patch("subprocess.Popen", return_value=proc):
            result, err = glimpse.invoke_worker("payload", "haiku", 30)
        self.assertIsNone(result)
        self.assertIn("not authenticated", err)

    def test_timeout_kills_the_worker_before_reporting_error(self):
        # Regression guard for the real bug: subprocess.run's own timeout
        # handling only kills the direct child, not any grandchildren it
        # spawned. This asserts our replacement path (group-kill) actually
        # runs on a timeout, not just that an error message comes back.
        import subprocess as sp
        proc = self.fake_popen(communicate_side_effect=sp.TimeoutExpired(cmd="claude", timeout=30))
        proc.poll.return_value = None  # still "alive" until wait() below runs

        def _wait_marks_dead(*_a, **_k):
            proc.poll.return_value = -9  # killed
            return -9
        proc.wait.side_effect = _wait_marks_dead

        with patch("subprocess.Popen", return_value=proc), \
             patch("os.killpg") as mock_killpg, \
             patch("os.getpgid", return_value=4242):
            result, err = glimpse.invoke_worker("payload", "haiku", 30)
        self.assertIsNone(result)
        self.assertIn("timed out", err)
        # Exactly once: the finally block must see the process is already
        # dead (via poll()) and not redundantly kill an already-reaped group.
        mock_killpg.assert_called_once()
        proc.wait.assert_called_once()

    def test_read_main_rejects_missing_file_without_calling_worker(self):
        with patch("subprocess.Popen") as mock_popen:
            rc = glimpse.read_main(["--question", "q?", "--paths", "/tmp/glimpse-nope.txt"])
        self.assertEqual(rc, 1)
        mock_popen.assert_not_called()

    def test_read_main_rejects_sensitive_path_without_calling_worker(self):
        f = make_file(self.tmp.name, "id_rsa", content="fake key material")
        with patch("subprocess.Popen") as mock_popen:
            rc = glimpse.read_main(["--question", "q?", "--paths", str(f)])
        self.assertEqual(rc, 1)
        mock_popen.assert_not_called()

    def test_read_main_rejects_oversized_payload_without_calling_worker(self):
        f = make_file(self.tmp.name, "big.txt", content="x" * 1000)
        with patch("subprocess.Popen") as mock_popen:
            rc = glimpse.read_main(["--question", "q?", "--paths", str(f), "--max-chars", "10"])
        self.assertEqual(rc, 1)
        mock_popen.assert_not_called()

    def test_read_main_success_prints_answer_only_on_stdout(self):
        import io
        proc = self.fake_popen(stdout=json.dumps({"result": "- the answer", "total_cost_usd": 0.001}))
        f = make_file(self.tmp.name, "small.txt", content="hello\n")
        out, err = io.StringIO(), io.StringIO()
        with patch("subprocess.Popen", return_value=proc), \
             patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
            rc = glimpse.read_main(["--question", "what is this?", "--paths", str(f)])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "- the answer")
        self.assertIn("worker=haiku", err.getvalue())


@unittest.skipUnless(glimpse._CAN_USE_PROCESS_GROUP, "process-group kill is POSIX-only")
class ProcessGroupKillTests(unittest.TestCase):
    """Not mocked: proves _kill_worker_tree reaches grandchildren.

    subprocess.run's own timeout handling (process.kill()) signals only the
    direct child PID. A grandchild the child forked before being killed is
    orphaned, not terminated. This is exactly the gap start_new_session +
    os.killpg closes -- proven here against a real process tree, not a mock.
    """

    def test_kill_worker_tree_kills_grandchild_too(self):
        pidfile = Path(tempfile.mkdtemp()) / "grandchild.pid"
        # Bash backgrounds a grandchild `sleep`, records its pid to a file,
        # then the shell itself also sleeps -- modeling a worker whose own
        # child process would outlive a plain kill() of the shell.
        proc = subprocess.Popen(
            ["bash", "-c", f"sleep 30 & echo $! > {pidfile}; sleep 30"],
            start_new_session=True,
        )
        try:
            for _ in range(50):
                if pidfile.exists():
                    break
                __import__("time").sleep(0.05)
            grandchild_pid = int(pidfile.read_text().strip())

            glimpse._kill_worker_tree(proc)

            self.assertIsNotNone(proc.poll(), "the shell itself should be dead")
            with self.assertRaises(ProcessLookupError):
                os.kill(grandchild_pid, 0)  # signal 0: existence check only
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    unittest.main()
