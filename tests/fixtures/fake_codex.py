#!/usr/bin/env python3
"""Controlled raw WebSocket proxy fixture. Never invokes Codex or a model."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time

config = Path(sys.argv[sys.argv.index('--sock') + 1] + '.json')
inp, out = sys.stdin.buffer, sys.stdout.buffer


def take(n):
    data = b''
    while len(data) < n:
        chunk = inp.read(n - len(data))
        if not chunk:
            sys.exit(0)
        data += chunk
    return data


def frame(op, data, final=True):
    n = len(data)
    header = bytes([(0x80 if final else 0) | op])
    header += bytes([n]) if n < 126 else bytes([126]) + struct.pack('!H', n) if n < 65536 else bytes([127]) + struct.pack('!Q', n)
    out.write(header + data)
    out.flush()


def respond(message):
    data = json.dumps(message).encode()
    frame(1, data[:7], final=False)
    frame(9, b'fixture-ping')
    frame(0, data[7:])


headers = b''
while not headers.endswith(b'\r\n\r\n'):
    headers += take(1)
key = next(line.split(':', 1)[1].strip() for line in headers.decode().split('\r\n') if line.lower().startswith('sec-websocket-key:'))
accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
out.write(f'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n'.encode())
out.flush()
data = b''
while True:
    first, second = take(2)
    size = second & 127
    if size == 126:
        size = struct.unpack('!H', take(2))[0]
    elif size == 127:
        size = struct.unpack('!Q', take(8))[0]
    mask = take(4) if second & 128 else None
    payload = take(size)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if first & 15 in (9, 10):
        continue
    data += payload
    if not first & 128:
        continue
    request = json.loads(data)
    data = b''
    if 'id' not in request:
        continue
    if 'method' not in request:
        with config.with_suffix('.lock').open('a') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            state = json.loads(config.read_text())
            state['server_reply_count'] = state.get('server_reply_count', 0) + 1
            temporary = config.with_suffix('.tmp')
            temporary.write_text(json.dumps(state))
            os.replace(temporary, config)
        continue
    params = request.get('params', {})
    method = request['method']
    drop = False
    error = None
    notifications = []
    with config.with_suffix('.lock').open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        state = json.loads(config.read_text())
        for turn in state.get('turns', []):
            if turn.get('ready_at', float('inf')) <= time.time():
                if not state.get('stale_history'):
                    turn['status'] = state.get('turn_status', 'completed')
        runtime_active = state.get('active') or any(t.get('status') == 'inProgress' and t.get('ready_at', float('inf')) > time.time() for t in state.get('turns', []))
        settings = {'model': 'fixture-model', 'modelProvider': 'fixture', 'reasoningEffort': 'high',
            'approvalPolicy': 'on-request', 'approvalsReviewer': 'human', 'sandbox': {'type': 'readOnly'},
            'cwd': state['cwd'], 'activePermissionProfile': {'id': ':read-only', 'extends': None},
            'disabledPluginIds': [], 'serviceTier': None, **state.get('settings', {})}
        if method == 'initialize':
            result = {'userAgent': 'fixture', 'codexHome': os.environ.get('CODEX_HOME'), 'platformFamily': 'unix', 'platformOs': 'linux'}
        elif method == 'thread/read':
            result = {'thread': {'id': state['thread_id'], 'cwd': state['cwd'],
                'status': {'type': 'active', 'activeFlags': state.get('flags', [])} if runtime_active else {'type': 'idle' if state.get('loaded', True) else 'notLoaded'},
                'turns': state.get('turns', []) if params.get('includeTurns') else []}}
        elif method == 'thread/loaded/list':
            result = {'data': [state['thread_id']] if state.get('loaded', True) else [], 'nextCursor': None}
        elif method == 'thread/resume':
            state['resume_count'] = state.get('resume_count', 0) + 1
            if not state.get('loaded', True) and state.get('resume_reject'):
                error = {'code': -32600, 'message': 'thread already has an active writer'}
                result = {}
            else:
                state['loaded'] = True
                result = {'thread': {'id': state['thread_id'], 'cwd': state['cwd']}, **settings}
        elif method == 'thread/unsubscribe':
            result = {'status': 'unsubscribed'}
        elif method == 'thread/turns/list':
            if state.get('ephemeral'):
                error = {'code': -32600, 'message': 'ephemeral threads do not support thread/turns/list'}
                result = {}
            elif state.get('legacy'):
                error = {'code': -32601, 'message': 'Unsupported method'}
                result = {}
            else:
                for turn in state.get('turns', []):
                    if turn.get('ready_at', float('inf')) <= time.time():
                        if not state.get('stale_history'):
                            turn['status'] = state.get('turn_status', 'completed')
                        turn['items'] = turn.get('items', []) + ([] if any(i.get('type') == 'agentMessage' for i in turn.get('items', [])) else [{'id': 'answer', 'type': 'agentMessage', 'text': 'FIXTURE_OK'}])
                        if state.get('emit_completed_event'):
                            notifications.append({'method': 'turn/completed', 'params': {'threadId': state['thread_id'], 'turn': {**turn, 'id': 'wrong-turn' if state.get('wrong_completed_event') else turn['id'], 'status': state.get('turn_status', 'completed')}}})
                start = int(params.get('cursor', 0))
                limit = state.get('page_size', 100)
                result = {'data': state.get('turns', [])[start:start + limit],
                    'nextCursor': str(start + limit) if start + limit < len(state.get('turns', [])) else None}
        elif method == 'turn/start':
            state['dispatch_count'] = state.get('dispatch_count', 0) + 1
            if state.get('reject') or runtime_active:
                error = {'code': -32600, 'message': 'already has an active writer'}
                result = {}
            else:
                turn = {'id': 'target-' + str(state['dispatch_count']), 'status': 'inProgress', 'ready_at': time.time() + state.get('delay', 0),
                    'items': [{'id': 'completion-prompt', 'type': 'userMessage', 'clientId': params.get('clientUserMessageId'), 'content': params['input']}]}
                state.setdefault('turns', []).insert(0, turn)
                result = {'turn': turn}
                if state.get('approval_request'):
                    state['flags'] = ['waitingOnApproval']
                    notifications.append({'id': 'approval-1', 'method': 'item/commandExecution/requestApproval',
                        'params': {'threadId': state['thread_id'], 'turnId': turn['id']}})
                drop = state.get('drop_reply', False)
                state['drop_reply'] = False
        else:
            result = {}
            error = {'code': -32601, 'message': method}
        temporary = config.with_suffix('.tmp')
        temporary.write_text(json.dumps(state))
        os.replace(temporary, config)
    if drop:
        sys.exit(0)
    for message in notifications:
        respond(message)
    respond({'id': request['id'], **({'error': error} if error else {'result': result})})
