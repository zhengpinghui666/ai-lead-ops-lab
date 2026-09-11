# 抖音采集项目调研：后端优先的选型与接入方案

**实现后修订（2026-09-10）：** 进一步审查发现 TikTokDownloader 的 `douyin_params.py` 包装函数返回固定 a_bogus 字符串，不能直接采用。实际接入使用其独立动态模块读取一级评论、F2 GET 参数模块读取回复，正常运行均为纯 Python，暂不需要 Node 签名服务。评论／回复和隔离入库已实测；关键词搜索返回 verify_check，待人工验证。最新证据见 [COLLECTION_HTTP.md](C:/Users/admin/Documents/杂物/ai-lead-ops-lab/COLLECTION_HTTP.md)。以下正文为实施之前的静态调研快照，其中“尚未实现／实测”的表述以此最新增量为准。

调研日期：2026-09-10。对象：现有 ClubOps 工作台，普通个人抖音账号，电脑端运行，优先通过后端采集视频和评论，再交给现有筛选与消息模块。

**建议优先验证 TikTokDownloader 当前版本的后端采集路线：Python 异步 HTTP + 必要时本机 Node.js 参数处理，并在 ClubOps 内设置可替换的采集适配层。F2 作为第二候选。** 这个判断针对当前需求和本次审查的七个公开仓库，不代表任何项目已通过本账号实测，也不代表非公开接口可以长期免维护。

本次检查了固定提交下的 47 个文件、仓库目录和部分关键文件提交历史；下载内容仅用于静态审查，没有运行第三方入口、安装依赖或向抖音发送新的采集／私信请求。

## 1. 项目比较

“浏览器依赖”指已检查的正常采集请求路径，登录和人工验证另计。“最近提交”指默认分支 HEAD 的提交日期，统一为 UTC；不是 GitHub 页面更新时间，也不保证某个接口刚刚修复。

