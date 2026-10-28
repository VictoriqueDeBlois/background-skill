# 后台续接通知积压审查与修复

审查日期：2026-10-28（Asia/Shanghai）。现场来源：用户指定服务器的 `~/background-job-continuation-review` 中的中文总结、skill 修改包及选取的落盘证据。下载包 SHA-256 和包内 SHA256SUMS 均核验通过。没有执行包内历史 next_task、重跑原项目或向原聊天注入消息。

## 根因与严重程度

| 级别 | 已证实的问题 | 影响 |
| --- | --- | --- |
| P1 | 业务结果已被当前会话消费，但 helper 没有消费/取消接口 | 等待空闲的旧 job 在业务完成后仍发送续接 |
| P1 | 一个业务的子批次和重试分别注册唤醒，没有替代关系 | 不同 job 各发一次也会造成通知积压；锁和按 job 去重不能解决 |
| P1 | 2.1.x 的 worker 将计算与监控放在同一进程生命周期内 | 直接杀 worker、pane 或进程组可能影响仍需保留的计算 |
| P2 | 保存的 next_task 携带旧业务进度，没有检查当前 checkpoint | 旧失败和旧计划可能再次驱动已经完成的工作 |
| P2 | continuation 状态更新沿用旧 error、超时和待审批字段 | 成功/新观察仍可能带有过期故障，误导后续恢复 |

现场共登记 40 个 job，38 个有计算完成记录，34 个有发送尝试记录；匹配续接中 10 个完成、24 个被中断。选取样本延迟约 7–111 分钟。多数通知来自不同 job，并非同一 dispatch 被无限重发。JSON 中的 waiting/pending 只是历史状态，不能证明仍有对应活进程。

服务器 agent 的更新只改变 SKILL.md 指引。包内 bgjob.py 与本仓库修改前的 2.1.1 脚本 SHA-256 相同：`02d33e7be4ef618e19ebce836128ab62680807197d0a1d9eae4b9ca4774d18dc`。因此“记录 consumed checkpoint”原先没有实际停止旧 monitor 的作用。

不能仅从 interrupted turn 和多次 turn_aborted 推断每次中断都由 turn/start 抢占导致。[官方 App Server 文档](https://learn.chatgpt.com/docs/app-server)描述 turn/start 和 turn/steer，但没有为本 helper 建立原子 idle-only 提交保证；本机 CLI 0.162.1 生成的 TurnStartParams 也没有可用于该保证的条件字段。本地线程锁只能约束遵守同一锁根的 helper，不能锁住交互式输入。这一竞态边界继续保留，不宣称已修复服务器调度。

## 2.2.0 的最小实现

计算、消费和派发分别留证，避免将“结果处理过”伪写为“续接成功”：

- `completion.json`：计算是否结束、退出码、日志和产物。
- `continuation.json`：原 dispatch marker、返回的精确 turn ID 和已验证观察。
- `notification.json`：`acknowledged` 或 `cancelled`，包含原因、时间及退休时已知的派发结果。
- 工作流 registry：同一业务的当前通知 owner 与递增 generation。旧 owner 根据当前记录成为 `superseded`。

`ack` 要求本 job 的完成记录，表示调用方已处理结果，失败结果同样可以确认；它不是成功验收。`cancel-notification` 不要求计算结束，表示关闭通知。它们不发送信号、不删除日志、不改写原 turn 证据。worker 通过实际控制状态结束监控，计算还在运行时继续保存其结果。

控制命令与实际发送共享短期 `notification.lock`；工作流替代与发送另共享 workflow lock。退休先赢则不发送；发送先赢则明确返回已发出或结果未知，保留 marker，不能伪称撤回。监控在等待期间和真正发送前检查控制状态，`recover` 也检查，所以重启不会复活退休通知。已派发但尚未完成的 turn 不会被取消命令中断。

`--workflow` 只为明确的替代关系选择最新通知 owner，不自动汇总并行子任务。skill 采用一个业务协调器等待多个子任务、产出持久化汇总的默认组织方式；独立业务分配不同 ID。保留未指定 workflow 的独立 job 兼容行为，并要求主会话消费结果后执行 ack。helper 无法仅凭“某文件被读过”判断业务已经消费；调用方仍须确认处理。

新续接提示携带当前状态查询和 ack 命令，要求先读取当前 checkpoint，已退休/被替代的旧消息静默结束。当前状态更新清理过期错误、超时及审批摘要，但保留准确派发身份。

## 验证与迁移边界

隔离测试使用临时 Unix socket、独立 tmux server 和 fake RPC，观察实际请求次数、子进程、退出码、worker 锁与日志。覆盖已消费结果不补发、计算中停止通知、取消/发送竞态、丢失回复不重发、同一 workflow 多次替代、替代/发送竞态、独立业务各自通知、恢复不复活、旧 worker 拒绝虚假取消，以及原有单任务和身份/权限保护。具体执行结果见 [test-report.json](../test-report.json) 的 `notification_lifecycle_validation`。

本次审查阶段没有重新执行真实模型、物理 SSH 断开或用户输入与 turn/start 的真实服务器竞态测试。历史 2.1.x 的真实断线结果不是新接口的真实验收。审查时修复包只在本仓库生成；后续发布和安装单独核验，不计入这里的运行时验收。

已运行的旧 2.1.x 进程不会因文件升级而获得新逻辑。新 CLI 检测 worker 控制能力，对正在运行且不支持控制的旧 worker 拒绝声称已取消。对已经停止的旧 job，可直接用新 helper ack/cancel 并阻止后续 recover；若旧计算还在运行，先保留它，仅在确认监控与计算能安全分离后处置其监控。不得以停通知为由杀 app-server 或批量杀进程。详细迁移规则见 [protocol.md](../background-job-continuation/references/protocol.md#legacy-workers)。
