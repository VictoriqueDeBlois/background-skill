# 发布与后续开发

## 当前仓库的分支

本项目已发布的仓库是 [VictoriqueDeBlois/background-skill](https://github.com/VictoriqueDeBlois/background-skill)，公开代码位于 `publish` 分支。当前 Linux 工作区的本地 `main` 保留早期开发和原始测试历史，不作为公开代码的开发起点，也不要合并到 `publish` 或推送到公开仓库。

Windows 适配建议从最新的 `origin/publish` 创建独立分支，例如 `codex/windows-continuation`：

```bash
git clone --branch publish https://github.com/VictoriqueDeBlois/background-skill.git
cd background-skill
git switch -c codex/windows-continuation origin/publish
```

在该分支开发、测试并推送后，通过目标为 `publish` 的 Pull Request 合并。`publish` 是当前主线的名称，不影响正常开发，无需为了 Windows 适配切换或重命名为 `main`。克隆前先提交并推送需要迁移的本地文档修改。

新会话可使用 [Windows 开发接手 prompt](windows-handoff.md)。

## 首次发布到其他仓库

本地 `publish` 分支是用于首次公开发布的独立根提交，不包含开发时的原始测试记录或旧历史。本地 `main` 保留原有历史，`.local/` 保留原始材料；两者无需上传。

在 GitHub 创建空仓库，创建时不初始化 README、许可证或 `.gitignore`，再在本地执行以下命令。将 `OWNER/REPO` 替换为实际仓库：

```bash
git switch publish
git remote add origin git@github.com:OWNER/REPO.git
git push -u origin publish:main
```

这个示例适用于新建的其他空仓库，把本地 `publish` 发布为 GitHub 的 `main`。后续更新继续在本地 `publish` 提交，用 `git push origin publish:main` 推送。当前已发布仓库使用上面的 `publish` 开发流程。首次发布无需 `--force`，也无需 `--all` 或 `--mirror`。

## Release 附件

```bash
python3 scripts/package_skill.py
```

将 `dist/background-job-continuation-<版本>.zip` 作为对应版本的 GitHub Release 附件。它包含可安装的 skill 和 MIT 许可证；源码仓库保留打包脚本，生成的 ZIP 不提交。

仓库的 Release workflow 会在推送 `v*` 标签时核验标签与 helper 版本一致，运行隔离测试，打包并发布带 ZIP 和 `SHA256SUMS` 的 Release。发布前在 `docs/releases/v<版本>.md` 准备版本说明，例如：

```bash
git tag -a v2.1.0 -m "background-job-continuation v2.1.0"
git push origin refs/tags/v2.1.0
```

版本标签应指向已发布的源码提交。现有版本的标签和附件保持不变，更新时使用新版本号。

需要带测试和文档的源码 ZIP 时，可以从当前公开分支生成：

```bash
git archive --format=zip \
  --prefix=background-job-continuation-source/ \
  --output=dist/background-job-continuation-source.zip HEAD
```

源代码使用 MIT 许可证，CI 运行隔离测试和打包。公开报告采用摘要，不包含本机用户名、线程 ID、绝对工作路径或完整 RPC 记录。
