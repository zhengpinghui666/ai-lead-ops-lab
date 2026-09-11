# 个人号 HTTP＋数字 UID 接入记录

2026-09-11 HTTP 消息读取增量：独立 IM 认证过期后，经项目浏览器准备、关闭浏览器并通过两次 HTTP 核对，重新保存加密会话。新增 `uid_inbox.py`，提供有界会话核对和选定会话消息读取；历史 CO-HTTP-02 会话真实读取 5 条消息，3 条可解析为文字，其中 1 条入站、2 条出站，并找到原测试文字。该历史入站记录尚未证明是测试回复。5 个新采集 UID 在普通列表前三页的 30 条会话中未匹配，列表未读完；陌生人分支修正了收件箱字段解析，随后实测连接失败，不能据此认定陌生人关系。78 项 UID 专项测试通过；本轮未新增发送或业务库消息。读取当前仅为 Python 接口／命令，尚未接入工作台按钮、入站持久化和任务关联。详见 `UID_INBOX.md` 与 `artifacts/uid-read-20260911/`。

以下保留 2026-09-10 的阶段证据与接口说明；其中测试数量属于相应历史版本。

2026-09-10。代码已接入主系统的草稿、任务和结果页面。**CO-HTTP-02 已通过数字 UID 与无浏览器 HTTP 完成建会话、票据核对及唯一一次消息提交，结果为服务端接受；送达和已读尚未核验。** `uid_session.py` 实现当前账号会话封装、Windows 用户加密保存和 IM Cookie 精简。该历史发送版本的测试为 237 项 Python 与 13 个 Node 测试入口全部通过，配置仍禁用；运行方法、当前网页协议字段、真实证据与缺口见 [UID_SESSION.md](UID_SESSION.md)。

## 最新：专用会话与真实 HTTP 身份核对

`scripts/probe-uid-session.cjs` 与 `uid_bootstrap.py` 可独立于发送提供器运行。从项目专用登录目录读取目标 URL 作用域内的 Cookie 和浏览器实际 User-Agent，等待认证浏览器关闭，再通过本机管道交给 Python，只请求一次固定的登录者资料端点。输入是**发送方可见抖音号**，结果返回核对得到的数字 UID；不把输入当成接收方或发送请求。

```powershell
# 在项目目录运行；将占位值替换为当前发送账号的可见抖音号。
node scripts/probe-uid-session.cjs REPLACE_WITH_YOUR_ACCOUNT
```

需要已有的项目专用登录会话及本地 Node、Playwright、Chrome、Python；路径设置沿用 [PORTABILITY.md](PORTABILITY.md)。`CLUBOPS_PYTHON` 可指定 Python。没有专用会话、依赖不可用、浏览器打开或关闭失败时停止，不自动登录、安装或改用其他浏览器配置。该命令每次都会准备并关闭专用浏览器，**目前是接入诊断工具，不是长期复用认证的后台 HTTP 服务**。命令可以直接运行，无须重启 8765。

2026-09-10 的真实结果为 `identity_verified`：HTTP 200、15,863 字节 JSON，业务状态为 0，可见抖音号匹配，取得数字 UID。认证浏览器在这次 HTTP 请求前已关闭；只调用一次资料接口，未创建会话或发送消息。原始资料与凭证未落盘；本地证据 `artifacts/uid-bootstrap-real-20260910.json` 只保留核对身份、摘要、字节数与阶段标识，不进入源码包。

成功仍返回 `can_send=false`、`live_verified=false`，不会写入发送配置、接收范围、数据库或发送权限。纯 HTTP 请求成功不能证明 IM 认证、建会话、私信提交、送达或陌生人权限已经通过。

后续诊断确认先前 `ValueError` 来自响应体超过 256 KiB。改为请求一条会话后曾成功；早期完整 Cookie 复用又返回业务码 409。IM Cookie 精简为当前实际 `sessionid` 与 `sessionid_ss` 后，产品命令从保存密文完成独立 HTTP 核对，资料与 IM 均通过，未启动浏览器。阶段证据分别保留；409 的精确含义及具体冲突字段未单独确定。研究没有脚本消息操作；原始消息和响应体未存档，产品凭证仅以 Windows 用户密文保存。

