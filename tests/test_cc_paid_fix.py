import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/cc_paid_fix.py"
spec = importlib.util.spec_from_file_location("cc_paid_fix", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

# Deliberately a real local subprocess: checks stdin, argument boundaries, child
# environment and filesystem records without ever reaching an API.
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
if '--version' in sys.argv:
    print('2.1.316 (Claude Code)')
    sys.exit(0)
if '--help' in sys.argv:
    print('Usage: claude [options]\n' + os.environ.get('FAKE_HELP', ''))
    sys.exit(0)
args = sys.argv[1:]
prompt = sys.stdin.read()
Path(os.environ['CAPTURE']).write_text(json.dumps({
    'args': args, 'prompt': prompt,
    'auth': os.environ.get('ANTHROPIC_API_KEY'),
    'base_url': os.environ.get('ANTHROPIC_BASE_URL'),
    'oauth': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'),
    'nested': os.environ.get('CLAUDECODE'),
    'bedrock': os.environ.get('CLAUDE_CODE_USE_BEDROCK'),
    'custom_key': os.environ.get('CLAUDE_CREDITS_API_KEY'),
    'entrypoint': os.environ.get('CLAUDE_CODE_ENTRYPOINT'),
    'scrub': os.environ.get('CLAUDE_CODE_SUBPROCESS_ENV_SCRUB'),
    'cwd': os.getcwd(),
}))
mode = os.environ.get('FAKE_MODE', '')
if mode == 'malformed':
    print('not a result')
    sys.exit(0)
if '--fork-session' in args:
    session = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
elif '--resume' in args:
    session = args[args.index('--resume') + 1]
else:
    session = args[args.index('--session-id') + 1]
subtype = {'error': 'error_during_execution', 'subtype_failure': 'error_during_execution',
           'max_budget': 'error_max_budget_usd', 'max_turns': 'error_max_turns'}.get(mode, 'success')
print(json.dumps({'type': 'result', 'subtype': subtype, 'session_id': session,
    'is_error': mode in ('error', 'max_budget'), 'result': 'Review complete.',
    'errors': ['budget reached'] if mode == 'error' else [],
    'permission_denials': [{'tool_name': 'Write'}] if mode == 'denied' else [],
    'num_turns': 3, 'total_cost_usd': float(os.environ.get('FAKE_COST', '0.42'))}))
sys.exit(1 if mode in ('nonzero', 'max_budget') else 0)
'''


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cc-paid-fix-tests-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        binary = self.directory / "bin"
        binary.mkdir()
        fake = binary / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o700)
        self.capture = self.directory / "capture.json"
        self.home = self.directory / "state"
        self.env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                    "CLAUDE_CREDITS_HOME": str(self.home), "CAPTURE": str(self.capture),
                    "CLAUDE_CREDITS_API_KEY": "test-dedicated-key",
                    "ANTHROPIC_API_KEY": "wrong-parent-key",
                    "ANTHROPIC_BASE_URL": "https://wrong-gateway.invalid",
                    "CLAUDE_CODE_OAUTH_TOKEN": "wrong-oauth", "CLAUDECODE": "1",
                    "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
                    "FAKE_HELP": " ".join(runner.required_flags())}

    def invoke(self, action, request=None, *args, env=None, cwd=None):
        result = subprocess.run([sys.executable, str(SCRIPT), action, *args],
                                input=json.dumps(request) if request is not None else "",
                                capture_output=True, text=True, cwd=cwd or self.directory,
                                env=env or self.env, timeout=15)
        return result, json.loads(result.stdout) if result.stdout else None

    def write_record(self, record, response=None):
        folder = self.home / "runs" / record["run_id"]
        folder.mkdir(parents=True)
        (folder / "run.json").write_text(json.dumps(record))
        if response is not None:
            (folder / "response.json").write_text(json.dumps(response))
        return folder

    def test_default_child_auth_and_tools_are_isolated(self):
        result, record = self.invoke("run", {"task": "Review the code", "handoff": "Use SQLite, not Postgres"})
        self.assertEqual(result.returncode, 0, result.stderr)
        capture = json.loads(self.capture.read_text())
        self.assertEqual(capture["auth"], "test-dedicated-key")
        for key in ["base_url", "oauth", "nested", "bedrock", "custom_key"]:
            self.assertIsNone(capture[key], key)
        self.assertEqual(capture["entrypoint"], "cli")
        args = capture["args"]
        self.assertIn("--bare", args)
        self.assertIn("--restricted", args)
        self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep")
        # Grants are path-scoped: a bare "Read" would allow reading ~/.ssh and similar.
        self.assertEqual(args[args.index("--allowedTools") + 1], "Read(./**)")
        self.assertEqual(args[args.index("--disallowedTools") + 1], "mcp__*")
        self.assertEqual(capture["scrub"], "1")
        self.assertEqual(args[args.index("--setting-sources") + 1], "")
        self.assertEqual(args[args.index("--max-budget-usd") + 1], "10")
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertNotIn("--resume", args)
        self.assertEqual(json.loads(capture["prompt"])["context_handoff"], "Use SQLite, not Postgres")
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["session_cost_usd_estimate"], 0.42)
        self.assertEqual(record["run_cost_usd_estimate"], 0.42)
        self.assertEqual(record["stop_reason"], "success")
        self.assertEqual(record["num_turns"], 3)
        self.assertIsInstance(record["runner_pid"], int)
        self.assertIsInstance(record["worker_pid"], int)
        self.assertNotIn("test-dedicated-key", result.stdout + result.stderr)
        folder = Path(record["artifacts"])
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for path in folder.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, str(path))

    def no_env_key(self):
        return {k: v for k, v in self.env.items() if k != "CLAUDE_CREDITS_API_KEY"}

    def test_setup_stores_key_in_private_file_and_run_uses_it(self):
        key = "sk-test-" + "a" * 30
        env = self.no_env_key()
        result = subprocess.run([sys.executable, str(SCRIPT), "setup"], input=key + "\n", capture_output=True,
                                text=True, cwd=self.directory, env=env, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["stored_in"], "file")
        self.assertNotIn(key, result.stdout + result.stderr)
        path = self.home / "key"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        _, doctor = self.invoke("doctor", env=env)
        self.assertEqual(doctor["key_source"], "file")
        self.assertNotIn(key, json.dumps(doctor))
        result, _ = self.invoke("run", {"task": "Review"}, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.capture.read_text())["auth"], key)

    def test_env_key_wins_over_stored_key(self):
        self.home.mkdir(mode=0o700)
        (self.home / "key").write_text("sk-test-" + "b" * 30)
        (self.home / "key").chmod(0o600)
        _, doctor = self.invoke("doctor")
        self.assertEqual(doctor["key_source"], "env")

    def test_setup_rejects_malformed_key_and_stores_nothing(self):
        for bad in ["", "short", "has space " + "x" * 30, "quote\"" + "x" * 30, "semi;colon" + "x" * 30]:
            with self.subTest(bad=bad):
                result = subprocess.run([sys.executable, str(SCRIPT), "setup"], input=bad + "\n",
                                        capture_output=True, text=True, cwd=self.directory,
                                        env=self.no_env_key(), timeout=15)
                self.assertEqual(result.returncode, 2)
                self.assertFalse((self.home / "key").exists())

    def test_group_readable_key_file_is_refused(self):
        self.home.mkdir(mode=0o700)
        (self.home / "key").write_text("sk-test-" + "c" * 30)
        (self.home / "key").chmod(0o644)
        result, _ = self.invoke("run", {"task": "Review"}, env=self.no_env_key())
        self.assertEqual(result.returncode, 2)
        self.assertIn("chmod 600", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_logout_removes_stored_key(self):
        self.home.mkdir(mode=0o700)
        (self.home / "key").write_text("sk-test-" + "d" * 30)
        (self.home / "key").chmod(0o600)
        result, out = self.invoke("logout", env=self.no_env_key())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(out["removed_from"], ["file"])
        self.assertFalse((self.home / "key").exists())
        _, doctor = self.invoke("doctor", env=self.no_env_key())
        self.assertFalse(doctor["key_present"])

    def test_missing_dedicated_key_never_falls_back(self):
        env = {k: v for k, v in self.env.items() if k != "CLAUDE_CREDITS_API_KEY"}
        result, _ = self.invoke("run", {"task": "Review"}, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("CLAUDE_CREDITS_API_KEY", result.stderr)
        self.assertIn("setup", result.stderr)
        self.assertFalse(self.capture.exists())
        self.assertFalse(self.home.exists())

    def test_full_context_forks_parent(self):
        parent = str(uuid.uuid4())
        result, record = self.invoke("run", {"task": "Second opinion", "context": "full", "parent_session_id": parent})
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.capture.read_text())["args"]
        self.assertEqual(args[args.index("--resume") + 1], parent)
        self.assertIn("--fork-session", args)
        self.assertNotIn("--session-id", args)
        self.assertNotEqual(record["session_id"], parent)

    def test_fresh_omits_handoff(self):
        result, _ = self.invoke("run", {"task": "Review", "context": "none", "handoff": "Should not be sent"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(json.loads(self.capture.read_text())["prompt"])["context_handoff"], "")

    def test_explicit_tools_only(self):
        result, _ = self.invoke("run", {"task": "Implement", "edit": True, "allow_bash": True})
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.capture.read_text())["args"]
        self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep,Edit,Write,Bash")
        self.assertEqual(args[args.index("--allowedTools") + 1], "Read(./**),Edit(./**),Bash")
        denied = args[args.index("--disallowedTools") + 1].split(",")
        for rule in ["mcp__*", "Edit(./.git/**)", "Edit(./.claude/**)", "Edit(./.mcp.json)", "Edit(./.envrc)"]:
            self.assertIn(rule, denied)
        self.assertEqual(json.loads(self.capture.read_text())["scrub"], "1")

    def test_edit_without_bash_has_no_shell_grant(self):
        result, _ = self.invoke("run", {"task": "Implement", "edit": True})
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.capture.read_text())["args"]
        self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep,Edit,Write")
        self.assertEqual(args[args.index("--allowedTools") + 1], "Read(./**),Edit(./**)")

    def test_followup_resumes_specific_worker_and_retains_permissions(self):
        _, original = self.invoke("run", {"task": "Review", "budget_usd": 2})
        result, followup = self.invoke("followup", {"run_id": original["run_id"], "task": "Check finding 2", "budget_usd": 3})
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.capture.read_text())["args"]
        self.assertEqual(args[args.index("--resume") + 1], original["session_id"])
        self.assertNotIn("--continue", args)
        self.assertNotIn("--fork-session", args)
        self.assertEqual(args[args.index("--max-budget-usd") + 1], "3")
        self.assertNotEqual(original["run_id"], followup["run_id"])
        self.assertEqual(runner.identifier(followup["session_id"]), original["session_id"])

    def test_followup_permission_escalation_rejected(self):
        _, original = self.invoke("run", {"task": "Review"})
        result, _ = self.invoke("followup", {"run_id": original["run_id"], "task": "Edit now", "edit": True})
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown request fields", result.stderr)

    def test_semantic_failure_and_nonzero_exit_fail(self):
        for mode in ["error", "nonzero", "malformed", "subtype_failure"]:
            with self.subTest(mode=mode):
                result, record = self.invoke("run", {"task": "Review"}, env={**self.env, "FAKE_MODE": mode})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(record["status"], "failed")

    def test_turn_or_budget_limit_is_incomplete_not_completed(self):
        for mode, reason in [("max_budget", "error_max_budget_usd"), ("max_turns", "error_max_turns")]:
            with self.subTest(mode=mode):
                result, record = self.invoke("run", {"task": "Review"}, env={**self.env, "FAKE_MODE": mode})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(record["status"], "incomplete")
                self.assertEqual(record["stop_reason"], reason)
                self.assertEqual(record["result"], "Review complete.")

    def test_followup_records_per_invocation_cost(self):
        _, original = self.invoke("run", {"task": "Review"})
        result, second = self.invoke("followup", {"run_id": original["run_id"], "task": "More"},
                                     env={**self.env, "FAKE_COST": "1.0"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(second["previous_session_cost_usd_estimate"], 0.42)
        self.assertAlmostEqual(second["run_cost_usd_estimate"], 0.58)
        self.assertEqual(second["session_cost_usd_estimate"], 1.0)
        # Uses the worker's latest run, and copes if the CLI reports per-invocation cost.
        _, third = self.invoke("followup", {"run_id": original["run_id"], "task": "Again"},
                               env={**self.env, "FAKE_COST": "0.25"})
        self.assertEqual(third["previous_session_cost_usd_estimate"], 1.0)
        self.assertEqual(third["run_cost_usd_estimate"], 0.25)
        self.assertIn("cost_note", third)

    def test_permission_denials_are_visible(self):
        result, record = self.invoke("run", {"task": "Review"}, env={**self.env, "FAKE_MODE": "denied"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(record["permission_denials"], [{"tool_name": "Write"}])

    def test_invalid_requests_do_not_launch(self):
        for request in [{"task": ""}, {"task": "Test", "budget_usd": -1},
                        {"task": "Test", "budget_usd": True}, {"task": "Test", "budget_usd": float("nan")},
                        {"task": "Test", "edit": "true"}, {"task": "Test", "context": "full"},
                        {"task": "Test", "context": "everything"}, {"task": "Test", "context": []},
                        {"task": "Test", "effort": "invalid"}, {"task": "Test", "effort": []},
                        {"task": "Test", "unrecognized": True}, {"task": "Test", "model": "--help"},
                        {"task": "Test", "cwd": "relative/path"}, {"task": "Test", "cwd": 7},
                        {"task": "Test", "cwd": str(self.directory / "missing")}]:
            with self.subTest(request=request):
                result, _ = self.invoke("run", request)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.capture.exists())

    def test_dry_run_needs_no_auth_and_writes_nothing(self):
        env = {k: v for k, v in self.env.items() if k != "CLAUDE_CREDITS_API_KEY"}
        task = 'Review $(touch PWNED); `echo no` "quotes"\nnext line'
        result, plan = self.invoke("run", {"task": task}, "--dry-run", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(plan["api_called"])
        self.assertEqual(json.loads(plan["prompt"])["delegated_task"], task)
        self.assertFalse(self.home.exists())
        self.assertFalse(self.capture.exists())
        self.assertFalse((self.directory / "PWNED").exists())

    def test_doctor_and_list_are_local(self):
        result, doctor = self.invoke("doctor")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(doctor["key_present"])
        self.assertFalse(doctor["api_called"])
        self.assertEqual(doctor["flags_missing_from_help"], [])
        _, runs = self.invoke("list")
        self.assertEqual(runs, [])
        self.assertFalse(self.capture.exists())

    def test_doctor_reports_flags_missing_from_help(self):
        result, doctor = self.invoke("doctor", env={**self.env, "FAKE_HELP": "--bare --print --resume-later"})
        self.assertEqual(result.returncode, 0, result.stderr)
        missing = doctor["flags_missing_from_help"]
        self.assertIn("--restricted", missing)
        self.assertIn("--resume", missing)  # "--resume-later" must not count as "--resume".
        self.assertNotIn("--bare", missing)
        self.assertNotIn("--max-turns", missing)  # Known hidden flag.
        self.assertFalse(self.capture.exists())

    def test_show_validates_id(self):
        result, _ = self.invoke("show", None, "../../outside")
        self.assertEqual(result.returncode, 2)
        self.assertIn("UUID", result.stderr)

    def test_followup_missing_original_directory(self):
        _, original = self.invoke("run", {"task": "Review"})
        path = Path(original["artifacts"]) / "run.json"
        original["cwd"] = str(self.directory / "deleted-project")
        path.write_text(json.dumps(original))
        result, _ = self.invoke("followup", {"run_id": original["run_id"], "task": "Continue"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("working directory", result.stderr)

    def test_followup_runs_in_original_directory(self):
        _, original = self.invoke("run", {"task": "Review"})
        elsewhere = self.directory / "elsewhere"
        elsewhere.mkdir()
        result, followup = self.invoke("followup", {"run_id": original["run_id"], "task": "Continue"}, cwd=elsewhere)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(followup["cwd"], original["cwd"])
        self.assertEqual(json.loads(self.capture.read_text())["cwd"], original["cwd"])

    def test_run_uses_requested_directory(self):
        project = self.directory / "project"
        project.mkdir()
        result, record = self.invoke("run", {"task": "Review", "cwd": str(project)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(record["cwd"], str(project.resolve()))
        self.assertEqual(json.loads(self.capture.read_text())["cwd"], str(project.resolve()))

    def test_stale_running_records_are_reconciled(self):
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait()
        base = {"created_at": "2026-01-01T00:00:00+00:00", "cwd": str(self.directory), "status": "running",
                "session_id": str(uuid.uuid4()), "runner_pid": exited.pid, "worker_pid": None,
                "options": {"task": "Review", "context": "summary"}}
        finished = {**base, "run_id": str(uuid.uuid4())}
        self.write_record(finished, {"type": "result", "subtype": "success", "is_error": False,
                                     "result": "Done.", "total_cost_usd": 0.3})
        result, record = self.invoke("show", None, finished["run_id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["run_cost_usd_estimate"], 0.3)
        with patch.dict(os.environ, self.env):
            self.assertEqual(runner.load_run(finished["run_id"])["status"], "completed")

        lost = {**base, "run_id": str(uuid.uuid4())}
        self.write_record(lost)
        result, record = self.invoke("show", None, lost["run_id"])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(record["status"], "failed")
        self.assertIn("runner exited", record["error"])

        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(worker.wait)
        self.addCleanup(worker.kill)
        orphan = {**base, "run_id": str(uuid.uuid4()), "worker_pid": worker.pid}
        folder = self.write_record(orphan)
        result, runs = self.invoke("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({item["run_id"]: item["status"] for item in runs}[orphan["run_id"]], "orphaned")
        # Not finalized while the worker may still write its result.
        self.assertEqual(json.loads((folder / "run.json").read_text())["status"], "running")

    def test_worker_lock_blocks_overlapping_followups(self):
        with patch.dict(os.environ, self.env):
            session = str(uuid.uuid4())
            with runner.worker_lock(session):
                with self.assertRaisesRegex(ValueError, "already running"):
                    with runner.worker_lock(session):
                        self.fail("lock should not have been acquired")

    def test_launch_failure_saved_as_failed_not_running(self):
        with patch.dict(os.environ, self.env), patch.object(runner, "cli_version", return_value=("/missing/claude", "2.1.316")):
            record = runner.execute_task(runner.validate({"task": "Review"}))
            self.assertEqual(record["status"], "failed")
            self.assertIn("Could not start Claude", record["error"])
            self.assertEqual(runner.load_run(record["run_id"])["status"], "failed")

    def test_timeout_stops_process_and_records_failure(self):
        with patch.dict(os.environ, self.env), patch.object(runner, "cli_version", return_value=("claude", "2.1.316")), \
             patch.object(runner.subprocess, "Popen") as launch, patch.object(runner, "stop") as stop:
            process = launch.return_value
            process.pid = 4321
            process.communicate.side_effect = subprocess.TimeoutExpired("claude", 1800)
            process.returncode = -2
            record = runner.execute_task(runner.validate({"task": "Review"}))
            stop.assert_called_once_with(process)
            self.assertEqual(record["status"], "failed")
            self.assertIn("Timed out", record["error"])

    def test_sigterm_stops_worker_records_failure_and_restores_handlers(self):
        original = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
        with patch.dict(os.environ, self.env), patch.object(runner, "cli_version", return_value=("claude", "2.1.316")), \
             patch.object(runner.subprocess, "Popen") as launch, patch.object(runner, "stop") as stop:
            process = launch.return_value
            process.pid = 4321
            process.returncode = -15
            process.communicate.side_effect = lambda *args, **kwargs: os.kill(os.getpid(), signal.SIGTERM)
            record = runner.execute_task(runner.validate({"task": "Review"}))
            stop.assert_called_once_with(process)
            self.assertEqual(record["status"], "failed")
            self.assertIn("SIGTERM", record["error"])
            self.assertEqual(runner.load_run(record["run_id"])["worker_pid"], 4321)
        for sig, handler in original.items():
            self.assertEqual(signal.getsignal(sig), handler)


@unittest.skipUnless(os.environ.get("CC_PAID_FIX_REAL_CLI") == "1",
                     "set CC_PAID_FIX_REAL_CLI=1 to check the installed claude --help (local, no API call)")
class RealCliTests(unittest.TestCase):
    def test_installed_cli_lists_every_flag(self):
        executable = shutil.which("claude")
        if not executable:
            self.skipTest("claude is not on PATH")
        self.assertEqual(runner.missing_flags(executable), [])


if __name__ == "__main__":
    unittest.main()
