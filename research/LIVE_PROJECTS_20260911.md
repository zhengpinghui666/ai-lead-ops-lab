# 抖音直播监控 GitHub 调研

核对日期：2026-09-11。依据仓库 README、许可证、部分实现及 GitHub 公共 API。下述“支持”描述项目公开功能；本机没有运行这些第三方程序，不能据此认定它们已通过本机无畏契约房间实测。

## 选型结论

保留 ClubOps 工作台、房间发现、存档和模型队列。优先用 Dycast Desktop 做独立弹幕采集对照；协议问题参考 DouyinLiveWebFetcher；管理界面与会话记录参考 douyin-live-toolkit。最小接入单位是文字弹幕记录，不需要把礼物、PK、语音播报等功能一起搬进来。

| 项目 | 已核对能力 | 最近推送（UTC，API 快照） | 对 ClubOps 的价值与限制 |
| --- | --- | --- | --- |
| [qinant/dycast-desktop](https://github.com/qinant/dycast-desktop) | Tauri 桌面端；JSON WebSocket 转发；JSONL 录制和回放；有界重连 | 2026-06-25 | 最适合独立对照。需要转发适配器，不能直接当成现有 Python 模块。仓库为 MIT，来源声明见下文。 |
| [saermart/DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher) | Python WebSocket、gzip/protobuf、确认包及心跳；消息中有用户和消息标识 | 2026-09-08 | 优先核对协议字段和连接生命周期。AGPL-3.0，README 另含学习研究及禁止商业谋利声明，本轮没有复制其源码。 |
| [chuanyue98/douyin-live-toolkit](https://github.com/chuanyue98/douyin-live-toolkit) | 后端采集、直播监控台及历史报表，基于前一项目 | 2026-07-26 | 可参考房间状态与历史记录组织；直接替换现有系统改动较大。AGPL-3.0。 |
| [skmcj/dycast](https://github.com/skmcj/dycast) | TypeScript 弹幕解析及转发，是桌面项目来源 | 2026-04-12 | 可核对协议与原始实现。GitHub API 未识别许可证，直接复用前仍需确认授权范围。 |
| [adseng/dy-comment-cast](https://github.com/adseng/dy-comment-cast) | 较小的 TypeScript 弹幕接收／转发实现 | 2026-05-26 | 结构较易阅读，可参考适配层。package.json 标为 ISC，根目录未找到独立 LICENSE；不能写成“已确认无条件复用”。 |

推送时间只表示代码活动，不等于修复质量或当前接口可用性。上述仓库查询时均未归档。

## 可以直接试用的版本

[Dycast Desktop v1.4.1](https://github.com/qinant/dycast-desktop/releases/tag/v1.4.1) 发布于 2026-06-15，API 实际列出 Windows x64 `-setup.exe` 和 `.msi`；并非仅 README 声称存在安装包。其发布版早于主分支最新修改，主分支功能不能全部视为该安装包已包含。

[LICENSE](https://github.com/qinant/dycast-desktop/blob/main/LICENSE) 为 MIT；[NOTICE](https://github.com/qinant/dycast-desktop/blob/main/NOTICE) 明确说明源自 skmcj/dycast，并保留来源和提交历史。项目维护者对当前仓库给出 MIT 声明，上游 API 没有识别到许可证；商业源码复用时需要一并核对来源。独立测试工具与源码并入产品是两项不同决策。

项目转发示例使用 8765 端口，与 ClubOps 已占用端口冲突。实际联调应使用独立本机端口。未安装第三方程序、导入 Cookie 或使用外部签名服务。

## 对当前故障的帮助

本机 #14 观察到 27 个 `/webcast/im/fetch/` HTTP 200 响应，WebSocket 为 0，原采集器确实只监听 WebSocket。已在独立编写的现有协议解析器上补充页面 HTTP 响应读取，共用房间绑定、消息预算、过滤、去重和停止逻辑；没有额外主动轮询，也没有复制第三方签名代码。

[DouyinLiveWebFetcher 的协议定义](https://github.com/saermart/DouyinLiveWebFetcher/blob/main/douyin.proto) 用于交叉核对消息信封及文字弹幕字段。查阅的 `liveMan.py` blob SHA 为 `94a855375340e94b49b78c728f08d3aa2de4cbae`；Dycast Desktop `src/core/dycast.ts` blob SHA 为 `f95c43d76c3f040d0b6da6779457a1d36c6ffcfc`。这是代码阅读证据，不是本机成功采集证明。

新增 HTTP 路径后，#15 收到 26 次可解析响应，但没有文字消息，仍为 `no_data`。随后增加仅记录响应大小和数字字段编号的诊断，以区分空响应与结构不匹配；最终实测结果见 LIVE_MONITOR.md。HTTP 200、解析没有抛错、页面能打开均不足以证明已采到弹幕。

另查阅 [douyin-danmaku-tts](https://github.com/luohaojie-tt/douyin-danmaku-tts)：README 提到 HTTP／WebSocket／浏览器监听，实际 HTTP 连接器既监听已有响应，也有主动页面 fetch 路径。它主要服务语音播报，本轮不引入；不能仅凭 README 的稳定性星级断言采集可靠。

## 后续对照与接入标准

同一无畏契约房间、相近时间段，对比现有监听与独立客户端是否收到真实消息。保留消息 ID、实际房间 ID、UID、原文、平台时间和本机观察时间，缺失字段不猜测、不用回放时间冒充平台时间。

只有真实读取成立后，再接入已有本地消息存档与模型队列；对照重连重复、下播、取消、房间切换、超限、长期运行和断档。录制回放可用于回归，不计为新鲜弹幕或真实时效验收。当前还没有“安装后即可稳定全天无人值守”的本机证据。
