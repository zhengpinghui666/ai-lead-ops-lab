# 个人号 HTTP 会话与消息读取

2026-09-11。`uid_inbox.py` 增加选定数字 UID 的会话核对与消息读取，复用 `uid_session.py` 的 Windows 用户密文认证。正常读取不启动浏览器。当前提供 Python 接口与会话核对命令；工作台按钮、入站消息持久化及与草稿任务的关联尚未接入。

## 当前真实结果

- 独立 IM 会话超过本地 12 小时期限后，沿用项目浏览器准备认证。浏览器关闭后，一次 HTTP 资料身份与一次 IM 读取通过，重新保存 DPAPI 密文；采集会话没有被清除，发送配置仍关闭。
- 首次准备只返回 `session_not_saved`，原因无法追溯。已新增固定阶段诊断和不含字段值的认证结构摘要；随后准备成功不能证明首次故障原因已修复。
- 对此前已验证的 5 个新采集 UID，普通收件箱三页共观察 30 条会话，未匹配到这 5 个 UID，且列表仍有后续页。该轮陌生人列表响应未通过关联核对；整体记录保留 `read_failed`，不能把这些人认定为陌生人。
- 对已明确授权的历史 CO-HTTP-02 双方及会话编号，纯 HTTP 读取一页 5 条已有消息，3 条解析为文字，其中 1 条入站、2 条出站，并匹配到原测试文字。没有创建会话或重复发送。该入站文字是历史记录，尚未证明它回复了该测试消息，也没有接收端送达／已读证据。
- 真实读取发现响应字段 5 是 `inbox_type`，并非附加错误码。已修正为与请求收件箱匹配，并补充回归覆盖。修正后的陌生人列表云端复核在连接阶段失败，未取得响应；该分支目前只有离线修正验证。陌生人消息读取也只有合成测试。

证据位于 `artifacts/uid-read-20260911/`：`bootstrap-diagnosed.json`、`collected-conversations.json`、`existing-message-read.json`、`stranger-page-corrected.json`、`stranger-page-corrected-diagnostic.json`。保留原失败，不能用后来的成功覆盖。真实核对前后 8 张业务表逐行比较相同，正式库仍没有新增消息或发送尝试。证据只保存已选目标、消息标识、正文摘要、请求阶段和响应摘要，不保存 Cookie、票据或原始响应正文。

## 使用

有有效的独立 IM 会话时，在项目目录执行：

```powershell
python uid_inbox.py --recipient REPLACE_WITH_NUMERIC_UID --pages 3
```

`--recipient` 可以重复提供，最多 20 个不同的数字 UID；不接受浮点数、抖音号、sec_uid、OpenID 或当前账号自己。每个收件箱最多 1–5 页，每页最多 20 条。默认单次核对最多一次身份请求及六次列表请求，没有循环刷新或自动重试。此命令不登记联系人、不创建草稿、不发送消息，也不读取匹配对象的消息正文。

Python 接口：

```python
import uid_inbox
import uid_session

provider = uid_session.Provider()
sender = provider.current()['sender_uid']
check = uid_inbox.scan(sender, ['REPLACE_WITH_NUMERIC_UID'], provider=provider)
# 仅在检查返回了确切匹配的会话后，选择其中一条传给 messages。
conversation = check['targets']['REPLACE_WITH_NUMERIC_UID']['conversations'][0]
page = uid_inbox.messages(sender, conversation, provider=provider, limit=20)
```

调用方需要先检查状态及会话列表是否为空。普通会话支持返回 `next_cursor` 后显式读取下一页；陌生人消息接口没有请求条数或游标字段，响应最多解析 100 条，输出仍限制为所请求的条数，超出时标明 `output_truncated`，不虚构分页结束。

## 结果与边界

- `existing_conversation` 只证明当前读取范围内存在该双方会话；`not_observed` 只证明本次已读取范围未匹配。失败保持 `unknown`，已经取得的正向会话证据保留。列表读取完成也不能证明联系人关系或被删除的历史，始终返回 `contacts_checked=false`、`history_exhaustive=false`。
- 返回值核对请求命令、序列、登录者 UID、业务状态、收件箱和响应体类型；会话双方与短 ID 校验后才返回目标匹配。未匹配用户及会话票据不进入结果。
- 文字消息核对会话、短 ID、作者、服务端消息 ID，UID 与消息 ID 使用字符串保存。非文字、不可解析正文和非正常状态有跳过计数；不会以空文字冒充有效回复。时间保留为平台原始整数串，尚未转换单位。
- 所有操作只使用四个固定读取路径；不调用创建、发送、删除、标记已读等接口。陌生人读取显式传 `reset_unread_count=false`，消息在发送方会话中存在不等于接收方已读或送达。
- 消息读取不会自动授予联系依据、变更草稿状态或计为导流。后续需把入站服务端消息 ID 去重、来源和账号关联接到业务库及界面，再验收真实回复与导流。

字段依据为此前保存的抖音网页公开编解码资源 `15347.ee40c89f.js` 与 SDK 封装 `20546.cd8aa1ca.js`，位于 `artifacts/uid-auth-review-20260910/public-client-assets/`。本模块独立实现，不把网页脚本或其他项目的认证材料作为运行依赖。

## 本轮验证版本

`artifacts/uid-read-20260911/focused-tests.log` 在 2026-09-11 01:34（北京时间）记录 78 项 UID 专项通过；这些是离线测试，不是 78 次真实平台请求。真实普通消息读取先于响应收件箱字段修正，记录保留当时源码哈希；修正后的陌生人云端连接失败也单独保留，没有把旧成功改写为新版本实测。本轮未改 UI，未重启 8765；该模块尚未成为工作台的可用消息入口。