研究目录 `artifacts/uid-auth-review-20260910/` 不属于可分发实现；读取请求样本不能作为发送模板或可续期认证提供器。详细状态和测试摘要见本地 `artifacts/uid-bootstrap-validation-summary.json`。

## 本地接口与历史身份核对增量

新增独立的 `POST /api/uid-http-probe?mode=live`，请求体只能是 `{}`，仍要求本地页面会话的 `X-ClubOps-Token`。配置完整后，“私信导流”中的“核对登录身份（不发送）”才可用。原来的“检查本地配置”依旧只读取本地文件，不导入提供器、不访问平台。缺发送方、接收范围或提供器时分别列明，不再因 UID 校验提前退出而隐藏后续缺口。

身份核对只调用提供器的 `prepare('identity', ...)`，传输层仅向登录者资料端点发起一次 GET；不创建会话、草稿、发送尝试或消息，不自动重试。它与发送任务共用进程锁，避免同时操作会话。成功返回 `identity_verified`，同时明确 `live_verified=false`、`can_send=false`；不会把这一结果缓存为发送权限。实际发送仍重新核对登录身份。

HTTP 200、HTML 验证页、业务错误响应、缺失或不匹配的 UID、浮点数或布尔 UID 都不能通过核对。原发送路径已复用这个判断，修复“业务失败响应带有匹配 UID 仍继续创建会话”的缺口。输出仅包括结果、时间、HTTP 状态、响应摘要与已核对的发送方 UID，不返回原始资料、Cookie、票据或提供器异常文本。

早期身份核对增量通过 31 项 UID 测试，全量 Python 158 项、Node 11 个入口；当时尚未配置实际凭证或请求抖音。历史日志为 `artifacts/uid-identity-python-tests.txt`、`artifacts/uid-identity-node-tests.txt`，摘要为 `artifacts/uid-identity-summary.json`。这些合成响应不证明真实通道，当前真实只读证据和测试数量以本文开头及 `UID_SESSION.md` 为准。

本地接入顺序如下；这些是 ClubOps 的接口，**不是抖音提供的“输入 UID 即发送”公开 API**。实际发送前提是有效的本机会话、明确同意的测试范围和已保存草稿。

| 本地操作 | 请求路径 | 请求体 |
| --- | --- | --- |
| 获取本地会话令牌与通道状态 | `GET /api/state?mode=live` | 无；响应中的 `csrf` 用于后续 POST 请求头 |
| 检查本地配置 | `POST /api/uid-http-check?mode=live` | `{}` |
| 核对发送方登录身份 | `POST /api/uid-http-probe?mode=live` | `{}`；不接受临时 UID、凭证或消息 |
| 登记明确同意的测试对象 | `POST /api/uid-http-target?mode=live` | `uid` 字符串、`nickname`、`contact_note`；返回 `lead_id` |
| 保存本地草稿 | `POST /api/draft?mode=live` | `lead_id`、唯一 `request_id`、`content`；返回草稿 `id` |
| 提交已保存草稿 | `POST /api/uid-http-send?mode=live` | 仅 `{ "id": 草稿整数ID }`；不要通过此入口试登录 |

该接口最初仅在隔离服务验证；其后正式 8765 已通过正常更新加载既有 UID 接口。当前新增消息读取仍为 Python 接口／命令，尚未接入工作台。

## 已实现的调用路径