| 项目／代码证据 | 实际实现 | 已看到的采集能力 | 正常请求的浏览器依赖 | 默认分支最近提交 | 本次建议 |
| --- | --- | --- | --- | --- | --- |
| [TikTokDownloader](https://github.com/JoeanAmier/TikTokDownloader/blob/df8aced70e476ae3330fa913186f3207b4843201/src/interface/comment.py) | Python、curl_cffi 异步 HTTP；本地 Python／Node.js 参数处理 | 综合／视频搜索、一级评论、回复和分页 | 请求代码可在后端运行；Node.js 不等于 Chrome | 2026-09-08 | **优先做兼容性验证**；GPL-3.0，复用时还需核对所带 JS 的来源 |
| [F2](https://github.com/Johnserf-Seed/f2/blob/7dab3e2ffffaa2535834d28fca99dbc2e89fa9d3/f2/apps/douyin/crawler.py) | Python、httpx 异步 HTTP，本地参数算法 | 作品、评论、回复、主页内作品搜索等 | 已查评论请求无需浏览器 | 2025-10-12 | 结构清楚，Apache-2.0；评论读取的第二候选，任意关键词发现能力需补查 |
| [Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/42784ffc83a72a516bfe952153ad7e2a3998d16c/crawlers/douyin/web/web_crawler.py) | 后端 HTTP 采集，再用 FastAPI 暴露接口 | 视频详情、评论、回复、热榜等 | 已查请求无需浏览器 | 2025-10-12 | 可参考 API 分层；当前评论路径仍调用 X-Bogus 辅助逻辑，兼容性待测；文件许可来源需复核 |
| [DouYin_Spider](https://github.com/cv-cat/DouYin_Spider/blob/9afaf79580b1ee84e8954ff906ff26869d5b7f1f/dy_apis/douyin_api.py) | Python、curl_cffi、本地参数逻辑和 Node VM 辅助运行时 | 搜索、评论、回复，以及其他账号／消息相关接口 | 已查采集可走后端 | 2026-08-30 | 技术参考价值高；目录未发现许可证，不能按可自由复用处理 |
| [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler/blob/d6f7c5bb906b6dac40ddf343ef9e26438a3de092/media_platform/douyin/client.py) | httpx 请求 + Playwright 页面参与请求准备 | 关键词搜索、评论、回复 | **有**，每次参数处理仍读取页面上下文 | 2026-08-14 | 不作为本需求的主采集器；另有非商业学习许可限制 |
| [ad-deeke](https://github.com/DeekeScript/ad-deeke/blob/73353edd828ff605d8875ca3427717e8c0fb91e2/app/dy/Search.js) | Android UI 控件查找、点击、滑动 | App 内搜索与评论操作 | 依赖 Android App 自动化环境 | 2026-08-17 | 可参考业务流程，不采用其采集执行方式；目录未发现许可证 |
| [douyin-web-api-sdk](https://github.com/Rockedw/douyin-web-api-sdk/blob/2c9d76d595717b5a186527586d3f91b6c26dc541/src/main/java/com/dy_web_api/sdk/message/handler/VideoCommentListHandler.java) | Java HTTP/2、Cookie、本地参数辅助 | 已见评论读取及消息相关协议代码 | 已查评论请求无需浏览器 | 2026-04-27 | 当前消息实现的相邻参考；不据此认定完整搜索采集已解决；目录未发现许可证 |

维护判断还参考了关键路径：TikTokDownloader 的 `douyin_params.py` 在 2026-09-07 有参数处理修复；F2 的 `crawler.py` 最近修改为 2025-03-06；DouYin_Spider 的主 API 文件最近修改为 2026-08-30。新提交只能提高验证优先级，不能代替实际返回数据的验收。[TikTokDownloader 提交](https://github.com/JoeanAmier/TikTokDownloader/commit/3d42cd6e0bf99912466302b61bf897b7769f8de6)、[F2 提交](https://github.com/Johnserf-Seed/f2/commit/d34de2754b5a18ba3de6776af15f6c0a448dac49)、[DouYin_Spider 提交](https://github.com/cv-cat/DouYin_Spider/commit/fe3eb249c5a6cb0f4ad27ba8a0aa2f3879951215)。

## 2. 它们是怎么做的

### 后端 HTTP 路线

共同结构是：取得有效账号会话 → 按具体接口组装参数 → 本地生成所需动态参数 → 发 HTTP 请求 → 校验业务响应 → 按游标翻页。Cookie、请求上下文和动态参数均参与请求，不能概括成“一个 URL 加 UID”。F2 的评论／回复方法调用本地参数管理器后进入 HTTP 客户端；TikTokDownloader 则把请求参数处理统一放在模板层。[F2 评论调用](https://github.com/Johnserf-Seed/f2/blob/7dab3e2ffffaa2535834d28fca99dbc2e89fa9d3/f2/apps/douyin/crawler.py#L196)、[TikTokDownloader 请求模板](https://github.com/JoeanAmier/TikTokDownloader/blob/df8aced70e476ae3330fa913186f3207b4843201/src/interface/template.py#L323)。

TikTokDownloader 的当前参数模块同时有本地 Python 逻辑和通过 JSPyBridge 调用 Node.js 的分支。后者在 Node VM 中加载本地 JS 文件，已检查路径不要求启动 Chrome。缺少 Node 或特定上下文时存在返回较少参数的分支，因此接入时应报告“参数能力未就绪”，不能把降级后的请求默认当成可用。[参数模块](https://github.com/JoeanAmier/TikTokDownloader/blob/df8aced70e476ae3330fa913186f3207b4843201/src/encrypt/douyin_params.py)。

评论与回复是不同调用：一级评论按视频 ID 和游标读取；回复另外传父评论 ID。搜索也有自己的分页状态，TikTokDownloader 保存 `offset` 和 `search_id`。只实现第一页评论，或者把一级评论游标拿去读回复，都不足以形成完整采集器。[评论／回复](https://github.com/JoeanAmier/TikTokDownloader/blob/df8aced70e476ae3330fa913186f3207b4843201/src/interface/comment.py)、[搜索](https://github.com/JoeanAmier/TikTokDownloader/blob/df8aced70e476ae3330fa913186f3207b4843201/src/interface/search.py)。

### HTTP 服务不一定摆脱浏览器

MediaCrawler 的 DouyinClient 在参数准备时执行 `playwright_page.evaluate` 读取页面存储，并在相应分支借助页面生成参数，然后才交给 HTTP 客户端。这是浏览器辅助采集，不能因为外层能包装成 HTTP API，就称为正常运行无需浏览器。[请求准备代码](https://github.com/NanmiCoder/MediaCrawler/blob/d6f7c5bb906b6dac40ddf343ef9e26438a3de092/media_platform/douyin/client.py#L70)。

### 不应混淆的能力

- F2 已查的 `fetch_home_post_search` 是主页内作品搜索，搜索推荐词也不等于全站关键词视频结果。
- Evil0ctal 已查的 `fetch_hot_search_result` 是热榜能力，不能拿它替代业务关键词发现。
- ad-deeke 的搜索使用 Android 控件选择器和点击，评论对象读取 UI 控件；它不能直接改一个后端地址，就变成电脑上的纯 HTTP 采集器。

以上分别依据 [F2 爬虫](https://github.com/Johnserf-Seed/f2/blob/7dab3e2ffffaa2535834d28fca99dbc2e89fa9d3/f2/apps/douyin/crawler.py#L142)、[Evil0ctal 爬虫](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/42784ffc83a72a516bfe952153ad7e2a3998d16c/crawlers/douyin/web/web_crawler.py#L249)、[ad-deeke 评论对象](https://github.com/DeekeScript/ad-deeke/blob/73353edd828ff605d8875ca3427717e8c0fb91e2/app/dy/Comment.js)。

## 3. 为什么不建议原样整包接入

TikTokDownloader 的 HTTP Session 默认设置 `verify=False`，接入应启用 TLS 证书校验。请求日志已主动剔除名称为 `Cookie` 的请求头，不能说它完全没有脱敏；但代码仍记录 URL、参数及全部响应头，因此 ClubOps 应采用字段白名单，避免保存令牌或 Set-Cookie。这里是对日志调用和默认配置的静态发现，没有据此断言发生过凭据泄漏。[Session 配置](https://github.com/JoeanAmier/TikTokDownloader/blob/df8aced70e476ae3330fa913186f3207b4843201/src/tools/session.py)、[日志代码](https://github.com/JoeanAmier/TikTokDownloader/blob/df8aced70e476ae3330fa913186f3207b4843201/src/interface/template.py#L419)。

代码复用还需逐文件核对：TikTokDownloader 根许可为 GPL-3.0，参数模块另列 JS 来源；Evil0ctal 根许可为 Apache-2.0，但 `abogus.py` 文件头明确记录来自 GPL 项目的代码。这些信息不足以给整个依赖链作许可兼容结论。MediaCrawler 的许可证明确限制非商业学习用途，商业使用需要作者书面授权。没有许可证的公开仓库也不能直接作为可分发依赖。[TikTokDownloader 许可](https://github.com/JoeanAmier/TikTokDownloader/blob/df8aced70e476ae3330fa913186f3207b4843201/license)、[Evil0ctal 文件声明](https://github.com/Evil0ctal/Douyin_TikTok_Download_API/blob/42784ffc83a72a516bfe952153ad7e2a3998d16c/crawlers/douyin/web/abogus.py)、[MediaCrawler 许可](https://github.com/NanmiCoder/MediaCrawler/blob/d6f7c5bb906b6dac40ddf343ef9e26438a3de092/LICENSE)。

因此，技术验证优先级与代码能否直接纳入产品是两个判断。拆成独立进程有利于维护，但本身不能证明免除许可义务。对于准备复用的具体版本，应先列出实际依赖及文件来源，再确定纳入范围。

## 4. 适合 ClubOps 的推荐架构

以下是根据源码审查和现有工程状态提出的设计，尚未实现或验收。

业务链路：**关键词／指定视频 → HTTP 搜索与评论采集 → 标准化、去重、时间筛选 → 需求判断 → 线索审核与发送队列 → 现有 UID HTTP 消息适配器。**

| 层 | 推荐实现 | 作用与边界 |
| --- | --- | --- |
| 会话管理 | 继续使用项目自己的受保护会话存储，校验账号身份和有效期 | 登录、人工验证单独处理；正常采集不自动启动浏览器 |
| 参数适配 | 按接口封装参数提供器，必要时调用本机 Node.js | 可以替换上游算法；记录版本与就绪状态；失败明确上报 |
| HTTP 读取 | Python 异步客户端，固定目标域名，启用 TLS 校验 | 超时、响应体上限、取消、受控并发；不记录原始认证头 |
| 数据归一化 | 视频、评论、作者标识、父评论、发布时间及来源统一入库 | UID 等长数字标识保留字符串；缺失信息明确标为未知 |
| 任务与筛选 | 复用本地任务／数据库结构，再补自动模型队列 | 单机第一阶段无须引入 Redis 或额外远程服务 |
| 消息 | 保留现有独立 UID HTTP 通道及去重、结果不明时不重发的约束 | 采集成功不自动代表该用户可被私信，也不代表送达 |

建议采集接口先收敛为 `search_videos`、`list_comments`、`list_replies` 三项，统一返回数据、下一页状态、是否还有数据及错误分类。关键词搜索和指定视频入口分开，便于先验证评论链路。不要把某个开源项目的完整配置对象贯穿到业务层。

从评论响应的作者对象保留实际返回的标识字段，并注明来源；只有明确取得数字 UID 才能进入使用数字 UID 的后续通道。`sec_uid`、展示抖音号、OpenID 不应混存为同一个 UID，更不能只凭字符串形状推断可用于发送。

“最近七天评论”按评论自己的发布时间过滤；视频搜索的时间筛选不能替代评论时间筛选。发布时间缺失时不应伪装成采集时间。

浏览器可作为首次登录、人工验证或明确标注的备用采集方式。若回退到浏览器，任务结果必须显示这一事实，不能继续标记为纯 HTTP 成功。

## 5. 最小验证顺序与通过标准

1. **指定视频的一页评论。** 使用已有合法会话及已知有评论的视频，小页读取；实际调用期间不启动浏览器。检查 HTTP 状态、业务状态、JSON 结构，以及评论 ID、作者标识、原文、发布时间和视频关联。
2. **翻页与回复。** 验证第二页游标推进、重复过滤和一条父评论的回复分页。不能以空响应作为“零评论”或“已结束”的成功证据。
3. **关键词搜索。** 验证返回视频确实对应关键词，并能把搜索结果的视频 ID 传入评论读取。
4. **业务串联。** 新采集评论入库，经过时间筛选和需求判断形成带来源的线索；补齐模型自动任务调度。采集验收本身不需要实际发私信。

若出现登录失效、验证挑战、429、空响应或不符合预期的业务数据，暂停该任务并给出原因。一次成功不能形成长期可用性、吞吐或安全发送频率承诺。

原先简单 HTTP 评论探测返回空响应，只能说明当时请求没有取得可用数据。当前项目中的动态参数及上下文处理，提供了下一轮对照验证方向；尚不能断言原失败唯一原因是缺少某个签名，或补上后必然成功。

## 6. 当前完成状态

本次完成的是选型调研和接入设计，**纯 HTTP 采集还没有通过本账号的真实数据验收，采集到私信的自动闭环仍未完成。**

此前已分别验证浏览器采集，以及一条 UID HTTP 私信获得平台接受响应；后者不等于接收方可见或任意陌生 UID 都可发送。正式服务仍有版本切换和模型自动调度等独立缺口。这里不把分段测试当成完整业务链路验收。

与较早的仓库评估相比，本报告扩大了后端采集候选范围，并核对了 TikTokDownloader 最近的参数实现，因此将其提高为技术验证第一候选。旧报告中的产品状态属于当时快照，不应用来判断当前消息通道状态。

## 7. 可追溯资料

- [仓库版本与默认分支提交快照](C:/Users/admin/Documents/杂物/ai-lead-ops-lab/artifacts/github-backend-review-20260910/metadata-summary.json)
- [47 个下载文件的固定版本 URL、字节数与 SHA-256](C:/Users/admin/Documents/杂物/ai-lead-ops-lab/artifacts/github-backend-review-20260910/source-manifest.json)
- [关键代码路径的最近提交记录](C:/Users/admin/Documents/杂物/ai-lead-ops-lab/artifacts/github-backend-review-20260910/critical-path-history.json)

以上索引用于复核调研证据，不是已安装依赖或产品发布清单。
