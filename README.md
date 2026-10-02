# background-job-continuation

让 Linux 后台任务在 tmux 中运行，并在本地 CLI 或 SSH 客户端断开后，自动续接原来的 Codex 聊天。当前版本：**2.1.0**，许可证：[MIT](LICENSE)。

监控器与任务一起留在 Linux 主机上，保持原 app-server 的线程订阅。任务完成后，它保存退出码和日志，再向原线程发送已授权的后续任务；线程被卸载时，通过同一服务器重新加载并核验配置。

## 环境要求

- Linux、Python 3.10+、tmux。
- Codex CLI 支持 `app-server proxy`，原聊天运行在可访问的共享 app-server 上。
- 使用执行主机上的同一用户、Codex home 和工作目录。

已验证环境：Codex CLI / app-server 0.159.3、tmux 3.4、Python 3.12.3。其他版本需先运行 `doctor` 核验接口和归属。

## 安装 skill

将仓库克隆到本地，在仓库根目录执行：

```bash
skill_root="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$skill_root"
test ! -e "$skill_root/background-job-continuation" && \
  cp -a background-job-continuation "$skill_root/" && \
  cp LICENSE "$skill_root/background-job-continuation/LICENSE"
```

如果已安装同名 skill，先备份旧目录；上面的命令拒绝覆盖。安装后在下一轮 Codex 对话中使用 `$background-job-continuation`，例如：

> 用 background-job-continuation 在 tmux 中运行这个任务。完成后读取结果并继续当前任务，SSH 断开期间也要自动续接。

也可使用 Codex 自带的 skill-installer，从本仓库的 `background-job-continuation` 路径安装。

## 直接使用 helper

下面的路径需替换为自己的工作目录、后续任务文件和待运行脚本：

```bash
helper="${CODEX_HOME:-$HOME/.codex}/skills/background-job-continuation/scripts/bgjob.py"
python3 "$helper" doctor --cwd /absolute/workspace
python3 "$helper" launch \
  --cwd /absolute/workspace \
  --job-dir /absolute/workspace/.background-jobs/run-001 \
  --next-file /absolute/next-task.txt \
  --artifact /absolute/result.json \
  -- python3 /absolute/workspace/job.py
```

`next-task.txt` 应写明已授权的后续任务，以及如何解释成功、失败和部分结果。命令参数放在 `--` 后；需要 shell 语法时显式使用 `bash -lc`。

默认读取可信的 `CODEX_THREAD_ID` 和同一 Codex home 下的控制 socket；也可指定精确的 `--thread` 和 `--socket`。在普通 SSH shell 中没有线程环境变量时，需要提供准确的线程 ID。

`launch` 返回 tmux attach 命令和日志路径。`lease.json` 确认持久订阅，`completion.json` 记录计算结果，`continuation.json` 记录续接状态。

```bash
python3 "$helper" status --job-dir /absolute/job-directory
python3 "$helper" recover --job-dir /absolute/job-directory
```

`recover` 只恢复监控；不会重跑计算，也不会盲目重发结果未知的续接请求。

## 工作机制与边界

1. 核验当前线程、工作目录和原服务器，保存有效模型、审批和权限配置。
2. tmux worker 在任务开始前建立持久订阅，运行期间保持连接。
3. 若线程变成 `notLoaded`，通过原服务器的 `thread/resume` 重新加载并比较配置。
4. 计算完成后先保存结果，等待线程空闲，再派发后续任务。
5. 用精确的线程 ID、turn ID 和任务标记核验续接，通过完成事件或历史查询记录最终状态。

原 app-server 必须持续运行，Linux 主机必须能访问模型服务。tmux 不保证主机重启后的恢复。旧版或 `--no-daemon` 客户端没有可核验的共享控制端点时不适用。人工审批和客户端专属工具可能等待重连；配置变化或 writer 冲突会停止派发并保留结果。

详细操作见 [SKILL.md](background-job-continuation/SKILL.md)，协议和恢复说明见 [protocol.md](background-job-continuation/references/protocol.md)。

## 测试与打包

隔离测试使用 fake RPC 和临时 tmux socket，不调用模型：

```bash
python3 -m unittest discover -s tests -v
python3 scripts/package_skill.py
```

打包输出为 `dist/background-job-continuation-2.1.0.zip`，包含 skill 文件和 MIT 许可证。ZIP 不提交到源码仓库，可用于 GitHub Release 附件。

19 项隔离测试、真实服务器的断开与卸载测试、物理 SSH 断线测试均已通过。物理测试中，任务和续接均在用户报告的断线区间内完成。公开摘要见 [test-report.json](test-report.json)；原始日志、会话标识和本机安装记录仅保留在本地。

复现测试见 [docs/testing.md](docs/testing.md)，首次发布步骤见 [docs/publishing.md](docs/publishing.md)。
