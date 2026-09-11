# 登录会话与 iPhone 短信恢复

2026-09-11 补充：恢复子任务在同一创建事务内绑定原监控／计划，意图版本防止关闭、编辑或重启后过期回调继续派发；登录占用期间调度等待，原失败历史保留。模拟时钟全天检查不是实机 24 小时。真实两次管理器恢复为 identity_failed，独立会话准备后续为 identity_verified；真实短信填写与自动续跑仍未验收。阶段诊断只记录固定枚举、计数及哈希，不记录原始错误、短信或会话凭证。最新优先转向私信前采集时效，自动恢复关闭。

2026-09-11 锁屏自动转发实测通过一轮：任务 `701afda582244c08a0812aca6ec826f2` 在电脑端为 `phone_tested`，测试码已取回并核对正确；用户随后确认接收 iPhone 全程锁屏、由短信自动触发，未手动运行 Scriptable。手机锁屏与触发方式依据用户确认，电脑取回依据后台记录；本轮网络类型未记录。使用普通测试短信，未请求真实抖音登录短信。真实验证码同页填写、独立身份核对和原任务恢复仍待自然需要登录时验收，自动恢复开关保持关闭。证据：`artifacts/iphone-forwarder-20260910/lockscreen-test-701afda582244c08a0812aca6ec826f2.json`。

2026-09-11。入口 `/login`，手机配置说明 `/login-guide`。保存配置和打开页面不会启动登录、采集或私信。

## 当前进度

用户已确认关闭 VPN 后，手机蜂窝和 Wi-Fi 均能打开 Pages 检测页。已正常备份、关闭旧实例并完成正式配对迁移，当前入口为 `https://clubops-login-4ff187.pages.dev`，电脑以直连方式运行，PID 209648；同一 Worker 绑定、共享任务和迁移后的鉴权健康检查均通过。原账号和配对密钥保留，迁移时旧验收清空、自动恢复关闭。iPhone 已重新配置，任务 8a4fe1d34e604413b0fbd4180270415a 的手机合成转发和电脑取回通过；该往返的网络类型未记录。其后多开的未完成测试已超时，成功验收保留；后续锁屏短信自动化已通过一轮，真实短信登录仍待验收。见 `artifacts/iphone-forwarder-20260910/pages-migration-validation.json`。

- Cloudflare 邮箱已验证，早期 10034 阻塞已解除；沿用已授权的 Wrangler 加密凭证完成 Worker 部署与两个分角色 secrets 配置。
- 原 Worker 后端地址：https://clubops-login-relay.clubops-login-relay.workers.dev。电脑与手机当前均通过上述 Pages 入口访问。当前 100% 部署版本 `b32cb77f-acc8-441d-b106-acc1aa2cddbc`，资源为 Workers＋SQLite Durable Object。
- 电脑真实 HTTPS 鉴权、合成验证码提交和一次取回已通过。云端异常与真实 180 秒到期专项以 `artifacts/iphone-forwarder-20260910/cloud-acceptance-final.json` 为准；早期失败证据保留。
- 正式服务已正常更新。本机直连失败，显式接入已有本机代理后连接成功。此结果不证明 iPhone 蜂窝网络可达。
- 最近全量为 327 项 Python／18 个 Node 入口；之后网络与部署增量通过 37 项专项（客户端 8、部署 7、管理器 20、HTTP 2）。手机核心／模拟宿主 13 项包含在 Node 入口内，Worker 本地 10 项单列。
- iPhone 已安装、运行脚本并完成鉴权配对。首次蜂窝合成转发失败、电脑任务超时；用户确认 Safari 也不能直连当前 `workers.dev` 地址，开启 VPN 后才能访问。同账号 Pages 替代入口已部署，电脑无项目代理直连和 12 项云端协议检查通过，手机蜂窝／Wi-Fi 直连已确认，正式配对已迁移，iPhone 重新配置及一次手动合成往返已通过。锁屏短信触发已通过一轮，同页真实填写及恢复原任务尚待验收。自动恢复关闭；不为本次测试请求真实抖音短信。

登录等待计时已修复：后端用已验证 HTTPS 响应的 `Date` 与云端到期时间计算剩余期限，扣除秒级精度并从请求前的单调时钟起算；浏览器助手也用单调时钟等待。取消后返回的准备／验证码结果不再触发下一步，迟到验证码不填写。53 项 Python 登录专项与浏览器助手入口通过；一次真实云端合成任务核对为旧算法剩余 168.736 秒、新算法剩余 179.189 秒，任务已清理，未请求抖音短信。计时修复当时服务更新至 PID 222520；后续已完成 Pages 迁移，当前服务与待办见最新进度。证据见 `artifacts/iphone-forwarder-20260910/timing-progress.json`、`cloud-timing-check.json` 及 `timing-service-restart/result.json`；手机两种网络的公开入口直连及一次手动合成往返已通过；锁屏短信自动触发已通过一轮，真实登录恢复仍待验收。

