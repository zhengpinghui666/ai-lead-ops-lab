# 需求识别：规则、模型与人工判断

2026-09-10。当前提供本机及远程 API 适配器、独立分析记录和有界自动队列。用户现选择 `https://api.a2agent.me/v1` 的 `qwen3.8-max`；正式新采集的 5 条评论已完成自动分析，规则保留为回退，人工判断优先。**尚无独立人工评测或线上准确率证据。** 保存配置不会分析历史原文、下载模型或启动采集。API 用法见 `MODEL_API.md`，自动队列见 `MODEL_QUEUE.md`。

## 当前使用路径

当前使用远程 API，无需运行 Ollama。此前本机 Ollama 0.33.3 与 Qwen 4B 的 6 条评论和 12 条固定助手样本仅为历史开发验证；10 条类别匹配、1 条误判、1 条结构／证据拒绝，不能作为独立准确率。详见 [LOCAL_MODEL.md](LOCAL_MODEL.md)。提示、字段格式和提供商模型标识均纳入各自引擎版本；升级后的旧结果留在历史中，不混用本机与 API 结果。

在“数据与设置 → 语义模型”选择通道并填写其配置，保存后可在已初筛的评论或弹幕证据旁点击“分析此条原文”。远程通道填写固定 HTTPS 地址、模型与密钥；本机通道填写已有 Ollama 的模型及监听地址。实际配置已加入忽略规则，API 密钥另存 DPAPI 密文。

本版支持单条手动分析与规则初筛后的新内容自动队列；远程队列支持 1–4 条有限并发，本机固定 1 条，并有容量、时限和取消控制，不补跑旧内容、不自动重试失败。页面按人工判断 → 当前原文及上下文版本的有效模型结果 → 规则结果展示分类；监控表和需求详情共用此投影。配置关闭或切换模型、最新一次调用失败时回退规则，历史结果仍可核对。低确定性模型结果进入“待判断”，保留原提议分类；没有模型概率或联系授权推断。

仅本机适配器限制为 `127.0.0.1`、`::1`，依次核对 `/api/status`、`/api/show` 后向 `/api/chat` 提交。远程 API 适配器则按用户指定的固定 HTTPS 地址请求，不跟随重定向；两种通道都不拉取模型、不调用搜索或工具。下文 Ollama 参数仅适用于本机通道。[Ollama 官方说明](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features)

