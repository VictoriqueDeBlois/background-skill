# 测试与结果解释

## 隔离测试

在 Linux 上安装 Python 3.10+ 和 tmux，然后从仓库根目录运行：

```bash
python3 -m unittest discover -s tests -v
```

19 项测试使用临时目录、专用 tmux socket 和 fake app-server proxy，不访问真实模型或用户聊天。覆盖失败计算、部分结果、审批等待、线程卸载重载、配置变化、writer 冲突、丢失回复、精确 turn 匹配、并发恢复和不重复运行。

GitHub Actions 只运行这些隔离测试和打包，不执行真实模型测试。

## 真实服务器测试

需要已登录的 Codex 和运行中的共享 app-server。下面的测试创建临时持久线程，运行无害任务，主动断开测试前端并卸载线程；核验续接后只删除该临时线程。输出目录必须尚不存在。

```bash
python3 tests/live_smoke.py \
  --script background-job-continuation/scripts/bgjob.py \
  --output test-runs/native-smoke-001 \
  --disconnect-client --force-unload
```

验证 workspace 权限和自动审批审查配置时追加 `--workspace-profile`。它将临时线程的 runtime workspace roots 指向测试输出目录。

这是协议层前端断开测试，不等同于实际断开 SSH。测试结果写入 `smoke-result.json`；检查 `passed`、`test_thread_deleted`、退出码、精确续接 turn、最终响应和 `lease.reload_count`。

## 物理 SSH 断线测试

在当前 Codex 聊天中授权执行一次无害的 180 秒任务，让已安装 skill 将下面的程序放入 tmux，并准备结果核验作为后续任务：

```bash
python3 tests/ssh_disconnect_job.py --output /absolute/test-directory --seconds 180
```

输出目录应预先建立。后续任务需要读取 `completion.json`、stdout/stderr、`result.json` 和 `heartbeats.jsonl`，核验退出码 0、`BGJOB_SSH_TIMED_JOB_OK` 标记、约 180 秒心跳与 `settings_verified=true`，并记录核验实际执行时间。

1. 先确认 `lease.json` 中的订阅与配置核验成功，任务已开始产生心跳。
2. 记录实际断开时间，断开 SSH，保持至少 5 分钟后重连。
3. 记录重连时间，检查任务结束、续接派发、核验执行和监控器确认完成的时间是否都落在断线区间内。

续接 turn 应正常结束，让外部监控器随后记录其最终状态；不要在续接 turn 内等待自身完成。没有实际断开、重连时间时，只报告已观察到的任务和派发结果，不宣布物理断线测试通过。

完整日志可能包含提示词、会话标识和本机路径，保留在 Git 忽略的 `test-runs/` 中。公开测试摘要记录在 [test-report.json](../test-report.json)。