中转失败诊断已加载正式服务：只保存白名单内的原因、路由、阶段、错误码及 HTTP 状态，准备／取回失败与清理失败分别记录；不记录原始异常、响应正文、验证码或凭证。54 项 Python 登录专项通过，日志为 `artifacts/iphone-forwarder-20260910/relay-diagnostic-tests.log`。此前任务 `104bd75651d348c597246a6cee4c012b` 只有 `relay_unavailable`，无法追溯确切原因；单独的 urllib 下载请求曾获 403，不足以证明是该任务的失败原因。本次增加诊断，没有增加自动重试，也不能据此认定网络故障已修复。

## 切换到同一服务的 Pages 入口

`scripts/migrate-login-relay.py` 是独立维护工具，需要 Python 3.11+ 和当前锁定的 Wrangler。默认只预览，不改配置、不发短信、不停止服务。当前已通过 39 项登录相关专项检查，其中迁移新增 9 项；不是新的全量测试，也不是手机验收。

在项目根目录预览本次迁移：

```powershell
python scripts/migrate-login-relay.py --account-id 4ff187fe34a0dc740f60e9928e46ef4c --project clubops-login-4ff187 --from-origin https://clubops-login-relay.clubops-login-relay.workers.dev
```

正式执行顺序：手机关闭 VPN，分别确认蜂窝与 Wi-Fi 能访问新入口；完成数据库备份并通过服务自身正常关闭入口停止当前实例；给上述命令追加 `--apply`，成功后用 `start.ps1` 启动服务。现有服务的数据锁未释放时，工具拒绝修改。它不添加远程管理接口，不自动停止服务，也不自动请求抖音短信。

执行时通过 Wrangler 读取指定账号内 Pages 的生产配置，要求 `RELAY` 绑定原 Worker；再以独立合成账号验证“旧入口创建 → 新入口读取同一任务 → 清理”。该账号与手机配置不同，真实短信不能匹配此维护任务。检查失败时保留原配对和配置；全部通过后保留账号及两种令牌、将中转网络设为直连、关闭自动恢复、清空旧验收，最后原子替换加密配对地址。提交阶段如果被中断，已清除的验收不会恢复为成功；原地址可能仍保留，可重新预览后续接。重复执行已完成的同一次迁移不会重置新的手机验收。

启动后在 `/login` 重新“检查中转”和“显示手机配置”；iPhone 运行原脚本菜单的“重新配置”，粘贴新的完整配置。无需重装 App 或修改脚本，手机原尝试记录保留。随后重新启动一次合成手机测试，完成往返后才能启用自动恢复。普通绑定接口仍禁止跨地址复用令牌。

上述迁移、重新配置和手动连接验收已完成；无需重复执行。当前证据见“当前进度”及 `artifacts/iphone-forwarder-20260910/pages-migration-validation.json`。

## 工作流与数据

默认账号为 `1267597446`。设置位于 `data/login-recovery.json`，自动恢复默认关闭，需完成中转检查及手机连接测试才能启用。

任务按顺序进行：复用会话并核对身份 → 必要时打开项目专用登录页 → 先创建中转任务再触发登录 → 在同页等待验证码 → 填写一次 → 确认页面身份 → 关闭浏览器 → HTTP 复核 → 加密保存会话 → 按原参数恢复关联任务。自动恢复只由已结束的 HTTP `needs_login`、`session_expired`、`identity_failed` 任务触发，不因搜索 `needs_verification` 请求短信。

管理器和采集共享启动互斥；恢复保留目标、通道、预算、过滤词、绝对时间截止值和视频断点。相同恢复请求幂等；同一任务链不重复自动登录，恢复之间至少冷却 300 秒。服务重启将未结束任务标为中断，不请求新验证码。

本机配对保存在 `data/private/login-recovery/pairing.dpapi`，仅绑定一个 HTTPS 地址。手机与电脑令牌分离。`jobs.json` 保存状态和任务编号，`checks.json` 保存连接验收时间，均不保存真实验证码。真实验证码仅在进程内存和受控 stdin 中传递，不放入启动参数、日志或临时明文文件。

中转任务有效期 180 秒；手机提交需匹配任务、账号流程和收到时间，验证码文本为 4–8 位数字。一次接收、一次取回；取消或过期清理在线记录。Worker 存储的是验证码密文；Cloudflare 存储自身的备份／恢复保留不等同于在线记录的有效期，不宣称底层物理数据被即时擦除。

## 本机接口

所有写接口沿用 `Host`、`Origin` 和 `X-ClubOps-Token` 校验，仅支持正式工作区。

