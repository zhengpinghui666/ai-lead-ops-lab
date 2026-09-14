# 故障实时反馈

用户授权范围：将 ClubOps 故障反馈到现有“AI获客”任务，收到后按既有授权诊断、修复、测试、备份和正常恢复服务。监听器自身不采集、不联系客户，也不修改业务开关。

## 实际链路

Windows 目录变更事件 → 只读检查已提交的数据库状态 → 独立持久化通知账本 → 安装版 `codex queue --thread … --message …` → 现有任务接收 → 接收/处理回执。

监听器以 `ClubOps Incident Bridge` Windows 任务常驻，登录后启动、异常退出由任务计划程序重启。正常事件有 250ms 合并等待；每 10 秒另作进程失联、未提交事务与投递失败的兜底检查，不调用模型。服务缺失持续 15 秒才产生通知，避免短暂启动过程误报。原每小时 Codex 巡检仅作为额外兜底，不是这条链路的触发器。

评论/直播的 attention、任务卡住、调度过期、群与收件读取异常会排入通知；手动暂停、未配置和正常退避不会。多群异常合并，一次只允许一个尚未接收/结算的投递；新故障保存在账本中等待接续。恢复后再次出现的故障是新的事件。

## 状态不能混用

- `queued`：CLI 返回目标任务与消息编号，证明入队；不代表模型已接收。
- `received`：现有任务实际执行接收回执。
- `resolved`：接收后，原故障入口通过只读复核；不等于平台送达客户私信。
- `needs_user`：需要用户验证或处理限制；不自动反复重发。
- `unknown`：投递超时或结果无法确认；不盲目补发，后续接收回执可消除不确定性。
- `retry`：确认 Codex 子进程没有启动，允许有限频率重新尝试连接。

应用关闭、电脑休眠、模型额度不可用、当前任务正在工作，都会影响实际开始处理时间。队列接收时间、模型确认时间、完成时间分开记录；不承诺零延迟修复。

## 操作与维护

状态：`python scripts/incident-watch.py status`。独立账本在 `data/private/incident-bridge/outbox.db`，只含故障元数据、时间与回执，不进入源码备份。`status.json` 是监听器心跳；首页健康数据只有心跳新鲜且实际接收验证完成时才标记连接成功。

正常维护前可执行 `python scripts/incident-watch.py hold --seconds 300`，临时停止新投递；事件保留。维护完成执行 `hold --seconds 0`。最多 30 分钟并自动过期，不能永久隐藏停机。修改监听器只重启 `ClubOps Incident Bridge`，不重启业务服务。

真实投递验证：`python scripts/incident-watch.py self-test`。该命令只产生合成通知，不伪造业务故障；收到的消息要求现有任务执行 `ack --id <事件编号> --state received`，再执行 `resolved`。当前任务忙时排队，需要真正结束当前轮后核验空闲唤醒，不能由发送进程代填接收回执。

卸载：停止并删除 `ClubOps Incident Bridge` 任务，保留通知账本供审计；原评论、直播、群和收件开关不受影响。

官方协议参考：[App Server](https://learn.chatgpt.com/docs/app-server)。本机 `codex queue --help` 已确认支持向现有任务排入消息；首次连接验证返回了实际队列编号。Windows 不支持 daemon 生命周期命令及 proxy，本实现未启动第二个 app-server、创建新任务或改写 Codex 内部数据库。
