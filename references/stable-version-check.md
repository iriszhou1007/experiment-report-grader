# 批改前稳定版本核验

每次 `prepare` 都必须实时访问 `skill.json` 指定的 GitHub 仓库。核验器只接受正式发布的非预发布 GitHub Release，Release tag 必须符合 `vMAJOR.MINOR.PATCH` / `MAJOR.MINOR.PATCH`；未发布 tag、预发布版本、分支（包括 `main`）和本地缓存不参与候选。

核验器选择版本号最高的正式稳定 Release，并检查：

1. 本机 `skill_version` 与 Release tag 版本一致。
2. 若安装目录有 Git 元数据，本机 HEAD 必须与 tag 指向的 commit 完全一致。
3. 本机 Skill 文件树必须与该 Release tag 的 GitHub 文件树一致。ZIP 安装没有 Git 元数据时，逐文件比较内容；机器本地 `config/local.json`、签名文件、OCR 语言数据和生成缓存不参与比较。
4. 本批选用的评分标准必须位于 Skill 仓库内，并且其文件内容与该 tag 完全一致。批次同时记录评分标准标识、文件 SHA-256 和流程使用的语义 SHA-256。

网络/API 不可用、仓库没有稳定 Release、版本或 commit 不一致、存在未发布的本地文件/修改、评分标准不属于该稳定版本，或 GitHub 返回不完整文件树时，核验失败并阻止生成批次输出。核验器不会自动联网下载、覆盖或更新本机 Skill。先由维护者发布获批稳定 Release，再把本机 Skill 更新到该版本，重新运行核验。

可以在 `prepare` 前手动查看核验结果：

```powershell
python scripts/check_stable_version.py --rubric assets/rubrics/experiment-1-web-http.json --json
```

`batch_pipeline.py prepare` 会再次执行同一检查，因此不能通过跳过手动命令绕过门槛。成功结果写入 `grading-manifest.json` 和输出目录的 `批次信息.json`；重新准备时，稳定 Release 或 commit 变化会使旧评分决策失去可复用资格。

稳定 Release 的发布是版本批准边界。发布前先更新 `skill_version`、`pipeline_version` 和需要发布的评分标准，审阅完整变更后创建对应的稳定 Release/Release。不得用移动的分支名充当发布记录。
