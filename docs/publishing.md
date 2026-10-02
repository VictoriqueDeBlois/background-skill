# 首次发布到 GitHub

本地 `publish` 分支是用于首次公开发布的独立根提交，不包含开发时的原始测试记录或旧历史。本地 `main` 保留原有历史，`.local/` 保留原始材料；两者无需上传。

在 GitHub 创建空仓库，创建时不初始化 README、许可证或 `.gitignore`，再在本地执行以下命令。将 `OWNER/REPO` 替换为实际仓库：

```bash
git switch publish
git remote add origin git@github.com:OWNER/REPO.git
git push -u origin publish:main
```

这是把本地 `publish` 发布为 GitHub 的 `main`。后续更新继续在本地 `publish` 提交，用 `git push origin publish:main` 推送。首次发布无需 `--force`，也无需 `--all` 或 `--mirror`。

## Release 附件

```bash
python3 scripts/package_skill.py
```

将 `dist/background-job-continuation-2.1.0.zip` 作为 GitHub Release 附件。它包含可安装的 skill 和 MIT 许可证；源码仓库保留打包脚本，生成的 ZIP 不提交。

需要带测试和文档的源码 ZIP 时，可以从当前公开分支生成：

```bash
git archive --format=zip \
  --prefix=background-job-continuation-source/ \
  --output=dist/background-job-continuation-source-2.1.0.zip HEAD
```

源代码使用 MIT 许可证，CI 运行隔离测试和打包。公开报告采用摘要，不包含本机用户名、线程 ID、绝对工作路径或完整 RPC 记录。
