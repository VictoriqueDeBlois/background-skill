"""Explicit real smoke test: creates and deletes only its temporary test thread."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--script', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--disconnect-client', action='store_true', help='Close the original frontend before job completion')
    parser.add_argument('--force-unload', action='store_true', help='Archive/unarchive only the test thread to force notLoaded')
    parser.add_argument('--workspace-profile', action='store_true', help='Use the workspace permission profile with automatic approval review, scoped to the temporary test directory')
    args = parser.parse_args()
    script = Path(args.script).resolve()
    spec = importlib.util.spec_from_file_location('bgjob_live', script)
    bg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bg)
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    endpoint = str(Path(bg.home()) / 'app-server-control/app-server-control.sock')
    tmux_socket = root / 'tmux.sock'
    result = {'started_at': bg.now(), 'installed_script': str(script), 'socket': endpoint}
    owner = None
    try:
        owner = bg.Rpc('codex', endpoint, logs=root / 'setup-rpc', timeout=20)
        start_params = {'cwd': str(root), 'ephemeral': False,
            'permissions': ':workspace' if args.workspace_profile else ':read-only',
            'approvalPolicy': 'on-request'}
        if args.workspace_profile:
            start_params.update(approvalsReviewer='auto_review', runtimeWorkspaceRoots=[str(root)])
        created = owner.call('thread/start', start_params)
        result['test_permission_profile'] = start_params['permissions']
        thread = created['thread']
        result['thread_id'] = thread['id']
        bg.atomic(root / 'thread.json', {'thread': thread, 'temporary_test_thread': True})
        # Fresh persisted threads have no readable history until their first user message.
        bootstrap = owner.call('turn/start', {'threadId': thread['id'], 'input': [
            {'type': 'text', 'text': 'This is a temporary transport test. Reply BGJOB_TEST_READY. Do not use tools or change files.'}]})
        result['bootstrap_turn_id'] = bootstrap['turn']['id']
        ready_deadline = time.monotonic() + 180
        while time.monotonic() < ready_deadline:
            try:
                ready = next((t for t in bg.turns(owner, thread['id']) if t['id'] == result['bootstrap_turn_id']), None)
            except bg.RpcError as exc:
                # Native storage can lag the first turn/start reply. Never resend that turn.
                if 'is empty' not in str(exc) and 'not materialized' not in str(exc):
                    raise
                time.sleep(1)
                continue
            if ready and ready.get('status') in {'completed', 'failed', 'interrupted'}:
                if ready['status'] != 'completed':
                    raise RuntimeError('Test bootstrap turn failed: ' + json.dumps(ready.get('error')))
                break
            time.sleep(1)
        else:
            raise RuntimeError('Test bootstrap turn timed out')
        result['preflight'] = bg.verify(owner, thread['id'], str(root))
        # Establish read-only history support before starting the harmless job.
        bg.turns(owner, thread['id'])
        (root / 'next-task.txt').write_text(
            'This is an authorized integration smoke test. Read completion.json and job.stdout.log '
            'at the paths above. If exit_code is 0 and stdout contains BGJOB_COMPUTATION_OK, '
            'respond with BGJOB_LIVE_SMOKE_OK. Do not modify files, run more jobs or create chats.\n')
        jobdir = root / 'job'
        command = [sys.executable, str(script), 'launch', '--cwd', str(root),
            '--thread', thread['id'], '--job-dir', str(jobdir), '--next-file', str(root / 'next-task.txt'),
            '--tmux-socket', str(tmux_socket), '--lock-root', str(root / 'locks'),
            '--poll-interval', '1', '--wait-timeout', '30', '--observe-timeout', '180',
            '--', sys.executable, '-c', "import time; time.sleep(8); print('BGJOB_COMPUTATION_OK')"]
        launched = subprocess.run(command, capture_output=True, text=True, check=True)
        result['launch'] = json.loads(launched.stdout)
        print(json.dumps({'thread_id': thread['id'], 'launch': result['launch']}, ensure_ascii=False), flush=True)
        lease_deadline = time.monotonic() + 20
        while time.monotonic() < lease_deadline and not (jobdir / 'lease.json').exists():
            time.sleep(0.1)
        if not (jobdir / 'lease.json').exists():
            raise RuntimeError('Worker did not establish its persistent original-server subscription')
        if args.force_unload:
            owner.call('thread/archive', {'threadId': thread['id']})
            owner.call('thread/unarchive', {'threadId': thread['id']})
            unloaded = bg.verify(owner, thread['id'], str(root))
            result['forced_unload_observation'] = unloaded
            if unloaded['loaded'] or unloaded['status']['type'] != 'notLoaded':
                raise RuntimeError('Test server did not unload the test thread')
        if args.disconnect_client:
            result['client_disconnected_at'] = bg.now()
            owner.close()
            owner = None
            print(json.dumps({'original_frontend_disconnected': True, 'forced_unload': args.force_unload}), flush=True)
        deadline = time.monotonic() + 210
        while time.monotonic() < deadline:
            continuation = bg.read_json(jobdir / 'continuation.json', {})
            if continuation.get('state') in bg.FINAL or continuation.get('wait_expired'):
                result['continuation'] = continuation
                break
            time.sleep(1)
        else:
            raise RuntimeError('Smoke test timed out')
        result['completion'] = bg.read_json(jobdir / 'completion.json')
        evidence = bg.read_json(jobdir / 'continuation.evidence.json', {})
        response = '\n'.join(item.get('text', '') for item in evidence.get('turn', {}).get('items', []) if item.get('type') == 'agentMessage')
        result['response'] = response
        result['lease'] = bg.read_json(jobdir / 'lease.json', {})
        result['passed'] = (result['continuation']['state'] == 'followup_completed'
            and result['continuation'].get('delivery') == 'new-turn'
            and result['completion']['exit_code'] == 0 and 'BGJOB_LIVE_SMOKE_OK' in response)
        if args.force_unload:
            result['passed'] = result['passed'] and result['lease'].get('reload_count', 0) >= 1
        if args.disconnect_client:
            result['passed'] = result['passed'] and owner is None
        if not result['passed']:
            raise RuntimeError('Real job/continuation evidence did not meet smoke-test criteria')
        print(json.dumps({'passed': True, 'turn_id': result['continuation']['turn_id'], 'response': response}, ensure_ascii=False), flush=True)
    except Exception as exc:
        result.update(passed=False, error=str(exc))
        print(json.dumps({'passed': False, 'error': str(exc)}, ensure_ascii=False), flush=True)
    finally:
        if not owner and result.get('thread_id'):
            try:
                owner = bg.Rpc('codex', endpoint, timeout=20)
            except Exception as exc:
                result['cleanup_note'] = str(exc)
        if owner:
            if result.get('thread_id'):
                try:
                    # Delete only the thread created by this invocation, never a user's thread.
                    if not result.get('passed'):
                        job_record = bg.read_json(root / 'job/continuation.json', {})
                        target_id = job_record.get('turn_id') or result.get('bootstrap_turn_id')
                        if target_id:
                            try:
                                target = next((t for t in bg.turns(owner, result['thread_id']) if t['id'] == target_id), None)
                                if target and target.get('status') == 'inProgress':
                                    owner.call('turn/interrupt', {'threadId': result['thread_id'], 'turnId': target['id']})
                            except bg.RpcError as exc:
                                result['interrupt_note'] = str(exc)
                    owner.call('thread/delete', {'threadId': result['thread_id']})
                    result['test_thread_deleted'] = True
                except Exception as exc:
                    result['cleanup_note'] = str(exc)
            owner.close()
        subprocess.run(['tmux', '-S', str(tmux_socket), 'kill-server'], capture_output=True)
        result['ended_at'] = bg.now()
        bg.atomic(root / 'smoke-result.json', result)
    return 0 if result.get('passed') else 1


if __name__ == '__main__':
    sys.exit(main())
