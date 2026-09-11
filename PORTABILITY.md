# 安装、维护与源码交付

工作台可以在本机独立运行，采集依赖另装。纯 HTTP 评论和独立回复已通过小批实测，关键词搜索仍待人工验证；可选独立 Python 环境的安装与会话准备见 `COLLECTION_HTTP.md`。个人号 HTTP 私信已有一条服务端接受记录，本机语义模型已有小批推理记录。安装成功不代表具备其他账号的鉴权或通道权限。

## 新目录安装

使用 Python 3.10+（SQLite 3.35+）启动后端，无 pip 依赖。浏览器模块使用 Node.js 20+ 和项目锁定的 Playwright 1.62.1；本轮验证为 Windows / Python 3.12.7 / Node 24.19.0。新安装建议使用 Node 24；[Playwright 官方运行要求](https://playwright.dev/docs/intro)列出当前支持的操作系统与 Node 版本。另需已安装的 Google Chrome，项目不下载浏览器。

解压源码包后，在该目录运行：

```powershell
python manage.py doctor
pnpm install --frozen-lockfile --ignore-scripts
python manage.py doctor --require-browser
./start.ps1
```

未安装 pnpm 时先安装包管理器；仅需工作台功能时可跳过 Node 依赖步骤。安装使用 `package.json` 与 `pnpm-lock.yaml`，不会自动获取抖音账号、模型权重或收费服务。不要复制旧机器的 `node_modules` 作为跨机器安装方式。

`doctor` 分别报告 Python/SQLite、Node、Playwright、Chrome，缺少可选采集依赖时普通检查仍可成功；`--require-browser` 要求全部齐备。检查仅加载本地 Playwright 模块和检查文件，不启动浏览器、不读登录目录、不初始化数据库、不请求抖音或模型。`browser_dependencies_ready=true` 只是依赖存在，不是浏览器启动、登录或业务验收成功。

## 路径与启动

默认数据目录是源码旁的 `data`，与执行命令时所在目录无关。配置样例位于 `config/`，不在启动时自动复制或启用。

| 设置 | 用途 |
| --- | --- |
| `./start.ps1 -PythonPath <python.exe>` 或 `CLUBOPS_PYTHON` | 启动脚本使用的 Python；未设置时从 PATH 查找 |
| `./start.ps1 -Port 8766` 或 `LEADOPS_PORT` | 本机服务端口，默认 8765 |
| `./start.ps1 -DataDir <目录>` 或 `CLUBOPS_DATA_DIR` | 数据库、本机配置、采集专用登录目录的根目录 |
| `CLUBOPS_NODE` | 显式 Node 可执行文件；未设置时从 PATH 查找 |
| `CLUBOPS_PLAYWRIGHT` | 显式 Playwright 包目录；默认源码下 `node_modules/playwright` |
| `CLUBOPS_CHROME` | 可选 Chrome 可执行文件；默认 Playwright 的已安装 Chrome 通道 |

显式路径填错会报错，不会静默使用其他工具的缓存。路径可以包含空格与中文；显式路径建议使用绝对路径。启动脚本不改变调用者工作目录，退出时恢复临时修改的端口和数据目录环境变量。`./start.ps1 -Check` 只做普通依赖检查。

`node scripts/probe-uid-session.cjs REPLACE_WITH_YOUR_ACCOUNT` 可独立核对已有专用会话对应的发送方资料；占位值替换为可见抖音号。它沿用 `CLUBOPS_DATA_DIR`、`CLUBOPS_PLAYWRIGHT`、`CLUBOPS_CHROME` 和 `CLUBOPS_PYTHON`，没有设置 Python 时从 PATH 查找。浏览器关闭后才进行一次 HTTP 请求，不修改发送配置或数据库，也不需要重启工作台。具体条件与真实结果见 [UID_HTTP.md](UID_HTTP.md)。

`scripts/bootstrap-uid-session.cjs` 另外实现 Windows 用户加密的会话准备；`python uid_session.py check` 通过保存的上下文独立执行 HTTP 检查。凭证位于 `data/private/uid-http/`，不随源码包或数据库备份迁移。精简 IM Cookie 后，保存会话的独立复用已返回业务状态 0；该检查命令只验证身份与有限读取；另一次明确授权的 CO-HTTP-02 已通过建会话与单条消息提交，配置在测试后关闭。运行步骤、本地过期策略与真实结果见 [UID_SESSION.md](UID_SESSION.md)。

服务先绑定端口，再锁定数据目录，最后执行数据库初始化与恢复。占用端口或另一新版实例持有数据锁时立即失败，不终止其他进程。数据锁由操作系统释放，`.clubops.lock` 文件保留，不要手动删除。早期版本服务不识别新锁；升级现有部署仍需先由操作者正常关闭旧服务，再启动新版，不能仅凭锁文件判断旧版本已停止。

首次启动创建空业务库。旧库按现有迁移逻辑在结构变更前保存 `data/backups/` 快照。服务启动将中断任务标为待人工处理，监控保持关闭，不自动恢复平台采集、私信或模型请求。

## 数据库备份与恢复

```powershell
python manage.py backup --to "D:/ClubOps backups/20260910-01"
python manage.py restore --from "D:/ClubOps backups/20260910-01" --to "D:/ClubOps restored/20260910-01"
./start.ps1 -DataDir "D:/ClubOps restored/20260910-01"
```

按实际磁盘修改示例。`backup` 可加 `--data-dir` 指定来源；默认使用 `CLUBOPS_DATA_DIR` 或源码旁 `data`。目标目录必须尚不存在，恢复不会覆盖原库，也不会替换当前运行服务的数据目录。

备份使用 SQLite 在线备份 API，包含已提交的 WAL 事务；不会使用直接复制运行中 `.db` 的方式。支持存在的 `clubops-live.db`、`clubops-demo-valorant.db`、`clubops-demo.db`。各库分别取快照，不承诺跨库同一瞬间。每库备份与完整性检查有时间限制；没有数据库、失败或超时时不生成成功清单，残留目录不能视为有效备份。

完成后 `manifest.json` 记录文件名、字节数、SHA-256 与时间。恢复先检查清单、限定文件名、哈希与 SQLite 完整性，再复制到新目录并再次校验；失败时不会继续使用该目录。`restore-receipt.json` 表示复制完成，不表示已启动、迁移成功或业务通道接通。

此备份包含原始评论、用户标识、人工判断、联系依据及发送账本等私人业务数据，应存放在用户管理的本地受控目录；**不是可公开源码包，也不加密**。保留未知发送结果及幂等记录，恢复不重置发送编号，不自动重发。数据库内监控设置随库保存；以下内容不包含在数据库备份中：

- `browser-profile/`、`live-browser-profile/` 登录目录；换机器重新登录，不保证跨机器复制可用。
- `uid-http.json`、`private/` 认证提供器、`messaging-http-context.json`。
- `semantic.json`、环境变量、模型权重和 Node/Python/Chrome 运行库。
- `archive/` 的旧系统文件，以及不在上述三个文件名中的历史数据库。

这些本机配置需按需重新配置；恢复后默认为规则模式、HTTP 私信配置缺失。若需单独保存本机登录资料，应停用相应会话后自行存入受控位置，不能放进源码包。

## 测试与源码包

```powershell
python manage.py test
python scripts/verify-install.py --with-browser
python manage.py package --to "dist/clubops-source.zip"
```

统一测试入口用当前 Python 和已配置 Node，按顺序运行全部 Python 测试及 `package.json` 列出的 Node 测试；失败即返回非零状态，不依赖 npm 命令。测试使用临时数据库、合成响应与本地服务，不是实时平台验收。前端测试自动使用启动维护工具的 Python。

`verify-install.py` 在临时数据目录和操作系统分配的空闲端口启动一份服务，读取空库与前端入口、验证第二份实例被数据锁拒绝，然后只终止自己创建的进程并清理临时目录。`--with-browser` 另外启动全新 Chrome 空白页并关闭，不使用已有登录配置；不加该选项时只检查工作台。它不验证抖音登录、采集、模型或私信，也不触碰正式 8765 服务。

源码包只包含 `source-files.json` 中逐项列出的文件，并生成 `SOURCE-MANIFEST.json` 的文件哈希；新增文件不会自动进入分发包。路径越界、敏感目录、数据库和符号链接会拒绝打包。包中没有数据库、登录目录、账号私有认证数据、实际配置、历史验证存档、依赖二进制或模型权重。包中包含不带凭证的内置会话提供器源码、配置样例与合成评测集。源码包内文档引用的 `artifacts/`、`research/` 历史证据留在原工作目录，未随包分发；这些引用不是包内可复跑的真实平台证据。

`static/vendor/lucide.min.js` 的 ISC 与派生 Feather 图标的 MIT 声明保存在 `static/vendor/lucide.LICENSE.txt`，来源为 [Lucide 1.8.0 的许可证](https://github.com/lucide-icons/lucide/blob/1.8.0/LICENSE)。Playwright 从包管理器安装时附带其许可证。打包命令仅生成本地源码归档，不发布仓库，也不替项目声明新的开源许可。
