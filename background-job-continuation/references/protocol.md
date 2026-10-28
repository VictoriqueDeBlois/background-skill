# Protocol and recovery

## Baseline and transport

Helper version: 2.2.0. Dependencies: Linux, Python 3 standard library, tmux, and `codex app-server proxy`. Original live validation baseline: CLI 0.159.3 / tmux 3.4. Notification retirement is covered by isolated fake-RPC integration tests, not a new live-server acceptance run. Discover capabilities rather than assuming all newer versions work; daemon and CLI versions may differ.

```bash
codex app-server proxy --help
codex app-server generate-json-schema --out /absolute/temporary/schemas
```

[Official OpenAI App Server documentation](https://learn.chatgpt.com/docs/app-server) covers initialization, transports and thread/turn APIs. Unix sockets use WebSocket framing with HTTP Upgrade. On the verified baseline, `codex app-server proxy --sock PATH` forwards **raw bytes**, including the HTTP Upgrade and WebSocket frames; it does not translate JSONL. The helper implements masked client frames, fragmented messages and ping/pong over that proxy. `codex app-server --stdio` starts a different server and uses JSONL; it is not a substitute for connecting to the owner.

## Detection

Candidates: explicit `--socket`, `BACKGROUND_JOB_APP_SERVER_SOCKET`, and `<CODEX_HOME>/app-server-control/app-server-control.sock`. These are execution-host paths. Custom endpoints must be supplied. TCP endpoints, remote authentication and blind filesystem scans are outside this helper's scope.

Connect with `initialize`, then `initialized`. `thread/read` verifies exact ID and canonical cwd; paginated `thread/loaded/list` verifies ownership. One server saying `notLoaded` cannot establish writer release everywhere. Missing state, mismatched cwd or ambiguous endpoints fail closed. A job binds host/user/Codex home; cross-host recovery is rejected. Saved owner endpoints are revalidated after reconnect, not replaced with arbitrary servers.

## Disconnected execution and subscription

Preflight calls `thread/resume` on the verified, loaded owner with only `threadId` and `excludeTurns`. This attaches the connection and returns effective settings. Save model/provider, reasoning effort, service tier, approval policy/reviewer, sandbox policy, active permission profile and disabled plugins when available. Store a digest of collaboration settings rather than developer instructions. No configuration overrides are sent.

The tmux worker creates its own persistent proxy connection and subscription **before computation**, checks it during computation, and retains it until exact-turn completion, explicit notification retirement or the monitoring deadline. The frontend connection may close independently. `lease.json` records successful subscription, settings verification and reload count. `lease.error.json` records faults separately from computation.

If this known target becomes unloaded, use the saved original endpoint's `thread/resume`, then verify its exact identity, workspace and effective settings again. This is an RPC to the existing server, not a new `codex exec resume` process. A different named profile, model, approval mode or sandbox causes `configuration_mismatch`; an active-writer rejection causes `owner_conflict`. Neither is retried automatically. A legacy raw sandbox can acquire a named profile after reload on this baseline; that identity change also stops dispatch. Explicit profiles remain comparable across reload in the real acceptance test.

The supported automatic-continuation path requires the original daemon to outlive the disconnected frontend; tmux preserves the worker but cannot preserve a daemon that the host terminates. Quitting the App is not covered by a continuation guarantee: the outcome depends on whether its original server remains alive. Losing network access from the Linux host to the model provider also prevents continuation.

Unexpected server exit is a recovery scenario. If the host and tmux worker remain alive, computation can continue and save its completion record. A server restart can be retried via the saved stable socket symlink within the monitor's wait limits, but settings must still match; `recover` can restart monitoring after endpoint availability returns. This does not guarantee automatic continuation after an App exit or server restart, which have not been validated. The helper does not scan jobs on App startup, install services or restart servers.

## Completion and races

Verification primarily uses `thread/turns/list` with the exact ID and full items. This experimental method requires `experimentalApi` during initialization. If unsupported, legacy histories can fall back to `thread/read(includeTurns=true)`. If neither works, keep `completion_status_unverified`, without guessing or writing SQLite.

Preflight also checks history access before computation starts. Baseline ephemeral threads reject both paginated turns and `includeTurns`; they cannot use this durable verification path. Use a persisted target thread. Temporary test threads are explicitly recorded and deleted after the smoke test; never delete the user's target thread.

`thread/read` does **not** subscribe to item/turn notifications; `thread/resume` supplies the persistent subscription. The helper caches `turn/completed` by **both thread ID and turn ID**. After confirming the exact dispatch prompt in history, a matching final event can establish completion even if persisted history still says `inProgress`. Save that event in `continuation.completed-event.json` and the combined evidence in `continuation.evidence.json`. If no event was received, read-only turn queries remain the fallback. An unrelated notification or idle runtime never proves completion.

Incoming approval and client-tool requests are left pending. The worker does not impersonate a tool-capable frontend or grant approvals. Server-native commands work without that frontend; client-dependent tools and user approvals can wait for reconnect. Report `waiting_approval` and `pending_server_methods` rather than promising every workflow finishes unattended.

Before send, verify idle and snapshot previous turn IDs. Persist an attempt marker, then send `turn/start` with only thread ID, text input and `clientUserMessageId`. Client IDs and request IDs are not assumed server-side deduplication guarantees. A user may start work between idle check and send; helper locks cannot lock interactive input. If the response belongs to a previous turn, record `followup_delivered_to_active_turn` instead of claiming a new turn. Never interrupt user work.

An accepted request whose reply is lost stays `dispatch_outcome_unknown`. Search the exact marker in **user-message** items. No match is not proof of rejection; never resend automatically. Legacy manifests without verified settings can observe a known attempt while its owner is loaded, but cannot reload an unloaded thread or initiate another attempt.

## Records and recovery

### Result consumption and notification retirement

Computation (`completion.json`), dispatch/turn observations (`continuation.json`) and notification retirement (`notification.json`) are separate records. `ack --job-dir PATH --reason TEXT` records that the caller handled this job's completed result; it requires a matching job ID in `completion.json`, but accepts failed computation too. `cancel-notification` records an explicit stop without requiring completed computation. Neither command claims business success or modifies earlier dispatch evidence. Repeated controls preserve the first retirement record and reason.

These controls acquire `notification.lock`, which the monitor holds from the final eligibility check through persisting its attempt, sending, and recording the reply or unknown outcome. They do not acquire the long-lived `worker.lock` or `dispatch.lock`. Thus cancellation can complete during computation, idle waits, disconnected-server waits or turn observation. If a send is already in flight, control waits for that RPC boundary, normally bounded by `rpc_timeout` (plus local persistence/cleanup). `notification.delivery_at_retirement` records what was known then:

| Delivery | Meaning |
| --- | --- |
| `not_attempted` | No durable attempt exists; this control prevents sending. |
| `already_dispatched` | A turn ID was returned or reconciled; control cannot retract its message or stop the turn. |
| `dispatch_outcome_unknown` | An attempt exists without a confirmed reply; it may have been accepted. Never resend. |
| `rejected` | Explicit RPC rejection is recorded. |

The monitor checks retirement before endpoint access, on each iteration, before the durable send intent and immediately before `turn/start`. Once a control returns successfully, this worker cannot initiate another send. A notification already accepted may still appear in the UI later. `status` reports current delivery separately from the retirement snapshot. Retirement stops observation too, preserving the last verified turn state without converting it to completion. An unknown marker can still be investigated with read-only server queries; `recover` will not resume a retired notification just to reconcile it.

During computation, retirement closes the subscription at the next worker check but does not send any signal to the computation. The worker saves normal completion and logs, then exits instead of notifying. During monitoring, it exits at the next check, after any current RPC/poll wait. `status.worker_running` probes the actual worker flock, rather than inferring liveness from historical JSON states, tmux presence or a recorded PID. It can remain true while the preserved computation runs or an RPC finishes. `--keep-tmux` may leave a dead pane after the worker exits. Keep controls and job files: deleting a retirement record removes its protection.

### Workflow generations

`launch --workflow ID` assigns the new job the latest notification generation for that logical workflow. Ownership is an atomic record under `<lock_root>/workflows/<digest>.json`; the digest includes host/user/Codex-home scope, exact thread ID, canonical workspace and workflow ID. The generation increases on each registered launch. The ownership lock is also held across sending. A replacement that wins before send suppresses the old send; one that follows an accepted send cannot retract it. Never restore an older owner automatically, even when a newer launch fails: inspect the failure and arrange an authorized replacement.

Old workers derive `notification.state=superseded` from this record; their computation and dispatch records are unchanged. They stop subscribing/monitoring, keep any running computation, and cannot be revived by `recover`, even after the latest owner is acknowledged. Preserve the registry and use one lock root for related runs. Missing ownership records block dispatch rather than silently reactivating old generations.

This is replacement, not batch aggregation. Use one coordinator command that waits for children and writes a current aggregate, then register that coordinator's continuation. Reuse its workflow ID only for actual replacements. Parallel children needing inclusion in the aggregate must not each claim the same workflow ID: the latest child would replace the others rather than wait for them. Distinct business workflows use distinct IDs. Jobs without a workflow retain independent delivery for compatibility; acknowledge or cancel each exact old job individually.

### Legacy workers

Installing 2.2.0 does not change a process already executing 2.1.x code. The new worker records its control capability in `worker.json` while holding `worker.lock`; controls refuse a live worker without that capability. For a stopped legacy job, `ack`/`cancel-notification` are immediately durable and the new `recover` respects them. A legacy manifest recovered under the new helper gains the new worker capability.

For a running legacy worker, first inventory the exact thread, job ID, worker and child process. Only stop its verified job-specific **monitor** once computation is durably complete, or use a monitor-only mechanism whose separation from computation has been verified. A combined worker/pane kill can kill or signal computation: do not use it for a notification-only stop while the child runs. Preserve evidence and record retirement with the new helper after the old monitor exits. No broad process kills, lock deletion, database edits or app-server restart. Already dispatched messages remain distinct from future sends.

Records use same-directory temp files, file fsync, atomic rename and directory fsync. SIGINT/SIGTERM forward to the job process group. SIGKILL, power loss or host failure can prevent completion recording. Artifact existence does not prove business success.

The worker pane has `remain-on-exit=off` by default, explicitly overriding inherited settings for that pane. It exits only after computation and monitoring finish, including terminal errors or expired waits; ordinary one-window job sessions then disappear automatically. Records survive independently of tmux. `--keep-tmux` persists a debugging choice in the job manifest and uses `remain-on-exit=on`, including during recovery. Older manifests without this field use automatic closing for newly started recovery workers. Existing retained panes from earlier runs are not removed retroactively; no other panes, sessions or global settings are changed.

| State | Meaning |
| --- | --- |
| `pending` | Job not yet complete. |
| `waiting_endpoint` | Saved owner cannot be reached or confirmed. |
| `configuration_mismatch` | Effective settings differ; no new turn dispatched. |
| `owner_conflict` | Original server reports an active writer conflict; no external fallback. |
| `waiting_idle` / `waiting_approval` | Thread active; wait or use normal approval UI. |
| `dispatching` | Attempt persisted; recovery must reconcile. |
| `dispatch_outcome_unknown` | May have been accepted; search exact marker, never blindly resend. |
| `dispatch_failed` | Explicit server rejection; diagnose before a new attempt. |
| `followup_started` | Exact prompt belongs to the new turn. |
| `followup_delivered_to_active_turn` | Prompt reached a turn that already existed. |
| `completion_status_unverified` | Known turn's completion cannot yet be verified. |
| `followup_completed` | Exact matched turn is `completed`; audit actual outcome separately. |
| `followup_finished_with_error` | Matched turn failed or was interrupted. |
| `job_execution_unverified` | No durable completion; computation will not be rerun. |

Job locks prevent duplicate wrappers. Thread locks default to `<CODEX_HOME>/background-job-continuation/locks`. `--lock-root` enables isolated tests; all real monitors for a thread must share a root. These locks serialize helpers, not interactive users. Lock files remaining after exit are normal: ownership belongs to the open descriptor. Do not delete them to force access.

`execution.json`, `completion.json`, `continuation.json`, `notification.json`, RPC logs and matched-turn evidence have separate purposes. A stale `waiting_idle`/`pending` continuation record does not override notification retirement. Current-state updates clear transient errors, expired-wait flags and pending-request summaries; exact attempt and turn evidence remain. Recovery restarts only observation, not a killed computation. Raw RPC files include prompts and results; keep the job directory private.

## Acceptance

Use a temporary tmux socket and fake RPC fixture for failure paths. Check actual exits, artifacts and request counts for failed computation, active owner, exact-turn lookup, reply loss, concurrent hooks, missing events, restart, settings changes, writer conflicts and no job rerun. A real smoke test uses a temporary persisted test thread on the existing server plus a harmless job. Close that test frontend and optionally archive/unarchive only its thread to verify `notLoaded` reload; observe final completion before reconnecting the cleanup client. Delete only the test thread. Verify returned turn ID and final response; do not inject test prompts into the user's active turn or describe a protocol disconnect as a physically measured SSH disconnect.
