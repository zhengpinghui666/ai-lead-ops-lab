# GitHub 源码备份

2026-09-13 接续：应用每个任务只支持一个活动心跳，因此每日23:00源码备份已合入继任任务的“ClubOps 健康巡检与每日备份”（clubops）。该心跳每小时整点读取简短健康状态，只有北京时间23点执行原备份命令；旧任务上的clubops-github已暂停，避免重复执行。仓库、分支、文件白名单与备份时间保持。正常状态静默。

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

## 首次验收 · 2026-09-11

私有仓库已创建，219 个源码文件和 SOURCE-MANIFEST.json 已推送并逐文件核对远端哈希。再次运行返回 unchanged，未创建空提交。每日任务 clubops-github 已启用；首次自然到点执行仍待观察。最终版本提交保存在本机 last-run.json，不在文档内维护容易过期的当前提交号。

## GitHub API 备用通道

2026-09-11 首次备份成功后，增量推送遇到 github.com:443 连接失败，而 api.github.com 仍正常。脚本读取远端分支时使用官方 API，默认先 Git push，失败后把同一批 Git blob、tree、commit 对象通过 GitHub Git Database API 上传，所有返回 SHA 必须与本地一致，最后只允许 fast-forward 更新分支。原仓库与历史保持一致，不另开备份仓库，不强制推送。

可用 `--transport api` 验证已初始化备份仓库的备用通道；全新空仓库仍需首次 Git push。失败不会把分支指向部分上传结果；并发远端修改会停止更新。回执中的 transport 区分 git 与 github_api。

来源：[GitHub Git commits](https://docs.github.com/en/rest/git/commits)、[trees](https://docs.github.com/en/rest/git/trees)、[references](https://docs.github.com/en/rest/git/refs)。
