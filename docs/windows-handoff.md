# Windows 开发接手 prompt

在 Windows 上克隆 `publish` 分支、创建 `codex/windows-continuation` 开发分支后，可将下面的内容作为新 Codex 会话的开场提示词。

---

请接手 background-skill 项目的 Windows 适配开发。仓库：https://github.com/VictoriqueDeBlois/background-skill。

公开主线是 `publish`，请基于最新的 `origin/publish` 使用 `codex/windows-continuation` 分支。先检查工作区和分支状态，保留已有修改；不要基于旧 Linux 工作区的本地 `main`，它保留了早期私有测试历史。

先阅读 `README.md`、`background-job-continuation/SKILL.md`、`background-job-continuation/references/protocol.md`、`docs/testing.md` 和 `test-report.json`，再检查 `background-job-continuation/scripts/bgjob.py` 及 `tests/`。

当前版本为 2.1.0，已发布 MIT 许可证的 Release。Linux 实现用 tmux 保留任务和监控器，通过 `codex app-server proxy` 持续订阅原服务器上的原聊天。线程卸载后在同一服务器调用 `thread/resume`，核对保存的模型、审批、权限等配置，计算完成后派发已授权的后续任务，并用精确 thread ID、turn ID 和任务标记验证完成。19 项隔离测试、真实服务器前端断开与线程卸载测试，以及物理 SSH 断线期间的计算和自动续接已经通过。App 完全退出或 app-server 真正退出、重启没有实测。

本次目标：Windows 上的后台计算与监控不因前端连接断开而结束；原 app-server 仍运行时，计算完成后能自动续接原聊天，无需重新连接客户端。

沿用以下支持边界：

- 自动续接依赖原 app-server 持续运行，以及执行主机能访问模型服务；审批和客户端专属工具仍可能等待用户重连。
- 用户主动退出 App 后不保证自动续接；服务器意外退出后的结果保存、重连和手动 `recover` 作为补救措施。
- 不扩展到 App 启动扫描、自动重启服务器、系统常驻服务、主机重启恢复或 macOS 适配。

先检查实际 Windows 环境、Codex 版本和共享 app-server 能力，区分 Windows 原生与 WSL2。评估后台进程生命周期、IPC、身份归属和配置核验，给出推荐方案与验证计划，再实施。若采用 WSL2，明确它支持的是 WSL 内的 Linux 线程；不能据此声称已支持 Windows 原生桌面端聊天续接。不要假定 Windows 的 IPC 与 Linux Unix socket 相同。

重点检查并替换平台相关部分：tmux、`fcntl/flock`、`os.getuid()`、`/etc/machine-id`、进程组及信号、路径和原子文件写入。按需要分离平台后端，保留共用的任务记录、续接协议与结果核验，避免复制整套逻辑；保留现有 Linux 行为和测试。

必须保持的约束：计算结果先落盘，再续接；`recover` 不重跑计算；发送结果未知时只核对原请求，不能盲目重发；不用最新聊天、标题或 `--last` 猜目标；不另起服务器接管原线程，不自动改模型或权限，不绕过审批。计算成功和续接成功分别报告。

先做隔离测试，再使用无害任务和临时测试线程验证 Windows 后端；不关闭用户 App、不重启原服务器、不删除用户聊天。区分协议层断开与实际客户端断开，按真实证据报告测试结果；若环境缺少必需能力，明确阻碍。更新平台要求、使用方法、测试和限制，保持旧 Release 与版本标签不变。完成后总结改动、测试证据及尚未支持的边界；提交、推送或发布按本会话的授权范围执行。
