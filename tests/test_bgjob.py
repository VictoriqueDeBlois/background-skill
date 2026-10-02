import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'background-job-continuation/scripts/bgjob.py'
FAKE = ROOT / 'tests/fixtures/fake_codex.py'
spec = importlib.util.spec_from_file_location('bgjob', SCRIPT)
bg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bg)


class Jobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='bgjob-test-')
        self.root = Path(self.tmp.name)
        self.sock = self.root / 'owner.sock'
        self.config = Path(str(self.sock) + '.json')
        self.tmux = self.root / 'tmux.sock'
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.bind(str(self.sock))
        self.thread = str(uuid.uuid4())
        self.state = {'thread_id': self.thread, 'cwd': str(self.root), 'loaded': True,
            'turns': [{'id': 'unrelated', 'status': 'completed', 'items': []}]}
        self.config.write_text(json.dumps(self.state))
        FAKE.chmod(0o755)
        (self.root / 'next.txt').write_text('Only inspect the result and report success.')

    def tearDown(self):
        subprocess.run(['tmux', '-S', str(self.tmux), 'kill-server'], capture_output=True)
        self.socket.close()
        self.tmp.cleanup()

    def edit(self, **fields):
        with self.config.with_suffix('.lock').open('a') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            data = json.loads(self.config.read_text())
            data.update(fields)
            bg.atomic(self.config, data)

    def cli(self, *args, check=True):
        process = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)
        if check:
            self.assertEqual(process.returncode, 0, process.stderr)
            return json.loads(process.stdout)
        return process

    def launch(self, name='job', exit_code=0, duration=0.05, keep_tmux=False, wait_timeout=5):
        jobdir = self.root / name
        code = f"from pathlib import Path; import time; p=Path({str(self.root / (name + '.count'))!r}); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); print('partial result'); time.sleep({duration}); raise SystemExit({exit_code})"
        result = self.cli('launch', '--cwd', self.root, '--thread', self.thread, '--socket', self.sock,
            '--codex', FAKE, '--job-dir', jobdir, '--next-file', self.root / 'next.txt',
            '--artifact', self.root / (name + '.count'), '--tmux-socket', self.tmux,
            '--lock-root', self.root / 'locks', '--poll-interval', '0.1',
            '--wait-timeout', str(wait_timeout), '--observe-timeout', '5', '--rpc-timeout', '2',
            *(['--keep-tmux'] if keep_tmux else []),
            '--', sys.executable, '-c', code)
        return jobdir, result

    def until(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.05)
        self.fail('Timed out waiting for observable state')

    def state_is(self, jobdir, *states):
        def condition():
            record = bg.read_json(jobdir / 'continuation.json', {})
            return record if record.get('state') in states else None
        return self.until(condition)

    def dead(self, session):
        return self.until(lambda: subprocess.run(['tmux', '-S', str(self.tmux), 'display-message', '-p', '-t', session + ':0.0', '#{pane_dead}'], capture_output=True, text=True).stdout.strip() == '1')

    def session_exists(self, session):
        return subprocess.run(['tmux', '-S', str(self.tmux), 'has-session', '-t', '=' + session], capture_output=True).returncode == 0

    def gone(self, session):
        return self.until(lambda: not self.session_exists(session))

    def count(self):
        return json.loads(self.config.read_text()).get('dispatch_count', 0)

    def test_success_no_events_paginated_exact_turn_closes_tmux(self):
        self.edit(page_size=1)
        jobdir, info = self.launch()
        record = self.state_is(jobdir, 'followup_completed')
        self.assertEqual(record['turn_id'], 'target-1')
        self.assertEqual(self.count(), 1)
        self.assertEqual(bg.read_json(jobdir / 'completion.json')['exit_code'], 0)
        self.assertEqual((jobdir / 'job.stdout.log').read_text().strip(), 'partial result')
        self.assertTrue(bg.read_json(jobdir / 'completion.json')['artifacts'][0]['exists'])
        self.gone(info['session'])
        self.assertEqual(self.cli('status', '--job-dir', jobdir)['continuation']['state'], 'followup_completed')

    def test_failed_job_keeps_partial_results_and_continues(self):
        jobdir, info = self.launch(exit_code=7)
        self.state_is(jobdir, 'followup_completed')
        self.gone(info['session'])
        self.assertEqual(bg.read_json(jobdir / 'completion.json')['exit_code'], 7)
        self.assertEqual((self.root / 'job.count').read_text(), '1')

    def test_active_approval_waits_until_idle(self):
        self.edit(active=True, flags=['waitingOnApproval'])
        jobdir, _ = self.launch()
        self.state_is(jobdir, 'waiting_approval')
        self.assertEqual(self.count(), 0)
        self.edit(active=False)
        self.state_is(jobdir, 'followup_completed')
        self.assertEqual(self.count(), 1)

    def test_workspace_mismatch_never_launches(self):
        result = self.cli('doctor', '--cwd', ROOT, '--thread', self.thread, '--socket', self.sock, '--codex', FAKE, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Workspace mismatch', result.stderr)
        self.assertEqual(self.count(), 0)

    def test_lost_reply_reconciles_once_and_recover_does_not_rerun(self):
        self.edit(drop_reply=True)
        jobdir, info = self.launch()
        self.state_is(jobdir, 'followup_completed')
        self.gone(info['session'])
        self.cli('recover', '--job-dir', jobdir)
        self.assertEqual(self.count(), 1)
        self.assertEqual((self.root / 'job.count').read_text(), '1')

    def test_rejected_dispatch_does_not_retry(self):
        self.edit(reject=True)
        jobdir, info = self.launch()
        self.state_is(jobdir, 'dispatch_failed')
        self.gone(info['session'])
        self.cli('recover', '--job-dir', jobdir)
        self.assertEqual(self.count(), 1)
        self.assertEqual(bg.read_json(jobdir / 'completion.json')['exit_code'], 0)

    def test_unloaded_owner_reloads_without_external_resume(self):
        jobdir, _ = self.launch(duration=0.4)
        self.until(lambda: bg.read_json(jobdir / 'lease.json'))
        self.edit(loaded=False)
        self.state_is(jobdir, 'followup_completed')
        self.assertGreaterEqual(bg.read_json(jobdir / 'lease.json')['reload_count'], 1)
        self.assertEqual(self.count(), 1)

    def test_killed_monitor_recovery_and_concurrent_hooks_do_not_redispatch(self):
        self.edit(delay=1.8)
        jobdir, info = self.launch()
        self.state_is(jobdir, 'followup_started')
        pid = int(subprocess.check_output(['tmux', '-S', str(self.tmux), 'display-message', '-p', '-t', info['session'] + ':0.0', '#{pane_pid}']))
        os.kill(pid, signal.SIGKILL)
        self.gone(info['session'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.cli('recover', '--job-dir', jobdir, check=False), range(2)))
        self.assertTrue(any(result.returncode == 0 for result in results))
        self.state_is(jobdir, 'followup_completed')
        self.assertEqual(self.count(), 1)
        self.assertEqual((self.root / 'job.count').read_text(), '1')

    def test_missing_completion_never_reruns_job(self):
        jobdir, info = self.launch()
        self.state_is(jobdir, 'followup_completed')
        self.gone(info['session'])
        (jobdir / 'completion.json').unlink()
        bg.atomic(jobdir / 'continuation.json', {'state': 'pending'})
        result = self.cli('recover', '--job-dir', jobdir)
        self.assertEqual(result['continuation']['state'], 'job_execution_unverified')
        self.assertEqual((self.root / 'job.count').read_text(), '1')

    def test_unknown_send_without_matching_marker_never_redispatches(self):
        jobdir, info = self.launch()
        self.state_is(jobdir, 'followup_completed')
        self.gone(info['session'])
        self.edit(turns=[{'id': 'unrelated', 'status': 'completed', 'items': []}])
        bg.atomic(jobdir / 'continuation.json', {'state': 'dispatching', 'dispatch_id': 'uncertain', 'marker': 'uncertain-marker'})
        manifest = bg.read_json(jobdir / 'job.json')
        manifest['wait_timeout'] = 0.3
        bg.atomic(jobdir / 'job.json', manifest)
        recovered = self.cli('recover', '--job-dir', jobdir)
        record = self.state_is(jobdir, 'dispatch_outcome_unknown')
        self.assertEqual(record['dispatch_id'], 'uncertain')
        self.until(lambda: bg.read_json(jobdir / 'continuation.json').get('wait_expired'))
        self.gone(recovered['session'])
        self.assertEqual(self.count(), 1)

    def test_failed_followup_is_not_success(self):
        self.edit(turn_status='failed')
        jobdir, info = self.launch()
        record = self.state_is(jobdir, 'followup_finished_with_error')
        self.gone(info['session'])
        self.assertEqual(record['turn_status'], 'failed')

    def test_two_jobs_same_thread_serialize_followups(self):
        self.edit(delay=0.4)
        first, _ = self.launch('first')
        second, _ = self.launch('second')
        self.state_is(first, 'followup_completed')
        self.state_is(second, 'followup_completed')
        self.assertEqual(self.count(), 2)
        self.assertNotEqual(bg.read_json(first / 'continuation.json')['turn_id'], bg.read_json(second / 'continuation.json')['turn_id'])

    def test_legacy_history_supported_without_pagination(self):
        self.edit(legacy=True)
        jobdir, _ = self.launch()
        self.state_is(jobdir, 'followup_completed')
        self.assertEqual(self.count(), 1)

    def test_ephemeral_history_is_rejected_before_computation(self):
        self.edit(ephemeral=True)
        result = self.cli('doctor', '--cwd', self.root, '--thread', self.thread,
            '--socket', self.sock, '--codex', FAKE, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('ephemeral', result.stderr)
        self.assertEqual(self.count(), 0)

    def test_reloaded_settings_change_prevents_dispatch(self):
        jobdir, _ = self.launch(duration=0.4)
        self.until(lambda: (jobdir / 'execution.json').exists())
        self.edit(loaded=False, settings={'sandbox': {'type': 'dangerFullAccess'}})
        self.state_is(jobdir, 'configuration_mismatch')
        self.assertEqual(self.count(), 0)
        self.assertEqual(bg.read_json(jobdir / 'completion.json')['exit_code'], 0)

    def test_writer_conflict_does_not_retry_resume_or_dispatch(self):
        jobdir, _ = self.launch(duration=0.4)
        self.until(lambda: bg.read_json(jobdir / 'lease.json'))
        self.edit(loaded=False, resume_reject=True)
        self.state_is(jobdir, 'owner_conflict')
        self.assertEqual(self.count(), 0)
        # Detection, worker subscription, and one rejected reload, without a retry loop.
        self.assertEqual(json.loads(self.config.read_text())['resume_count'], 3)

    def test_matching_completion_event_overcomes_stale_history(self):
        self.edit(stale_history=True, emit_completed_event=True)
        jobdir, _ = self.launch()
        record = self.state_is(jobdir, 'followup_completed')
        self.assertEqual(record['evidence_source'], 'turn/completed')
        self.assertEqual(record['turn_id'], 'target-1')

    def test_unrelated_completion_event_does_not_complete_target(self):
        self.edit(stale_history=True, emit_completed_event=True, wrong_completed_event=True)
        jobdir, _ = self.launch()
        self.state_is(jobdir, 'followup_started')
        self.assertEqual(bg.read_json(jobdir / 'continuation.json')['turn_status'], 'inProgress')
        self.assertFalse((jobdir / 'continuation.completed-event.json').exists())

    def test_disconnected_approval_is_pending_without_automatic_response(self):
        self.edit(delay=10, approval_request=True)
        jobdir, info = self.launch()
        record = self.state_is(jobdir, 'waiting_approval')
        self.assertEqual(bg.read_json(jobdir / 'completion.json')['exit_code'], 0)
        self.assertTrue(self.session_exists(info['session']))
        self.assertIn('item/commandExecution/requestApproval', record['pending_server_methods'])
        state = json.loads(self.config.read_text())
        self.assertEqual(state.get('server_reply_count', 0), 0)
        for turn in state['turns']:
            if turn['id'] == record['turn_id']:
                turn['ready_at'] = 0
        self.edit(turns=state['turns'], flags=[])
        self.state_is(jobdir, 'followup_completed')
        self.gone(info['session'])
        self.assertEqual(self.count(), 1)

    def test_keep_tmux_preserves_completed_pane_for_debugging(self):
        jobdir, info = self.launch(keep_tmux=True)
        self.state_is(jobdir, 'followup_completed')
        self.dead(info['session'])
        self.assertTrue(self.session_exists(info['session']))
        self.cli('recover', '--job-dir', jobdir)
        self.assertEqual(self.count(), 1)
        self.assertEqual((self.root / 'job.count').read_text(), '1')

    def test_cleanup_overrides_global_remain_on_exit_and_preserves_other_session(self):
        subprocess.run(['tmux', '-S', str(self.tmux), 'new-session', '-d', '-s', 'unrelated', 'sleep 60'], check=True, capture_output=True)
        subprocess.run(['tmux', '-S', str(self.tmux), 'set-option', '-g', 'remain-on-exit', 'on'], check=True, capture_output=True)
        jobdir, info = self.launch()
        self.state_is(jobdir, 'followup_completed')
        self.gone(info['session'])
        self.assertTrue(self.session_exists('unrelated'))
        self.assertEqual(subprocess.check_output(['tmux', '-S', str(self.tmux), 'show-option', '-gqv', 'remain-on-exit'], text=True).strip(), 'on')

    def test_expired_monitor_closes_tmux_and_legacy_recovery_does_not_rerun(self):
        self.edit(active=True, flags=['waitingOnApproval'])
        jobdir, info = self.launch(wait_timeout=0.8)
        self.state_is(jobdir, 'waiting_approval')
        self.assertTrue(self.session_exists(info['session']))
        self.until(lambda: bg.read_json(jobdir / 'continuation.json').get('wait_expired'))
        self.gone(info['session'])
        self.assertEqual(self.count(), 0)
        manifest = bg.read_json(jobdir / 'job.json')
        manifest.pop('keep_tmux', None)
        manifest['version'] = '2.1.0'
        bg.atomic(jobdir / 'job.json', manifest)
        self.edit(active=False, flags=[])
        recovered = self.cli('recover', '--job-dir', jobdir)
        self.state_is(jobdir, 'followup_completed')
        self.gone(recovered['session'])
        self.assertEqual(self.count(), 1)
        self.assertEqual((self.root / 'job.count').read_text(), '1')


if __name__ == '__main__':
    unittest.main(verbosity=2)