1. 在“私信导流”登记明确同意的测试对象，输入数字 UID 字符串，保存联系依据。该入口不创建评论或付费需求，不发送消息。
2. 保存一条文字草稿。提交入口只接收本地草稿任务 ID，不允许请求临时替换接收人或消息。
3. 检查通道配置、测试范围、联系依据、禁止联系状态及历史尝试记录；先持久化唯一尝试，再访问网络。
4. HTTP 核对实际登录者 UID → 创建双方会话 → 核对会话参与者 → 获取该会话票据并封装当前认证 → 再次检查内容和联系依据 → 最多提交一次消息。
5. 校验服务端响应中的命令、请求序列、发送方、客户端消息 ID、业务状态和服务端消息 ID；证据不足记录“结果未知”。

`uid_protocol.py` 实现 Protobuf 业务字段；`uid_transport.py` 使用 Python 标准库 HTTPS 传输；`uid_messaging.py` 接入 SQLite 与业务检查。`uid_session.py` 复用密文认证并封装请求；这些 Python 模块不启动浏览器，首次会话准备单独使用项目浏览器。

普通抖音号、数字 UID、`sec_uid` 与 OpenID 是不同标识。UID 在 JSON、SQLite 和界面均保留为字符串；旧导入来源可能混用 OpenID，因此本通道只接受已明确记录 UID 的浏览器来源或专门登记的测试对象。

## 配置与会话提供器

配置样例在 `config/uid-http.example.json`，实际读取 `data/uid-http.json`。相对 `provider_file` 以 `data/` 为根解析。当前默认未启用；不要仅将 `enabled` 改为 `true` 就认为已经能发送。

| 字段 | 含义 |
| --- | --- |
| `enabled` | 显式开启实验通道；保存配置不会启动发送 |
| `sender_uid` | 当前发送账号真实的数字 UID 字符串，不能填可见抖音号 |
| `allowed_recipient_uids` | 已同意的测试账号 UID，当前限制 1–20 个；不是批量发送名单 |
| `provider_file` | 本地 Python 会话提供器路径；内置实现为 `uid_session.py`，已通过一次真实只读复用核对 |

提供器导出 `Provider` 类，声明 `transport = 'http'`，实现两个方法：

```python
prepare(operation, business_body: bytes, metadata: dict) -> dict
ticket(conversation: dict, sender_uid: str, recipient_uid: str) -> str
```

`prepare` 返回 `payload`（bytes）、`headers`（str→str）、`query`（str→str）。`identity` 返回空请求体；`create` 和 `send` 必须将给定业务体原样放进 Protobuf 顶层字段 8，顶层命令字段 1 分别为 609 和 100。主程序会拒绝提供器修改目标或消息的结果。

`metadata` 包含发送方，创建阶段增加接收方和命令；发送阶段增加客户端消息 ID、会话 ID 与短 ID。提供器使用当前账号认证构建外层；内置实现只接受已观察到的 `SESSION_AUTH=1` 形式，不实现其他模式的签名或刷新。`ticket` 从核对双方后的创建响应中取得票据。不能复用参考仓库作者的凭证或假定旧模板中的证书、令牌与签名仍有效。提供器不主动发消息、另开浏览器或自行重试，最终提交由传输层执行。

固定目标为登录核对 `www.douyin.com/aweme/v1/web/user/profile/self/`、会话创建 `imapi.douyin.com/v2/conversation/create` 和消息提交 `imapi.douyin.com/v1/message/send`。只读 IM 检查另用 `imapi.douyin.com/v1/stranger/get_conversation_list`，限制一条。当前账号的资料与该只读 IM 请求已通过真实 HTTP 核对，**指定授权测试的会话创建与消息提交已通过，不能推断其他账号或陌生人范围**，这些网页内部路径不能当作公开稳定 API。

现已完成会话准备、只读复用核对和一条真实 HTTP 消息的服务端接受验证；认证刷新与接收端送达仍待确认。账号密码、Cookie、令牌或证书不应粘贴到聊天、前端表单或报告。实际配置和私有模块目录已加入忽略规则；此代码没有上传凭证到第三方服务的步骤。

## 状态、幂等与数据保护

