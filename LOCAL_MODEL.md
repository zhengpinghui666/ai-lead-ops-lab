# 本机模型运行与验证

2026-09-15 已部署版本：Ollama v0.34.0，地址 `127.0.0.1:11434`，模型 `qwen3.5:4b`，量化 Q4_K_M。原 API 保留，分析与拟稿新任务交替分配 1:1；生产任务分配记录在 `semantic_routes`，重试不改派。短文案入口只生成草稿。以下旧端口和手动部署说明属于早期记录，当前以本段及实测报告为准。

后台任务 `ClubOps Local Model` 调用 `scripts/start-local-model.ps1`，监听回环地址、禁用云模型、GPU 单并发、4096 上下文；Windows PowerShell 的 native stderr 必须由 Start-Process 重定向，否则普通运行日志会让启动脚本误退出。可在本地查看 `data/private/local-model-runtime/stderr.log` 诊断启动问题，勿向外部提交整份日志。

4B 与 9B 已实测 CPU、显存、物理内存、冷启动、预热后延迟和固定样本。报告：[模型实测](artifacts/local-model-20260915-1521/MODEL_REPORT.md)。选择 4B 给采集留资源，9B 不常驻。4B 样本分类41/48、9B46/48，不应表述为真实业务准确率。

回退：先在模型设置关闭“本地与 API 各一半”，保留原 API；如需源码回退，正常停服务后使用 `artifacts/local-model-20260915-1521/source-before.zip` 及 `semantic-before.json`。数据库迁移均新增表，可保留，不要覆盖后续业务数据。需要恢复数据库时必须先备份最新数据并评估停机后的新增记录。

最新选择（2026-09-10）：按用户提供的 API 切换为远程 `qwen3.8-max` 分析，见 `MODEL_API.md`。以下保留本机通道的启动说明与历史验证；API 分析不依赖 Ollama。

工作台支持本机 Ollama。模型服务单独启动；保存配置不启动服务、不下载模型，也不启动评论采集或发送消息。需求先由规则初筛，模型支持手动逐条触发或新内容自动入队，见 MODEL_QUEUE.md。

## 本机已准备的运行文件

- 便携式 Ollama 0.33.3：`.tools/ollama-v0.33.3/ollama.exe`。
- 模型目录：`.tools/ollama-models/`。
- 模型：`qwen3:4b-instruct-2507-q4_K_M`，Qwen3 4B Instruct，约 2.5 GB。
- 独立地址：`127.0.0.1:11435`，启动时关闭云端功能，最多加载一个模型、并行处理一个请求。

运行时与权重不进入源码包，迁移到另一台电脑需另行安装。当前运行时已完成 SHA-256 核对，识别到本机 RTX 5060，并通过云端关闭状态检查；推理结果以验证记录为准。

## 启动和停止

在项目目录的 PowerShell 终端运行：

```powershell
.\scripts\start-local-model.ps1
```

保持此终端运行，在“数据与设置 → 语义模型”使用上述模型名、地址、端口，启用后保存。选择一条已初筛的原文进行分析。按 Ctrl+C 停止该前台模型服务；不影响工作台的规则初筛。端口被占用时脚本拒绝启动，不终止占用者。脚本仅设置本进程环境，退出时恢复，没有开机自启或系统服务安装。

已有其他便携式运行库或模型目录时可显式指定：

```powershell
.\scripts\start-local-model.ps1 -Executable 'D:\Ollama\ollama.exe' -ModelDirectory 'D:\Models' -Port 11435
```

工作台的模型端口应与启动参数一致。模型分析默认 30 秒，上限 60 秒；结果仍需证据校验，超时或格式不符时保留规则。正式 8765 已加载新模型界面与 API；后续正常重启见 SERVICE_LIFECYCLE.md。

## 新机器准备

1. 从 [Ollama 0.33.3 官方发布](https://github.com/ollama/ollama/releases/tag/v0.33.3) 下载 `ollama-windows-amd64.zip`，校验后解压到新的 `.tools/ollama-v0.33.3/` 目录。此 Windows x64 包大小为 1,469,175,900 字节；SHA-256 为 `52cb36a62e7e501f61514f60212dec7117b6c098811357585e02fffe32d2fcd7`。保留包内许可证。
2. 运行上述启动脚本。在另一个 PowerShell 终端显式下载选定模型：

```powershell
$env:OLLAMA_HOST = '127.0.0.1:11435'
& .\.tools\ollama-v0.33.3\ollama.exe pull qwen3:4b-instruct-2507-q4_K_M
```

3. 按模型配置说明完成一次单条分析。下载完成不等于推理成功，推理成功也不等于识别准确率已经验证。模型实际标签摘要和本机验证记录应单独保存。

便携式 CLI 和模型路径设置依据 [Ollama Windows 文档](https://docs.ollama.com/windows)；关闭云端功能依据 [官方 FAQ](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features)。所选模型页面列出 Apache 2.0 许可证：[Ollama 模型条目](https://ollama.com/library/qwen3:4b-instruct-2507-q4_K_M)、[Qwen 原始模型](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507)。未向第三方提交业务原文。

## 证据位置

本机使用 Ollama 0.33.3 和 `qwen3:4b-instruct-2507-q4_K_M`，仅监听本地地址，已核对云端功能关闭。最新 `intent-prompt-v5` 对固定 12 条助手开发样本发起真实推理：11 条通过结构和原文证据检查，其中 10 条类别与开发标签一致，1 条将买方误判为卖方；另 1 条拒绝采用。6 条已采集评论均通过主产品单条分析流程并保存结果。输入不含 UID、昵称或登录凭证。

该批不是独立准确率评测，样本已参与提示词开发。不同提示词的旧结果保留，不与最新结果混算。运行时和模型留在本机 `.tools/`，不随源码分发；验证进程已停止。启动脚本通过语法与端口占用保护检查，正常启动脚本及成功模型结果的完整 UI 验收尚待补齐。原始证据为私有目录的 `results-v5.json`。

`artifacts/local-model-validation-20260910/` 保存本机隔离验证的固定输入、真实推理结果、运行日志及业务库副本；它包含私人业务记录，不随源码包分发。固定开发样本为助手编写，6 条已采集评论没有独立人工标签，不能合并为线上准确率。

模型输出、规则与人工判断的归属和版本规则见 [SEMANTIC.md](SEMANTIC.md)。
