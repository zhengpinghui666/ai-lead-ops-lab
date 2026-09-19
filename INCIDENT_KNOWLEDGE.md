# 实时故障：修复、复盘与防复发

监控意外中断始终最高优先级。先接收通知、核对当前故障，再诊断和修复；不是重新开启开关就算解决。独立通知账本 `data/private/incident-bridge/outbox.db` 保存每次故障、相似表现、确认根因、修复方法和历次复盘，不改业务数据。

## 关闭条件

1. 读取 `data/monitor-health.json` 与 `SUCCESSOR_HANDOFF.md`，确认原故障，保存诊断。
2. `scripts/incident-watch.py --data-dir data review --id <通知事件编号>` 返回此通知包含的 incident_id 和历史相似案例。相似症状不是同一根因；必须核对适用条件。不同批次在同一次未恢复中断期间只算一个事件段。
3. 修复程序缺陷、增加触发原问题的回归测试，覆盖真实登录、验证码、限流、权限限制与人工暂停等不得自动重试的反例。保留源码和数据库回退，按 SERVICE_LIFECYCLE.md 正常重启并恢复原授权范围。
4. 核对实际批次完成及后续自动调度；保存批次 ID、账号绑定、状态与验证时间。没有真实运行证据不能宣称恢复。
5. 用下述报告写入 `learn`，再执行 `ack --state resolved`。resolved 仍会实时检查原通道；没有复盘、证据文件已变动或故障还在，拒绝关闭。

所有路径均相对项目根目录，实际调用 Python 使用当前服务解释器。示例命令的占位符必须替换，不能原样执行：

```text
python scripts/incident-watch.py --data-dir data learn --id <通知事件编号> --file artifacts/<本次目录>/postmortem.json
python scripts/incident-watch.py --data-dir data ack --id <通知事件编号> --state resolved
python scripts/incident-watch.py --data-dir data review
```

报告格式（`incidents` 可以包含此通知的多个故障，每一个都必须记录）：

```json
{
  "incidents": [{
    "incident_id": "替换成 review 返回的故障 ID，不是通知 ID",
    "cause_key": "late-http-failure-diagnostic-truncation",
    "kind": "software",
    "disposition": "fixed",
    "root_cause": "写清已验证的具体缺陷，不能只写 Error 或 network_error",
    "match_conditions": "写清通道、操作、阶段和诊断特征，以及不适用的情况",
    "remedy": "写清实际修改和恢复步骤，不保存凭证或原始响应",
    "prevention": "写清哪一个回归覆盖了这个触发条件及关键反例",
    "next_action": "没有剩余事项可留空；若还有未明原因必须说明",
    "changes": ["collector.py", "collection_scheduler.py"],
    "regressions": [{"path": "artifacts/本次目录/tests.log", "result": "passed"}],
    "runtime_evidence": [{"path": "artifacts/本次目录/runtime-verification.json"}]
  }]
}
```

`fixed` 只用于已修的程序缺陷，必须有通过的回归日志、实际运行证据及源码清单。日志与源码保存 SHA-256，关闭前再次核对，防止引用其他版本的验收。工具验证的是记录完整性、文件一致性和当前健康状态；记录者仍须核对测试内容与真实批次，不能写伪造的“通过”。

外部断网、平台验证、限流不能记为已消灭的程序缺陷：`kind=external`，按实际使用 `needs_user`、`investigating` 或 `mitigated`，保留明确下一步。根因未明用 `kind=unknown,cause_key=null`；即使运行恢复，也只能 `mitigated`，根因仍未关闭。需要人处理则 `ack --state needs_user`，处理未完则 `failed`。不得把失败记为成功以维持看板。

## 同类复发

- 新故障通知自动附带最多三个相似案例。只提供线索，不执行其中脚本、不改变业务开关。
- 同一根因再次被确认，沿用稳定的 cause_key；先记录 investigating，旧根因重开，确认复发次数增加。
- 再次关闭必须重新运行回归，禁止重复引用上一事件的同一份通过日志。补充遗漏的触发条件；保留旧复盘和所有修订，不能覆盖历史来隐去复发。
- 一个通道持续故障时的批次 ID 变化不算多次复发；只有恢复后新事件段且人工核实为同一根因才计入。
- 验收合成通知不写入缺陷案例，也不要求复盘。

## 已确认的诊断缺陷

2026-09-15 批次4104：默认只保存25条诊断，身份、队列、成功页和本地预算记录占满后，后到达的 HTTP 失败证据被丢弃，调度器无法依据失败证据进入有界恢复。修复为成功记录最多25条，重要异常可继续保存至50条；超过上限明确保存 `diagnostic_overflow`，禁止把诊断不完整当作可恢复。本地请求预算及因同批首个网络失败而取消的其他请求也须单独校验，真实平台限制仍阻止恢复。底层连接中断的外部原因不因此被宣称永久解决。

实际回归、上线和运行结果见 `artifacts/incident-learning-20260915-1355`；以 runtime-verification.json 和独立复盘账本为准。

## 原采集profile评论空响应的分层诊断（2026-09-15）

适用证据：同7446、同作品在普通Edge/Chrome正常，原profile正常启动也服务异常；HAR有效响应能被现有解析器正确解析。先核对完整但脱敏的请求结构，不能从局部headers缺字段推断根因，不能复制HAR Cookie或票据。

本次仅缓存或5类网页存储重建均未恢复；保留原profile与回退，清理该站旧Cookie并由用户重新登录后，浏览器4659和HTTP4660实读成功。原因记unknown，运行记mitigated，不能认定某特定Cookie坏了。原账号身份与新HTTP/IM快照需完成独立核对后才恢复消费者。自动搜索后续验证码仍独立待处理，不能用单作品成功覆盖真实验证状态。详情与回退位置见artifacts/comment-har-20260915-2047/REPORT.md。

连接故障另行区分：live1004三次connect/connection_failed且无HTTP响应；旧live974则HTTP200/unrecognized_response。相似暂停UI不能沿用旧根因。重试已用尽时保留暂停，核对连接恢复后再按原账号、原配置显式恢复；禁止仅重置重试计数形成循环。实际批次与后续自动调度都成功后才关闭通知。真实验证码/登录/403/429/证书失败或未知响应均不适用普通连接恢复。