- 未配置、联系依据不满足或对象不在测试范围时，不发消息。检查配置只读本地文件，不导入提供器或访问平台。
- 同一账号、接收方和消息内容全局去重；一个进程同一时间只处理一条。此版本是手动提交已保存草稿，没有后台自动群发任务。
- 每个草稿有持久化尝试记录和唯一客户端消息 ID。超时、异常、丢失最终写库或服务重启后，不自动重发；重新建相同内容草稿也不能绕过去重。本地维护函数可凭明确的 submission_reserved=false 证据和处理依据继续消息提交前的失败准备，保留同一任务、内容、去重键、客户端 ID 和失败历史，最多三次继续；普通 HTTP 提交 API 不开放此参数。旧记录缺证据、未知结果或曾保留消息提交次数时一律禁止继续。
- `accepted` 仅表示服务端接受；不推断送达、已读、回复或导流。平台错误码 8101、7174 不当作成功。
- 记录阶段、HTTP 状态、响应摘要和可核验消息 ID；不保存原始认证数据或带凭证的异常文本。
- 首次引入 `uid_message_attempts` 表前，使用 SQLite 备份 API 把已有数据库保存到 `data/backups/`。重启只恢复本地未知状态，采集计划保持暂停。
- 已完成的 `CO-DM-01`、`CO-HTTP-02` 禁止重复，旧固定测试发送 API 已关闭。CO-HTTP-02 实际消息提交恰好一次，服务端接受；历史失败与通过证据分别保留。

## 源码审查与验证

早期参考 [Rockedw/douyin-web-api-sdk 的 MessageSender](https://github.com/Rockedw/douyin-web-api-sdk/blob/2c9d76d595717b5a186527586d3f91b6c26dc541/src/main/java/com/dy_web_api/sdk/message/handler/MessageSender.java)，快照提交为 `2c9d76d595717b5a186527586d3f91b6c26dc541`，当时未发现 LICENSE 文件，没有引入其实现或认证模板。其 `.proto` 与生成的 Java 字段不一致；后来已按当前抖音网页结构修正为未打包的重复 int64 参与者字段、回执字段 2 的 `extra_info` 与字段 6 的普通检查文本。实现与证据说明见 [UID_SESSION.md](UID_SESSION.md)，指定测试的实际创建及单条提交已验证，详见最新证据。

最初协议增量通过 125 项 Python 测试、9 个 Node 测试入口，日志为 `artifacts/uid-http-python-tests.txt`、`artifacts/uid-http-node-tests.txt`。这些使用隔离数据库、合成协议响应或本地 HTTP 服务，**不能证明抖音服务接通**。旧 OpenAPI 面板残留由真实浏览器检查发现并移除，相关前端测试随后再次通过；最新全量测试数量见本文开头。

历史上，原运行服务的强制停止被自动审批拒绝，当时转在 8766 临时数据库验证界面。之后已采用正常关闭／备份／启动更新正式 8765，既有数据保留；最新维护证据见 `SERVICE_LIFECYCLE.md`。本轮消息读取增量没有执行正式服务重启或业务库迁移。

实际浏览器验证已完成：在临时数据库登记明确标注“离线 UI 样例 · 非真实账号”的合成对象，数字 `10000000000000002` 原样保存；保存一条草稿后，消息数仍为 0，HTTP 发送按钮禁用。通过页面快照及可见窗口截图核对布局与状态；测试页和临时服务已关闭。摘要保存于 `artifacts/uid-http-ui-check.json`。原正式区操作前后仍为 7 视频、65 评论、64 线索、0 消息和 0 发送任务，监控暂停；数量快照见 `artifacts/uid-http-before.json` 与 `artifacts/uid-http-after.json`。

CO-HTTP-02 已获明确批准，并在隔离数据库通过主产品链路完成建会话、票据及一次 HTTP 提交，首次 inbox 路由错误及修复后的回执保留。接收端送达、已读仍需独立证据。测试完成后配置关闭，不能复用已完成测试文本；此前网页 CO-DM-01 与本次已有会话测试都不证明任意陌生人可联系。