使用 JSON Schema 结构化输出，`stream=false`、温度 0、输出上限 2048 token、上下文 16384 token，不允许静默截断。输入上限为当前原文 5000 字、上级原文 5000 字、标题 1000 字；单条总请求时限 5–60 秒，默认 30 秒。响应最多 256 KiB；超时中断本次连接，不自动重试。[结构化输出文档](https://docs.ollama.com/capabilities/structured-outputs)、[Chat API](https://docs.ollama.com/api/chat)

`/api/status` 的云端状态接口目前在官方客户端标为 experimental；旧服务不支持时会保留规则并显示未能核对，而不是跳过检查。状态结构和 `remote_host` / `remote_model` 字段依据官方 API 定义实现。[官方客户端](https://github.com/ollama/ollama/blob/main/api/client.go)、[API 类型](https://github.com/ollama/ollama/blob/main/api/types.go)

## 输入、输出和数据保存

- 输入只附带 `kind`、`text`、`parent`、`title`，不附加作者昵称、UID、浏览器会话、联系记录或人工答案。原文本身包含的内容不会被伪装成系统指令；请求没有工具能力。
- 当前原文以外的上下文只用于游戏和询价对象理解。除游戏外的字段证据必须来自当前原文。预算、时间、人数、段位、区服须完整复制对应片段；缺证据、改写数值或从上级搬运字段会拒绝整条模型结果。
- 游戏、服务方向使用固定枚举。字段证据必须能在对应输入中逐字找到，再由程序计算字符起止位置。此校验只证明证据片段存在，不能证明模型对否定、角色等语义理解正确；这仍需要标注评测。
- `intent_results` 独立保存规则快照、模型引擎、请求编号、输入哈希、状态、结果和时间。人工作业仍保存在原人工记录与历史中，模型重跑不覆写它们，也不修改原文、联系依据、草稿或消息。
- 开始调用前持久化请求编号。同一条原文与请求编号不会重复调用；同一来源记录只有一个模型分析在途，手动与自动任务共享通道并发上限。调用过程中原文、上下文或配置变化，结果标为 `stale`，只保留历史。服务中断的在途记录标为 `interrupted`，不自动恢复。
- 新迁移前通过 SQLite 备份 API 创建 `before-intent-results` 备份。已有规则可以直接存档；旧人工记录若没有保留原规则分类和理由，明确标为未知，不重新计算一条“历史规则结果”。

可替换接口位于 `semantic.py`：适配器接收固定配置，返回结构化结果和模型信息摘要；共享 `validate_result` 负责证据校验。现有实现包括本机 `OllamaAdapter` 和 `semantic_api.py` 的远程适配器。增加实现时仍须明确数据去向、实际方法和模型版本，不能把规则回退标成模型输出。

## 本地 API

所有 POST 沿用 Host、Origin 和 `X-ClubOps-Token` 校验。

| 操作 | 请求 |
| --- | --- |
| 保存配置，不启动分析 | `POST /api/semantic-save?mode=live`，使用配置样例中的字段 |
| 单条分析 | `POST /api/semantic-analyze?mode=live`，仅接收 `evidence_type`、整数 `id`、当前 `analysis_input_hash` 对应的 `input_hash`、唯一 `request_id` |
| 最近分析历史 | `GET /api/analysis-history?mode=live&evidence_type=comment&id=1`，最多 30 条 |

未初筛的评论需先完成规则初筛。人工核对表单内不放模型触发按钮，防止调用后丢失正在填写的人工判断。

## 固定开发评测

`evaluation/intent-development-v1.json` 是本次在首次运行前编写并固定的 **助手合成开发集**：63 条输入，去重后 62 条，涵盖六类、否定、转述、假设、询价上下文、角色冲突与服务提供者表达。不是用户真实分布，也不是独立人工标注集。

运行：

```powershell
python intent_eval.py --output artifacts/intent-development-v1-rules-v3.json
```

会同时生成 JSON 与 Markdown 报告，包含数据集和分类器源码摘要、每类混淆矩阵、TP/FP/FN、精确率、召回率、逐条误判漏判及全部预测。原规则 `rules-v2` 在这 62 条上有 24 条分类分歧；客户需求类 TP=5、FP=2、FN=6。转述与假设需求产生两条客户误判。**不把这一开发结果外推为线上准确率，也没有修改标签来使规则通过。**

后续 `rules-v3` 在相同标签上有 5 条分歧，均为普通讨论保守归入待判断；增加了另一组预先固定的 36 条助手开发样本。两组都参与了规则开发，不能当作独立验证。旧版报告原样保留，完整对比与历史保护行为见 [RULES.md](RULES.md)。

评测器按完整原文与上下文去重，保留上下文不同的相同短句；重复标注冲突时整组排除并单独报告，不投票制造答案。未确认标签、缺失预测另行计数；没有分母时显示未知。

真实模型或其他引擎的预测可以通过 `--predictions 文件.json` 独立评测。文件是数组，每条包含 `id`、`input_hash`、`category`、`method`、`engine`，必须与标注原文版本一致。单次报告不能混合规则和模型。样本中的 `origin`、`labeler`、`review_status`、`source_ref`、`rationale` 必须保留；真实独立人工标注尚待补充。

## 验证边界

全量 181 项 Python 与 11 个 Node 测试入口通过。覆盖原文版本变化、并发、重启中断、重复请求、人工判断优先、直播接入、字段归属、API 校验和本地 HTTP 协议。HTTP 协议测试使用隔离本机模拟服务及明确标注的合成输出，不能当作真实 LLM 验收。

日志：`artifacts/semantic-python-tests.txt`、`artifacts/semantic-node-tests.txt`。后续增加诊断阶段、开发报告源码摘要、设置编辑保护断言，并避免迁移重复扫描已存档规则以及视频统计的重复全表遍历；最后通过 42 项语义／规则／直播工作流测试和两个前端测试入口，日志见 `artifacts/semantic-followup-tests.txt` 与 `artifacts/semantic-frontend-followup.txt`。开发评测：`artifacts/intent-development-v1-report.json` 与 `.md`。

实际浏览器使用 8766 的合成记录验证：模型字段保持未保存编辑状态超过多个轮询周期，保存配置没有模型任务；点击单条分析后，本机服务连接失败，页面显示失败原因并保留原规则分类。数据库只有一条失败模型记录，消息、草稿、HTTP 私信尝试均为 0。此检查证明错误流程可用，不能证明真实模型推理。验证结束后关闭了该页面和临时服务，并恢复隔离配置为关闭。审计摘要见 `artifacts/semantic-validation-summary.json`。正式服务仍为 7 视频、65 评论、64 线索、0 消息、0 草稿，活动采集任务为 0。

原 8765 服务先前的重启操作被自动审批以 `blocked by policy` 拒绝，仍待用户手动重启；本增量的真实浏览器检查仅使用独立 8766 服务和隔离数据，不代表正式后端已升级。HTTP 私信鉴权、纯 HTTP 采集和完整闭环仍未验收。
