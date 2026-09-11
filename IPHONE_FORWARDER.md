# iPhone 短信转发：快捷指令＋Scriptable

2026-09-11 锁屏自动转发实测通过一轮：任务 `701afda582244c08a0812aca6ec826f2` 在电脑端为 `phone_tested`，测试码已取回并核对正确；用户随后确认接收 iPhone 全程锁屏、由短信自动触发，未手动运行 Scriptable。手机锁屏与触发方式依据用户确认，电脑取回依据后台记录；本轮网络类型未记录。使用普通测试短信，未请求真实抖音登录短信。真实验证码同页填写、独立身份核对和原任务恢复仍待自然需要登录时验收，自动恢复开关保持关闭。证据：`artifacts/iphone-forwarder-20260910/lockscreen-test-701afda582244c08a0812aca6ec826f2.json`。

2026-09-10，按用户选择开发。实际入口为工作台 `/login`、步骤说明 `/login-guide`，下载脚本 `/iphone-script`。2026-09-11 更新：手机已重新配置并通过一次手动合成往返；后续锁屏短信自动转发也已通过一轮，真实登录恢复仍待验收。

## 安装与运行

1. iPhone 安装 Scriptable。在应用中新建名为“ClubOps 登录转发”的脚本，粘贴 `static/clubops-iphone.js` 的全部内容。这是 Scriptable JavaScript，不是苹果 `.shortcut` 安装文件。
2. Cloudflare 实际部署并在电脑绑定地址后，电脑点击“显示手机配置”，将完整 Scriptable 配置 JSON 粘贴到手机脚本的首次配置框。脚本检查手机鉴权和当前账号后才保存。配置保存在 Scriptable Keychain，代码没有嵌入真实凭证。
3. 日常使用支持 Wi-Fi 或蜂窝网络，手机和电脑无需在同一网络。先用手机 Safari 和脚本菜单的“检查中转连接”确认当前网络能访问中转，再在电脑启动“测试手机转发”，手机运行“手动连接测试”并输入本次合成文字。手机的“已转发”和电脑的“手机连接测试通过”分别核对。为确认外出可用，额外关闭 Wi-Fi、VPN 测试蜂窝直连；关闭 Wi-Fi 不是日常使用条件。
4. 创建同名系统快捷指令：从“快捷指令输入”获取文本 → Scriptable 的 Run Script 动作，选择该脚本并把文本传入 Parameter；关闭“在 App 中运行”。
5. 创建“信息”个人自动化，按用户实际抖音短信设置条件并立即运行，将收到的正文作为输入传给上述快捷指令。先完成首次权限确认，再验证锁屏运行。脚本不能自行读取 iPhone 全部短信。

详细步骤、异常含义见实际页面。当前脚本下载地址为 https://clubops-login-4ff187.pages.dev/iphone-script ，提供无凭证下载；迁移后下载内容与本机源码逐字节相同。原 workers.dev 地址在用户所测蜂窝网络下需要 VPN，现已切换到用户确认两种网络都可直连的 Pages。原脚本不含固定入口，不必重装或重新下载，仅需在菜单中重新配置。

## 行为与边界

- 只接受同时包含“抖音”“验证码”、且恰有一个 4–8 位数字片段的文本。首位 0 保留。非目标、多个候选、错误类型、超过 2000 字的输入不发网络请求。
- 先用手机专用凭证 GET `/v1/pending`，校验严格任务编号、账号、时间字段和最长 180 秒期限。没有等待任务直接结束；账号不符不提交。
- 仅 POST `{id, code, received_at}` 到 `/v1/otp`。完整短信留在手机进程内存，不进入请求、日志、文件或 Keychain。普通输出仅含固定状态和说明。
- 在提交前把任务编号与期限保存至 Keychain，最多保留 8 个有效任务。已尝试的同一任务不自动再次提交，即使上次网络结果未知。手机并发实例的最终一次接收由 Worker 的存储事务保证。
- 接收时间字段为脚本开始处理自动化输入的时间，不能证明运营商送达时刻；可接受调用方显式提供整数毫秒时间。立即运行自动化、不重放旧短信，保持设备时间自动设置。超时后在电脑原登录页处理。
- 请求仅使用配置中的固定 HTTPS 主机；禁用重定向与不安全证书，单次空闲超时 8 秒，响应最多接受 4096 字符。不关闭 TLS 校验，不自动重试提交。
- 手机显示“已转发”只证明中转接收；电脑仍须同页填写、确认页面登录、关闭浏览器、独立 HTTP 身份核对、保存会话并恢复原任务。搜索验证和 IM 私信凭证分别处理。

## 文件与构建

- `integrations/iphone/forwarder-core.cjs`：文本提取、任务与账号校验、有界转发和状态。
- `integrations/iphone/scriptable-runtime.js`：实际 Scriptable Request／Keychain／Alert／Shortcuts 接口。
- `scripts/build-iphone-client.py`：确定性生成 `static/clubops-iphone.js` 和 Worker 的 `integrations/login-relay/iphone-client.mjs`。修改源文件后重新生成。
- `test_iphone_forwarder.cjs`：提取与协议异常、重复／未知提交、生成脚本在模拟 Scriptable 宿主中的执行。它不是 iPhone 实机测试。
- `integrations/login-relay/relay.runtime.test.mjs`：在本地真实 workerd SQLite 上运行客户端→Worker→电脑取回的合成测试，同时验证公开下载不包含测试凭证。

