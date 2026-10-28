---
name: background-job-continuation
description: Run Linux jobs in tmux and automatically continue their original Codex thread while a local or SSH client is disconnected, using a persistent subscription to the verified owning app-server. Use for explicit background wakeup or recovery requests; not ordinary background jobs or manual handoff notes.
---

# Continue after a Linux background job

Use the bundled Python 3 helper instead of writing an ad hoc hook. It requires Linux, tmux, and a Codex CLI with `app-server proxy`. Run on the **execution host**, as the original thread's user and Codex home. SSH presence is not thread ownership; local CLI and remote desktop clients can both use a shared app-server.

Keep the original thread, workspace, model, permissions and authorized next task. Never select the target with `--last`, a title, or the most recent history file. A completed turn does not mean its writer was released.

## Choose one notification owner

For related parallel jobs or a repair/retry loop, launch **one workflow coordinator** that waits for its children, persists their results and exits with an actionable aggregate. Run children as ordinary subprocesses or tmux jobs; registering a chat wakeup for every child creates a backlog while the chat is busy. Independently actionable workflows may have separate notifications.

Give the coordinator a stable `--workflow` ID. Launching a replacement with the same ID advances its generation and suppresses older notifications within the same host/user/Codex home, thread, workspace and lock root. It preserves their computations and evidence. This selects the latest notification owner; it does not aggregate child results for you. Use different IDs for independent workflows. Without `--workflow`, jobs remain independent for compatibility.

Keep the saved next task short and point it to a current durable checkpoint. The continuing turn must reload that checkpoint before acting: old counts, failures and plans may already have been superseded. Per-job deduplication and per-thread locks cannot determine whether results have been handled.

## Prepare and launch

1. Resolve the absolute helper path below. Take the exact current thread ID from trusted tool context or `CODEX_THREAD_ID`; the helper verifies it. If missing, obtain the exact ID. Supply `--socket` for a custom server; otherwise the helper checks the effective Codex home's control socket and `BACKGROUND_JOB_APP_SERVER_SOCKET`.
2. Run `doctor`. It must verify exact ID, canonical workspace, readable persisted history and loaded runtime state on one unambiguous owner. It captures effective model, approval, sandbox and permission-profile settings through that owner's `thread/resume`. Read [protocol and recovery](references/protocol.md) for failed detection or version limitations. Do not start another server to load an occupied thread.
3. Write a small UTF-8 file specifying the **already authorized next task**, the current checkpoint path and criteria for interpreting partial results. The helper supplies completion, log, exit-code and artifact paths. A failed batch does not authorize rerunning completed work.
4. Launch. Pass command arguments after `--`; use an explicit `bash -lc` only when the job needs shell syntax. Choose durable output outside disposable worktrees when necessary. Do not include secrets in arguments or capture the whole environment.

```bash
python3 /absolute/skill/scripts/bgjob.py doctor --cwd /absolute/workspace
python3 /absolute/skill/scripts/bgjob.py launch \
  --cwd /absolute/workspace \
  --workflow evaluation \
  --job-dir /absolute/workspace/.background-jobs/run-001 \
  --next-file /absolute/next-task.txt \
  --artifact /absolute/results \
  -- python3 /absolute/workspace/scripts/evaluate.py
```

The helper creates a visible `bgjob-<id>` tmux session. Its worker pane closes when computation and notification monitoring end; results and logs remain. `--keep-tmux` retains the exited pane for debugging, including during recovery. Only that worker pane's `remain-on-exit` is changed. Before computation, the worker subscribes to the original app-server; `lease.json` records it. Give the user the exact attach command, logs and job directory from launch. tmux does not survive reboot or protect against forced process termination.

## Consume results and retire notifications

Treat computation, consumption and delivery as separate states. Whenever the active chat handles a job's result, run `ack` for that exact job **before ending the turn or moving to a replacement**. Reading files or writing your own consumed marker does not retire a notification. On an explicit notification-stop request, use `cancel-notification`; it also works while computation runs.

```bash
python3 /absolute/skill/scripts/bgjob.py ack \
  --job-dir /absolute/job-directory --reason 'Results handled in the current chat'
python3 /absolute/skill/scripts/bgjob.py cancel-notification \
  --job-dir /absolute/job-directory --reason 'User stopped this notification'
python3 /absolute/skill/scripts/bgjob.py status --job-dir /absolute/job-directory
```

