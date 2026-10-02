---
name: background-job-continuation
description: Run Linux jobs in tmux and automatically continue their original Codex thread while a local or SSH client is disconnected, using a persistent subscription to the verified owning app-server. Use for explicit background wakeup or recovery requests; not ordinary background jobs or manual handoff notes.
---

# Continue after a Linux background job

Use the bundled Python 3 helper instead of writing an ad hoc hook. It requires Linux, tmux, and a Codex CLI with `app-server proxy`. Run on the **execution host**, as the original thread's user and Codex home. SSH presence is not thread ownership; local CLI and remote desktop clients can both use a shared app-server.

Keep the original thread, workspace, model, permissions and authorized next task. Never select the target with `--last`, a title, or the most recent history file. A completed turn does not mean its writer was released.

## Prepare and launch

1. Resolve the absolute helper path below. Take the exact current thread ID from trusted tool context or `CODEX_THREAD_ID`; the helper verifies it. If missing, obtain the exact ID. Supply `--socket` for a custom server; otherwise the helper checks the effective Codex home's control socket and `BACKGROUND_JOB_APP_SERVER_SOCKET`.
2. Run `doctor`. It must verify exact ID, canonical workspace, readable persisted history and loaded runtime state on one unambiguous owner. It captures effective model, approval, sandbox and permission-profile settings through that owner's `thread/resume`. Read [protocol and recovery](references/protocol.md) for failed detection or version limitations. Do not start another server to load an occupied thread.
3. Write a small UTF-8 file specifying the **already authorized next task**, with criteria for interpreting partial results. The helper supplies completion, log, exit-code and artifact paths. A failed batch does not authorize rerunning completed work.
4. Launch. Pass command arguments after `--`; use an explicit `bash -lc` only when the job needs shell syntax. Choose durable output outside disposable worktrees when necessary. Do not include secrets in arguments or capture the whole environment.

```bash
python3 /absolute/skill/scripts/bgjob.py doctor --cwd /absolute/workspace
python3 /absolute/skill/scripts/bgjob.py launch \
  --cwd /absolute/workspace \
  --job-dir /absolute/workspace/.background-jobs/run-001 \
  --next-file /absolute/next-task.txt \
  --artifact /absolute/results \
  -- python3 /absolute/workspace/scripts/evaluate.py
```

The helper creates a visible `bgjob-<id>` tmux session. Its worker pane closes by default when computation and continuation monitoring have ended; a session containing only that window exits with it. Do not close tmux merely because computation has finished: the worker must remain alive until monitoring finishes or reaches its wait limit. Results and logs remain in the job directory. Add `--keep-tmux` at launch only when an exited pane is wanted for debugging; recovery preserves that choice. The helper changes only its own pane's `remain-on-exit`, not global settings or other sessions. Before executing the job, its worker establishes a persistent subscription to the original app-server; `lease.json` records it. Task, subscription and monitor run on the execution host independently of the SSH client. Give the user the exact attach command, logs and job directory from launch. tmux does not survive reboot or protect against the host forcibly killing user processes.

## Dispatch and verify

`completion.json` is atomically written on job success or failure **before** continuation. `continuation.json` tracks continuation independently. The worker keeps its original-server connection open during computation and continuation. If the thread becomes `notLoaded`, it reloads and subscribes through **that same server's** `thread/resume`, verifies the saved settings, then waits for idle and sends the completion prompt. It does not need the SSH client to reconnect. A settings mismatch or active-writer conflict stops dispatch and preserves results. It never automatically falls back to external `exec resume`.

Per-job and per-thread locks serialize helper dispatches. A durable attempt marker is written **before** send. Request IDs and `clientUserMessageId` are correlation aids, not assumed idempotency keys. A lost reply enters `dispatch_outcome_unknown`; recovery searches for that exact marker instead of resending. This prevents blind retries, without promising distributed exactly-once execution.

Initiation requires the exact returned turn ID and proof that the completion prompt belongs to it. Completion requires that same turn's final status, from a matching `turn/completed` notification or a read-only turn query. An idle thread, latest completed turn, queue response or exit code is insufficient. Report failed/interrupted turns and approval waits separately. A Codex turn completing does not prove the business objective succeeded. The continuing turn must finish normally; its external monitor records final status afterward, so never wait for your own completion.

## Inspect and recover

```bash
python3 /absolute/skill/scripts/bgjob.py status --job-dir /absolute/job-directory
python3 /absolute/skill/scripts/bgjob.py recover --job-dir /absolute/job-directory
```

`recover` restarts only the monitor in tmux. It never reruns a job with an execution attempt but no completion record, or redispatches a known/ambiguous continuation attempt. If the job vanished without durable completion, inspect artifacts and decide separately whether rerunning any work is authorized. Diagnose explicit RPC rejection before arranging a new attempt.

Connection recovery has bounded waits and records the boundary. Recover after endpoint availability is restored. Normal approvals and client-owned tools can still require a connected UI; leave such requests pending without approving, rejecting or bypassing them. Native server tools can run while the UI is absent. Writer conflicts are continuation failures, not computation failures. Do not remove locks, edit Codex databases or restart the user's server to force access.

Automatic continuation requires the original app-server to keep running on the execution host, with access to its model provider. SSH or frontend disconnection is supported while that server remains alive. Do not promise continuation after the user explicitly quits the App; it depends on whether the original server stays alive. Unexpected server exit is a recovery scenario: computation can continue and save results while the host and tmux worker remain alive, and bounded reconnection or manual `recover` may restore monitoring. App-exit and server-restart recovery have not been validated. App-start scanning, automatic server restart and service installation are outside the current scope.

Old or `--no-daemon` CLI sessions without an owned endpoint cannot guarantee immediate automatic continuation. Report failed preflight or `waiting_endpoint`. Use the same host's supported shared-server mode for future runs. Legacy job manifests without settings snapshots cannot safely reload an unloaded thread or start a new continuation; they can only observe an existing attempt. External CLI resume is a separately diagnosed fallback requiring actual writer release and configuration preservation.

Read [protocol and recovery](references/protocol.md) for subscription, recovery and version details. Report job and continuation outcomes separately with their evidence files.
