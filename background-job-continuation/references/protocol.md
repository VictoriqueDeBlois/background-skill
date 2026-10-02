# Protocol and recovery

## Baseline and transport

Helper version: 2.1.1. Dependencies: Linux, Python 3 standard library, tmux, and `codex app-server proxy`. Validation baseline: CLI 0.159.3 / tmux 3.4. Discover capabilities rather than assuming all newer versions work; daemon and CLI versions may differ.

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

The tmux worker creates its own persistent proxy connection and subscription **before computation**, checks it during computation, and retains it until exact-turn completion. The frontend connection may close independently. `lease.json` records successful subscription, settings verification and reload count. `lease.error.json` records faults separately from computation.

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

`execution.json`, `completion.json`, `continuation.json`, RPC logs and matched-turn evidence have separate purposes. Recovery restarts only observation, not a killed computation. Raw RPC files include prompts and results; keep the job directory private.

## Acceptance

Use a temporary tmux socket and fake RPC fixture for failure paths. Check actual exits, artifacts and request counts for failed computation, active owner, exact-turn lookup, reply loss, concurrent hooks, missing events, restart, settings changes, writer conflicts and no job rerun. A real smoke test uses a temporary persisted test thread on the existing server plus a harmless job. Close that test frontend and optionally archive/unarchive only its thread to verify `notLoaded` reload; observe final completion before reconnecting the cleanup client. Delete only the test thread. Verify returned turn ID and final response; do not inject test prompts into the user's active turn or describe a protocol disconnect as a physically measured SSH disconnect.
