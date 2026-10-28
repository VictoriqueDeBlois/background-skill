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

    def launch(self, name='job', exit_code=0, duration=0.05, keep_tmux=False, wait_timeout=5, workflow=None):
        jobdir = self.root / name
        code = f"from pathlib import Path; import time; p=Path({str(self.root / (name + '.count'))!r}); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); print('partial result'); time.sleep({duration}); raise SystemExit({exit_code})"
        result = self.cli('launch', '--cwd', self.root, '--thread', self.thread, '--socket', self.sock,
            '--codex', FAKE, '--job-dir', jobdir, '--next-file', self.root / 'next.txt',
            '--artifact', self.root / (name + '.count'), '--tmux-socket', self.tmux,
            '--lock-root', self.root / 'locks', '--poll-interval', '0.1',
            '--wait-timeout', str(wait_timeout), '--observe-timeout', '5', '--rpc-timeout', '2',
            *(['--keep-tmux'] if keep_tmux else []),
            *(['--workflow', workflow] if workflow else []),
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

    def pause(self, method):
        entered, release = self.root / 'rpc-entered', self.root / 'rpc-release'
        self.edit(pause_method=method, pause_entered=str(entered), pause_release=str(release))
        return entered, release

    def test_consumed_result_never_dispatches_after_chat_becomes_idle_or_recovery(self):
        self.edit(active=True)
        jobdir, info = self.launch()
        self.state_is(jobdir, 'waiting_idle')
        before = bg.read_json(jobdir / 'continuation.json')
        result = self.cli('ack', '--job-dir', jobdir, '--reason', 'Read and handled result in current turn')
        self.assertEqual(result['notification']['state'], 'acknowledged')
        self.assertEqual(result['notification']['delivery'], 'not_attempted')
        self.gone(info['session'])
        self.assertEqual(bg.read_json(jobdir / 'continuation.json'), before)
        self.edit(active=False)
        self.assertNotIn('session', self.cli('recover', '--job-dir', jobdir))
        self.assertEqual(self.count(), 0)
        self.assertFalse(self.cli('status', '--job-dir', jobdir)['worker_running'])
        self.assertEqual((self.root / 'job.count').read_text(), '1')

    def test_cancel_notification_preserves_running_computation_and_other_session(self):
        subprocess.run(['tmux', '-S', str(self.tmux), 'new-session', '-d', '-s', 'unrelated', 'sleep 60'], check=True, capture_output=True)
        jobdir, info = self.launch(duration=1.5)
        self.until(lambda: bg.read_json(jobdir / 'execution.json', {}).get('state') == 'running')
        execution = bg.read_json(jobdir / 'execution.json')
        premature = self.cli('ack', '--job-dir', jobdir, '--reason', 'too early', check=False)
        self.assertEqual(premature.returncode, 1)
        result = self.cli('cancel-notification', '--job-dir', jobdir, '--reason', 'User stopped notifications')
        self.assertEqual(result['notification']['state'], 'cancelled')
        self.assertTrue(result['worker_running'])
        os.kill(execution['pid'], 0)
        self.assertTrue(self.session_exists(info['session']))
        self.assertTrue(self.session_exists('unrelated'))
        self.until(lambda: bg.read_json(jobdir / 'completion.json'))
        self.gone(info['session'])
        self.assertEqual(bg.read_json(jobdir / 'completion.json')['exit_code'], 0)
        self.assertEqual((self.root / 'job.count').read_text(), '1')
        self.assertEqual((jobdir / 'job.stdout.log').read_text().strip(), 'partial result')
        self.assertNotIn('session', self.cli('recover', '--job-dir', jobdir))
        self.assertEqual(self.count(), 0)

    def test_cancellation_wins_during_last_history_query_before_send(self):
        self.edit(active=True)
        jobdir, info = self.launch()
        self.state_is(jobdir, 'waiting_idle')
        entered, release = self.pause('thread/turns/list')
        self.edit(active=False)
        self.until(entered.exists)
        result = self.cli('cancel-notification', '--job-dir', jobdir, '--reason', 'Cancel before send')
        self.assertEqual(result['notification']['delivery'], 'not_attempted')
        release.touch()
        self.gone(info['session'])
        self.assertEqual(self.count(), 0)
        self.assertFalse(bg.read_json(jobdir / 'continuation.json').get('dispatch_id'))

    def test_cancel_inflight_send_reports_delivery_and_preserves_exact_attempt(self):
        self.check_inflight_cancellation(drop_reply=False)

    def test_cancel_inflight_lost_reply_stays_unknown_and_never_resends(self):
        self.check_inflight_cancellation(drop_reply=True)

    def check_inflight_cancellation(self, drop_reply):
        self.edit(active=True, drop_reply=drop_reply, delay=30)
        jobdir, info = self.launch()
        self.state_is(jobdir, 'waiting_idle')
        entered, release = self.pause('turn/start')
        self.edit(active=False)
        self.until(entered.exists)
        attempt = bg.read_json(jobdir / 'continuation.json')
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.cli, 'cancel-notification', '--job-dir', jobdir, '--reason', 'Stop further delivery')
            time.sleep(0.1)
            self.assertFalse(future.done())
            release.touch()
            result = future.result(timeout=5)
        self.assertEqual(result['notification']['state'], 'cancelled')
        self.assertEqual(result['notification']['delivery'], 'dispatch_outcome_unknown' if drop_reply else 'already_dispatched')
        self.assertEqual(result['notification']['dispatch_id'], attempt['dispatch_id'])
        self.assertEqual(result['notification']['marker'], attempt['marker'])
        self.gone(info['session'])
        after = bg.read_json(jobdir / 'continuation.json')
        self.assertEqual(after['dispatch_id'], attempt['dispatch_id'])
        self.assertNotIn('session', self.cli('recover', '--job-dir', jobdir))
        self.assertEqual(self.count(), 1)
        self.assertEqual(len([t for t in json.loads(self.config.read_text())['turns'] if t['id'].startswith('target-')]), 1)

    def test_workflow_retries_replace_only_same_workflow_notification(self):
        self.edit(active=True)
        first, first_info = self.launch('attempt-1', workflow='repair')
        self.state_is(first, 'waiting_idle')
        second, second_info = self.launch('attempt-2', workflow='repair')
        self.state_is(second, 'waiting_idle')
        third, _ = self.launch('attempt-3', workflow='repair')
        self.state_is(third, 'waiting_idle')
        independent, _ = self.launch('independent', workflow='other-business')
        self.state_is(independent, 'waiting_idle')
        self.gone(first_info['session'])
        self.gone(second_info['session'])
        self.assertEqual(self.cli('status', '--job-dir', first)['notification']['state'], 'superseded')
        self.assertEqual(self.cli('status', '--job-dir', second)['notification']['state'], 'superseded')
        self.assertEqual(self.cli('status', '--job-dir', third)['workflow']['generation'], 3)
        self.assertNotIn('session', self.cli('recover', '--job-dir', first))
        self.edit(active=False)
        self.state_is(third, 'followup_completed')
        self.state_is(independent, 'followup_completed')
        self.assertEqual(self.count(), 2)
        for name in ('attempt-1', 'attempt-2', 'attempt-3', 'independent'):
            self.assertEqual((self.root / (name + '.count')).read_text(), '1')

    def test_superseded_running_job_keeps_computing(self):
        self.edit(active=True)
        first, info = self.launch('first', duration=1.8, workflow='repair')
        self.until(lambda: bg.read_json(first / 'execution.json', {}).get('state') == 'running')
        second, _ = self.launch('second', workflow='repair')
        self.state_is(second, 'waiting_idle')
        self.assertEqual(self.cli('status', '--job-dir', first)['notification']['state'], 'superseded')
        os.kill(bg.read_json(first / 'execution.json')['pid'], 0)
        self.until(lambda: bg.read_json(first / 'completion.json'))
        self.gone(info['session'])
        self.assertEqual(bg.read_json(first / 'completion.json')['exit_code'], 0)
        self.cli('ack', '--job-dir', second, '--reason', 'Handled final result')
        self.edit(active=False)
        self.assertNotIn('session', self.cli('recover', '--job-dir', first))
        self.assertEqual(self.count(), 0)

    def test_workflow_replacement_wins_before_old_send(self):
        self.edit(active=True)
        first, info = self.launch('first', workflow='repair')
        self.state_is(first, 'waiting_idle')
        entered, release = self.pause('thread/turns/list')
        self.edit(active=False)
        self.until(entered.exists)
        # Preflight for the replacement also queries history, so release only that
        # new proxy from the fixture barrier. The old response remains paused.
        self.edit(pause_method=None, active=True)
        second, _ = self.launch('replacement', workflow='repair')
        release.touch()
        self.gone(info['session'])
        self.state_is(second, 'waiting_idle')
        self.assertEqual(self.count(), 0)
        self.cli('ack', '--job-dir', second, '--reason', 'Handled replacement')
        self.edit(active=False)
        self.assertNotIn('session', self.cli('recover', '--job-dir', first))

    def test_workflow_replacement_after_inflight_send_preserves_old_delivery(self):
        self.edit(active=True, delay=30)
        first, info = self.launch('first', workflow='repair')
        self.state_is(first, 'waiting_idle')
        entered, release = self.pause('turn/start')
        self.edit(active=False)
        self.until(entered.exists)
        with ThreadPoolExecutor(max_workers=1) as pool:
            replacement = pool.submit(self.launch, 'replacement', workflow='repair')
            self.until(lambda: (self.root / 'replacement').is_dir())
            self.assertFalse(replacement.done())
            release.touch()
            second, _ = replacement.result(timeout=5)
        self.gone(info['session'])
        state = self.cli('status', '--job-dir', first)
        self.assertEqual(state['notification']['state'], 'superseded')
        self.assertEqual(state['notification']['delivery'], 'already_dispatched')
        self.assertEqual(state['continuation']['turn_id'], 'target-1')
        self.state_is(second, 'waiting_idle')
        self.cli('cancel-notification', '--job-dir', second, '--reason', 'Stop replacement')
        self.assertNotIn('session', self.cli('recover', '--job-dir', first))
        self.assertEqual(self.count(), 1)

    def test_ack_failed_result_means_handled_not_success(self):
        self.edit(active=True)
        jobdir, info = self.launch(exit_code=7)
        self.state_is(jobdir, 'waiting_idle')
        state = self.cli('ack', '--job-dir', jobdir, '--reason', 'Inspected partial failure')
        self.assertEqual(state['completion']['exit_code'], 7)
        self.assertEqual(state['notification']['state'], 'acknowledged')
        self.assertEqual(state['continuation']['state'], 'waiting_idle')
        self.gone(info['session'])
        self.assertEqual(self.count(), 0)

    def test_notification_controls_are_idempotent_and_keep_first_reason(self):
        self.edit(active=True)
        jobdir, info = self.launch()
        self.state_is(jobdir, 'waiting_idle')
        first = self.cli('cancel-notification', '--job-dir', jobdir, '--reason', 'Original stop')['notification']
        second = self.cli('ack', '--job-dir', jobdir, '--reason', 'Later handling')['notification']
        self.assertEqual(first, second)
        self.gone(info['session'])
        self.assertEqual(self.count(), 0)

    def test_legacy_live_worker_control_refuses_false_cancellation(self):
        self.edit(active=True)
        jobdir, info = self.launch()
        self.state_is(jobdir, 'waiting_idle')
        (jobdir / 'worker.json').unlink()
        result = self.cli('cancel-notification', '--job-dir', jobdir, '--reason', 'Stop', check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('legacy worker', result.stderr)
        self.assertFalse((jobdir / 'notification.json').exists())
        self.assertTrue(self.session_exists(info['session']))

    def test_legacy_stopped_job_can_be_acknowledged_without_reviving_notification(self):
        self.edit(active=True)
        jobdir, info = self.launch(wait_timeout=0.4)
        self.until(lambda: bg.read_json(jobdir / 'continuation.json', {}).get('wait_expired'))
        self.gone(info['session'])
        manifest = bg.read_json(jobdir / 'job.json')
        manifest['version'] = '2.1.1'
        bg.atomic(jobdir / 'job.json', manifest)
        (jobdir / 'worker.json').unlink()
        self.cli('ack', '--job-dir', jobdir, '--reason', 'Handled old result')
        self.edit(active=False)
        self.assertNotIn('session', self.cli('recover', '--job-dir', jobdir))
        self.assertEqual(self.count(), 0)

    def test_current_state_does_not_retain_transient_error_or_expiry(self):
        bg.put_state(self.root, 'waiting_idle', error='Earlier contention', wait_expired=True)
        bg.put_state(self.root, 'followup_started', turn_id='exact-turn')
        record = bg.read_json(self.root / 'continuation.json')
        self.assertNotIn('error', record)
        self.assertNotIn('wait_expired', record)
        self.assertEqual(record['turn_id'], 'exact-turn')


if __name__ == '__main__':
    unittest.main(verbosity=2)