同一服务的入口迁移工具已准备：`scripts/migrate-login-relay.py` 默认只预览；执行时要求正常关闭服务，读取当前账号 Pages 生产绑定并核对共享任务，保留账号和密钥，清除旧验收并关闭自动恢复。39 项登录专项通过，其中新增迁移 9 项。正式迁移已执行；两种网络的公开页面直连由用户确认。iPhone 已重新配置，任务 8a4fe1d34e604413b0fbd4180270415a 的手机合成转发和电脑取回通过；该往返的网络类型未记录。其后多开的未完成测试已超时，成功验收保留；后续锁屏短信自动化已通过一轮，真实短信登录仍待验收。详见 `LOGIN_RECOVERY.md` 与 `artifacts/iphone-forwarder-20260910/migration-preparation.json`。

登录等待计时已修复：后端用已验证 HTTPS 响应的 `Date` 与云端到期时间计算剩余期限，扣除秒级精度并从请求前的单调时钟起算；浏览器助手也用单调时钟等待。取消后返回的准备／验证码结果不再触发下一步，迟到验证码不填写。53 项 Python 登录专项与浏览器助手入口通过；一次真实云端合成任务核对为旧算法剩余 168.736 秒、新算法剩余 179.189 秒，任务已清理，未请求抖音短信。计时修复当时服务更新至 PID 222520；后续已完成 Pages 迁移，当前服务与待办见最新进度。证据见 `artifacts/iphone-forwarder-20260910/timing-progress.json`、`cloud-timing-check.json` 及 `timing-service-restart/result.json`；手机两种网络的公开入口直连及一次手动合成往返已通过；锁屏短信自动触发已通过一轮，真实登录恢复仍待验收。

## 当前验收

用户已确认关闭 VPN 后，手机蜂窝和 Wi-Fi 均能打开 Pages 检测页。已正常备份、关闭旧实例并完成正式配对迁移，当前入口为 `https://clubops-login-4ff187.pages.dev`，电脑以直连方式运行，PID 209648；同一 Worker 绑定、共享任务和迁移后的鉴权健康检查均通过。原账号和配对密钥保留，迁移时旧验收清空、自动恢复关闭。iPhone 已重新配置，任务 8a4fe1d34e604413b0fbd4180270415a 的手机合成转发和电脑取回通过；该往返的网络类型未记录。其后多开的未完成测试已超时，成功验收保留；后续锁屏短信自动化已通过一轮，真实短信登录仍待验收。见 `artifacts/iphone-forwarder-20260910/pages-migration-validation.json`。

脚本核心与模拟 Scriptable 宿主的 13 项检查已通过；完整 327 项 Python、18 个 Node 入口通过，Worker 的 10 项本地检查通过，部署预检查通过。实际正式服务已正常重启加载，下载文件 HTTP 200 且 SHA-256 与源码相同。证据保存在 `artifacts/iphone-forwarder-20260910/`。

Wrangler 沿用用户授权完成实际部署。注册邮箱已验证，早期 10034 失败记录保留。当前版本 `b32cb77f-acc8-441d-b106-acc1aa2cddbc`，两个分角色 secrets 已设置，电脑 HTTPS 合成收发和一次取回成功；网络／部署增量 37 项专项通过。云端异常及三分钟到期结果以 `cloud-acceptance-final.json` 为准。

iPhone 已安装 App、运行脚本并在显示 Wi-Fi 图标的网络下完成鉴权配对，实机截图为 `04-iphone-pairing-confirmed.jpg`。首次手机合成测试 `5c3627fd0c5340eb888f02ed3aa8c26b` 在蜂窝网络下提示“中转暂时不可用”，电脑最终超时；原失败记录保留。用户随后确认 Safari 也无法直连，开启 VPN 后可访问。这是原 Worker 入口的失败历史；后续 Pages 入口已有一次手机合成往返成功。后续锁屏短信触发已通过一轮，真实短信自动登录仍未验收。

同账号 Pages 替代入口已部署至 `https://clubops-login-4ff187.pages.dev`，见 `integrations/login-relay/pages-entry/README.md`。本地 Service binding 验证与编译通过，电脑无项目代理直连成功，12 项真实云端协议检查通过。手机关闭 VPN 的蜂窝与 Wi-Fi 直连均由用户确认，正式配对已迁移到 Pages，电脑改为直连；手机已重新配置并通过一次手动合成往返，锁屏短信自动转发也已通过一轮，后续验证真实登录恢复。自动恢复保持关闭，没有为本次测试请求真实抖音短信。

官方接口依据：[Scriptable 参数](https://docs.scriptable.app/args/)、[HTTP 请求](https://docs.scriptable.app/request/)、[Keychain](https://docs.scriptable.app/keychain/)、[交互框](https://docs.scriptable.app/alert/)、[快捷指令输出](https://docs.scriptable.app/script/)、[Apple 信息触发](https://support.apple.com/zh-cn/guide/shortcuts/apdd711f9dff/ios)、[自动运行](https://support.apple.com/zh-cn/guide/shortcuts/apd602971e63/ios)。动作名称、首次许可和后台执行以用户设备实测为准。