| 接口 | 方法及内容 | 行为 |
| --- | --- | --- |
| `/api/login-recovery` | GET | 公开状态、CSRF、可恢复任务；不返回配对密钥 |
| `/api/login-recovery-save` | POST `{account, auto_recover}` | 仅保存配置 |
| `/api/login-recovery-start` | POST `{kind: "login" 或 "phone_test", task_id?: 整数}` | 有界任务；手机测试不接受 task_id |
| `/api/login-recovery-cancel` | POST `{id}` | 取消指定在途任务 |
| `/api/login-recovery-complete` | POST `{id}` | 通知同页助手人工已完成，仍需身份复核 |
| `/api/login-relay-provision` | POST `{}` | 生成加密配对，响应不含令牌 |
| `/api/login-relay-bind` | POST `{origin}` | 绑定已部署的独立 HTTPS 主机 |
| `/api/login-relay-check` | POST `{}` | 后端鉴权健康检查 |
| `/api/login-relay-phone` | POST `{}` | 明确的手机配置操作才返回固定地址、账号和手机专用请求头 |

手机配置显示 60 秒后、页面隐藏或离开时清空；普通轮询不取回该凭证。手机连接测试页面显示的是临时合成测试文字，不是抖音短信。

## 部署与手机接入

Worker 源码在 `integrations/login-relay/`。Node >=22；使用已锁定的 pnpm 依赖，执行 `pnpm install --frozen-lockfile`、`pnpm test`、`pnpm run check`。正式部署使用用户自己的 Cloudflare 账号、Workers 默认 HTTPS 地址和 SQLite Durable Object；不要使用临时预览账号替代固定入口，也不升级付费套餐。

首次配对后，由受控部署流程将同一套 `BACKEND_TOKEN`、`PHONE_TOKEN` 写入 Worker secrets。不得复制到普通配置、源码或日志。完成 OAuth 授权后部署，再将实际地址绑定到本机，执行鉴权健康检查与合成验证码 HTTPS 往返验证。完成记录应包含实际 URL、部署版本和资源信息。

iPhone 按 `/login-guide` 安装 Scriptable 与 `/iphone-script` 脚本，完整开发说明见 `IPHONE_FORWARDER.md`。系统快捷指令传入短信正文；脚本提取品牌与唯一验证码，GET `/v1/pending` 核对账号，再 POST `/v1/otp`，JSON 为 `{id: 文本, code: 文本, received_at: 整数毫秒}`。配置只保存在 Scriptable Keychain。成功响应为 `{status: "received"}`；空任务直接结束。先完成蜂窝网络手动连接测试，再验证锁屏短信自动化和自然需要重新登录时的实际流程。默认时间字段是脚本开始处理输入的时刻，不是运营商送达证明。

该配置使用 Apple 信息触发和 Scriptable 网络请求；本机快捷指令设置已有截图，锁屏执行已获用户确认且电脑取回通过；其他设备仍须单独确认。[通信触发](https://support.apple.com/zh-cn/guide/shortcuts/apdd711f9dff/ios)、[Scriptable 请求](https://docs.scriptable.app/request/)、[自动运行](https://support.apple.com/zh-cn/guide/shortcuts/apd602971e63/ios)。

本次云端 17 个断言通过，含实际等待 182 秒后的任务消失、拒绝过期提交和取回。首轮测试因本机时钟快约 11 秒而提前检查，现已改用单调时钟；失败及诊断证据保留。这只证明在线协议行为，不证明 Cloudflare 底层备份即时擦除。

## 电脑中转网络与部署复核

`data/login-relay-network.json` 是本机中转专用设置，不进入源码包。缺失或 `{"http_proxy":""}` 时直连；迁移后当前电脑配置为 `{"http_proxy":""}`，直连 Pages；早期原 Worker 验证曾使用电脑已有代理 `127.0.0.1:7897`。只允许本机 HTTP 代理，不接受凭证、其他主机、路径或额外字段；不会继承环境代理，不会自动切换或重试。HTTPS CONNECT 后仍验证中转主机的 TLS 证书。该文件不配置 iPhone 的网络。

已有部署只核验、不重新上传：

```powershell
python scripts/deploy-login-relay.py --account 4ff187fe34a0dc740f60e9928e46ef4c --verify-only
```

有界云端验收使用自有中转和随机合成验证码，不请求抖音短信；等待真实三分钟到期，需要服务没有登录／采集任务，并指定新的证据文件名：

```powershell
python scripts/verify-cloud-login-relay.py --verify --expiry --out artifacts/cloud-acceptance-new.json
```

仅有部署成功、健康检查或电脑合成结果，不能写入手机验收时间。失败检查保留固定角色、路由、HTTP 状态及异常类型，不输出验证码、令牌、请求内容或原始异常上下文。

## 验证与恢复

`test_login_relay.py` 覆盖 DPAPI、地址绑定和 HTTP 边界；`test_login_recovery.py` 覆盖控制流程、延迟输出、取消、失联、期限、原任务范围与循环限制；`test_login_recovery_http.py` 验证真实本机 HTTP 的凭证隔离和 CSRF；`test_login_recovery.cjs` 验证浏览器助手模拟场景；`test_login_frontend.cjs` 验证后台刷新、表单与敏感配置清空。

部署后只在自然需要登录时做真实短信联调。新会话成功不自动改变 IM 发送凭证；搜索验证及私信连接各自保留独立状态。损坏的恢复记录不能让整个工作台不可用，修复前暂停此模块操作；不要直接删除现用会话文件。