`ack` requires the job's durable completion record; it records handling, not business success. Both controls persist in `notification.json` and survive `recover`. They stop future sends and notification monitoring without killing computation, the ongoing Codex turn, or other sessions. While computation is running, the worker remains alive to save its result; after it finishes, it exits. Check `notification.state`, `notification.delivery` and `worker_running`; an old `continuation.state=waiting_idle` is historical evidence, not proof that notification is still enabled.

Controls and sends serialize at a short per-job lock. If a send already won, delivery is `already_dispatched` or `dispatch_outcome_unknown`; controls cannot retract a message or prove that an unknown send failed. Preserve its marker and turn ID. For a delayed message whose result is already handled or superseded, quietly finish after checking current state; do not restart work or send another batch progress recap.

An old **running 2.1.x worker** does not understand these controls. The new CLI refuses to claim cancellation of such a worker. Follow the legacy retirement procedure in [protocol and recovery](references/protocol.md); stopped legacy jobs can be acknowledged directly. Installing a new helper alone does not upgrade an already running process.

## Dispatch and verify

`completion.json` is atomically written on success or failure **before** continuation. `continuation.json` tracks dispatch/turn observations; `notification.json` tracks explicit retirement independently. The worker keeps its original-server connection open until monitoring ends. If the thread becomes `notLoaded`, it reloads and subscribes through **that same server's** `thread/resume`, verifies saved settings, then waits for idle. A settings mismatch or active-writer conflict stops dispatch and preserves results. It never automatically falls back to external `exec resume`.

The monitor checks retirement while waiting, before persisting a send attempt, and immediately before sending. Workflow ownership and notification controls share the send boundary's locks. Per-job and per-thread locks serialize helper dispatches. They cannot lock interactive user input or guarantee atomic idle-only admission by the server.

A durable attempt marker is written **before** send. Request IDs and `clientUserMessageId` are correlation aids, not assumed idempotency keys. A lost reply enters `dispatch_outcome_unknown`; recovery searches for that exact marker instead of resending. This prevents blind retries, without promising distributed exactly-once execution.

Initiation requires the exact returned turn ID and proof that the completion prompt belongs to it. Completion requires that same turn's final status from a matching `turn/completed` event or a read-only query. Idle state, the latest completed turn, a queue response or an exit code is insufficient. Report failed/interrupted turns and approval waits separately. A completed turn does not prove the business objective succeeded. Explicit retirement stops observation too; it leaves prior turn evidence intact and does not claim turn completion. Never wait for your own completion.

## Inspect and recover

```bash
python3 /absolute/skill/scripts/bgjob.py status --job-dir /absolute/job-directory
python3 /absolute/skill/scripts/bgjob.py recover --job-dir /absolute/job-directory
```

Before `recover`, check current workflow and consumption state. A stale waiting/pending record is not an instruction to resurrect a notification. `recover` refuses to revive acknowledged, cancelled or superseded notifications. It restarts only monitoring, never computation or a known/ambiguous dispatch. If a job vanished without durable completion, inspect artifacts and decide separately whether rerunning any work is authorized. Diagnose explicit RPC rejection before arranging a new attempt.

Connection recovery has bounded waits. Recover after endpoint availability returns. Normal approvals and client-owned tools can require a connected UI; leave requests pending without bypassing them. Native server tools can run while the UI is absent. Writer conflicts are continuation failures, not computation failures. Do not remove locks, edit Codex databases or restart the user's server to force access.

Automatic continuation requires the original app-server to keep running on the execution host, with model-provider access. SSH or frontend disconnection is supported while that server remains alive. Quitting the App, app-server exit/restart and host reboot have no continuation guarantee. Computation can still save results while its host and worker survive; bounded reconnection or manual `recover` may restore monitoring. App-start scanning, server restart and service installation are outside scope.

Old or `--no-daemon` CLI sessions without an owned endpoint cannot guarantee automatic continuation; report failed preflight or `waiting_endpoint`. Legacy manifests without settings snapshots can only observe existing attempts while loaded, not reload or initiate another attempt.

Read [protocol and recovery](references/protocol.md) for locking, race and migration details. Report computation, notification and continuation outcomes separately.
