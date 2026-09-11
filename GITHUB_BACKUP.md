# GitHub 源码备份

用户于 2026-09-11 确认：新建个人私有仓库 `zhengpinghui666/ai-lead-ops-lab`，每天北京时间 23:00 有源码变更时推送。

运行命令（在项目目录；Python 3.11+、Git 和 GitHub CLI 已安装）：

```powershell
python github_backup.py --repo zhengpinghui666/ai-lead-ops-lab
```

首次建库使用同一命令加 `--create-private`；定时任务无需这个选项。`--dry-run` 只检查源码，不登录、不联网。

备份读取 `source-files.json`，包含实现、测试、依赖锁文件、配置示例和项目说明。每个提交附带 `SOURCE-MANIFEST.json`，记录逐文件 SHA-256 和长度。`data/`、数据库、浏览器登录目录、业务原文、运行日志、截图、私有验证码样本、依赖安装目录和密钥不上传。新增源码时应同步更新清单；未列出的文件不会自行进入备份。

`github_backup.py` 在内存中创建源码快照，使用 `data/github-backup/repository.git` 作为独立备份 Git 仓库，推送到 `codex/backup` 分支，不修改父目录“杂物”的 Git 索引或开发文件。源码无变化时不生成空提交。每次推送前核对登录账号、仓库所有者、私有状态和本地仓库身份，检查所有尚未发布的提交；遇到远端新提交不会强制覆盖。

优先沿用 GitHub CLI 登录；未配置 CLI 时，使用 Git 标准凭据接口调用现有 Git 凭据管理器。令牌只在内存和认证子进程环境中使用，不写入脚本、仓库或回执。凭据扫描是附加检查，不能替代对新入清单文件的审核。

成功或无变化回执：`data/github-backup/last-run.json`，含仓库地址、提交、树哈希和核对时间。失败返回非零状态并保留未推送提交，下次重新检查后重试。若异常退出遗留 `backup.lock`，先确认其中 PID 已不存在，再移除这个锁文件；不要删除整个备份仓库。

定时任务附在当前 Codex 任务，每天 23:00（Asia/Shanghai）执行以上命令。无变化保持安静；失败或需要操作时报告原因，不输出凭据。电脑须开机、联网且 Codex 应用运行，才能备份本机文件。[官方定时任务说明](https://learn.chatgpt.com/docs/automations?surface=app)

恢复时将仓库克隆到新目录，按 README 安装依赖并核对源码清单。该仓库是代码备份；业务数据库和登录态须使用原本的本机备份、配置和登录流程恢复。
