# 抖音验证码项目核对（2026-09-11）

## 21:30 增量：透明拼图与后续主动研究

用户要求新题型主动检索 GitHub 并适配，通用模型选型暂缓。本轮工具列表没有可调用的 GitHub 插件仓库接口，实际通过 GitHub 网页调研；不能称作插件执行。既有 GitHub 命令行用于源码备份。

#56 在零字节搜索响应旁出现透明拼图，原工作器在识别前拒识。核对已锁定 ddddocr 1.6.1 的本机源码：滑块引擎先转 RGB 再执行 Canny 匹配，未保留 alpha 掩码，不能直接去掉项目的拒识条件。

| 参考 | 核对与取舍 |
| --- | --- |
| [sml2h3/ddddocr](https://github.com/sml2h3/ddddocr) | 本地边缘匹配与双背景差分。保留 OCR 和不透明滑块路线，透明图片新增独立模块，未升级依赖。 |
| [gbiz123/tiktok-captcha-solver](https://github.com/gbiz123/tiktok-captcha-solver) | README 明确是 SadCaptcha 客户端。此前固定版本的 Douyin iframe 参考保留，没有调用外部服务；此次固定源码页读取失败，未当作新的源码复核成功。 |
| [Hiram-Wong/captcha-bypass](https://github.com/Hiram-Wong/captcha-bypass) | README 提供本地滑块及 TikTok 双图旋转接口，留作实际遇到旋转题时的候选，接口示例不等于本账号实测。 |
| [GitHub 透明模板示例](https://gist.github.com/MichaelSnowden/2b5ab97322b1e8d1631df59c6a71a0e1) | 展示 alpha 掩码思路，但使用旧 OpenCV、特殊 alpha 值和四通道比较，不能照搬；未复制代码。 |
| [OpenCV 模板匹配文档](https://docs.opencv.org/4.13.0/de/da9/tutorial_template_matching.html) | 按掩码与相关性定义自行实现中心化 RGB 匹配，显式处理平坦图像和数值异常。 |

实际采用 captcha_slider.py 的 alpha-slider-v1。有效纹理须在三个掩码收缩程度下均得分至少 0.80、独立候选差至少 0.06、位置一致到 1 像素；保持完整 PNG 画布中心坐标。分数不是概率，图片尺寸／有效面积／纹理不足仍拒识。没有新增依赖、收费服务或业务模型调用，真实图片留在本机。

3 组真实图片均可离线预测，14 项合成图片检查及相关回归通过。新真实搜索 #57 没有观察到验证码而返回空响应，故本轮无真实提交和恢复证据。详见 CAPTCHA_WORKFLOW.md。

后续新题型主动按“取图 → 题型 → 图像识别 → 布局／提交 → 平台判定 → 恢复读取”定位缺口；先核对 GitHub 项目接口及许可证，再做正反例和有界实测。不以反复开启监控代替适配，不将新题型硬套滑块，也不将图片预测记为平台通过。


**后续实现与真实观察更新（15:32）**：自建本地颜色／轮廓／双模型 OCR 与确认流程已在正式 #45 新题上观察到一次真实恢复：约 7.4 秒后提示解除、操作后新发起的对应搜索请求有效返回，随后读取一个视频的 10 条时间窗口外评论。当前版本仅 1 次真实处理、1 次恢复，长期通过率未知；不是纯 HTTP 验证。饱和度分离使 11 张开发／回归图片正确定位由 9 张增至 10 张，同色重叠仍拒识；累计私有图片 12 张不进源码包。#41—#44 的原失败历史保留。没有引入下列外部服务，详见 `CAPTCHA_WORKFLOW.md`。下文项目对比及“本次仅调研”等表述为实现前历史记录。

当前结论：**优先免费、本地运行；复用已经接入的 ddddocr，按真实题型补充识别能力。** gbiz123/tiktok-captcha-solver 的抖音 iframe 分支曾为页面适配提供参考，但 #33 已明确进入点选分支，继续调整滑块位移不能解决该题。SadCaptcha 是外部付费服务，其免费试用不作为长期免费方案。

## 最新筛选：免费服务优先

用户已明确优先免费。2026-09-11 重新检索 GitHub、CSDN，并读取项目文档和供应商官网后，按以下顺序评估：

| 顺序 | 方案 | 免费范围与后端形式 | 当前适用边界 |
| --- | --- | --- | --- |
| 1 | [sml2h3/ddddocr](https://github.com/sml2h3/ddddocr) | 开源本地识别，没有按次 API 费用；本项目已有隔离识别进程，文档另提供可选 API 依赖。 | 继续用于已适配滑块。字符 OCR、目标框检测不能直接等同于任意点选题求解；当前抖音点选仍未接通。 |
| 2 | [Hiram-Wong/captcha-bypass](https://github.com/Hiram-Wong/captcha-bypass) | 提供本地 CLI 与 HTTP Server，工程为 MIT 许可；有本地 ONNX、滑块、旋转、检测模块。使用本地模式无需购买识别次数，自行承担电脑资源开销。 | 最贴合“现成免费 HTTP 服务”的补充候选；仍需页面取图、题目语义、控件操作与原采集请求恢复验证。没有本账号当前点选题通过证据。可选外部 AI 模式不能一并算作免费。 |
| 3 | [CSDN：DrissionPage + ddddocr 滑块验证方案](https://blog.csdn.net/qq_27275851/article/details/148473945) | 可阅读的实现示例，识别使用本地 ddddocr。 | 示例留有 `@class=xxx` 控件占位并使用固定尺寸比例，不是可直接接入的托管服务，也不是当前点选题解法。 |

[SadCaptcha 官网](https://sadcaptcha.com/) 当前注明注册赠送 25 API credits，持续使用按额度购买；[公开 Python 仓库](https://github.com/gbiz123/tiktok-captcha-solver) 是该服务的客户端，源码公开不代表识别服务长期免费。本轮没有注册、购买或调用它。

截至本次检索，**尚未找到可核实为长期免费、且已经实测适配当前抖音点选题的托管 API**。这不代表此类项目不存在，而是现有公开证据不足。识别模块可在后端运行，也不意味着网页验证码会话的获取和提交已经可以纯 HTTP 完成。后续优先确认实际点选题的提示和图像，再评估本地算法／模型；分别记录本地图片结果与平台确认恢复，不能拿演示准确率代替本账号通过率。

本次仅更新调研结论和优先级，没有安装替代服务、改变运行配置或开启监控。只读检查确认 8765 服务在线、监控关闭、最近任务没有仍在执行的实例；可以开始小范围监控测试，点选自动识别仍是待完成项。

## 调研初期任务 #31 的确定事实（历史记录）

正式任务 #31，浏览器关键词搜索，2026-09-11 12:24:37 记录 `detected → capturing → needs_review`，原因 `unsupported_challenge_dom`，提交 0、确认恢复 0。当前 `captcha_browser.cjs` 只在顶层页面查找三个旧版控件，任一控件不是唯一可见匹配就退出；还没有调用 ddddocr。这不能证明模型识别失败，也没有确定这次一定是 iframe 题型，需要对实际页面结构继续核对。

## 对比

| 项目 | 实际核对内容 | 本项目取舍 |
| --- | --- | --- |
| [gbiz123/tiktok-captcha-solver](https://github.com/gbiz123/tiktok-captcha-solver) | 仓库存在独立 DouyinPuzzle 选择器和 `solve_douyin_puzzle`；抖音分支进入 iframe 取两张图、计算位移并操作控件。另有 TikTok 不同版本、旋转与点选分支。 | 最值得核对的页面适配参考。完整识别调用 SadCaptcha，非纯本地免费解法；不能把 TikTok 多题型支持直接当作当前抖音所有题型通过。 |
| [sml2h3/ddddocr](https://github.com/sml2h3/ddddocr) | 本地图片识别，提供滑块匹配／差分算法。需要调用方提供图片。 | 已接入，先保留。取图失败时，换识别算法没有作用。 |
| [Hiram-Wong/captcha-bypass](https://github.com/Hiram-Wong/captcha-bypass) | 有本地 CLI／HTTP 服务，图像模块包含滑块匹配、双图旋转等；源码存在 TikTok 双圆图旋转实现。 | 可作本地识别能力的后续候选，仍需要浏览器取图、题型分发和平台结果验证。不是抖音页面自动适配器。 |
| [dengyie/slidex](https://github.com/dengyie/slidex) | 有 CDP、可配置控件及插件结构；当前内置 provider 注册文件只有阿里与极验。 | 未看到内置抖音 provider，不适合作为当前故障的直接替换。 |
| [OSfigitive/yym](https://github.com/OSfigitive/yym) | 旧 Selenium/OpenCV 示例。GitHub 元数据最后推送为 2022-07-14；主文件使用旧页面控件。 | 当前适配正来自这一历史结构，继续照搬不会补足新的页面结构。 |
| [NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/douyin/login.py) | 登录滑块函数仍采用旧背景图和固定 XPath，不能只凭项目持续更新认定该验证码分支已适配当前页面。 | 可作流程研究，不能作为这次问题已解决的证据；不复制其受用途限制的代码。 |
| [Gisnsl/tiktok-captcha-solver](https://github.com/Gisnsl/tiktok-captcha-solver) | 有 HTTP 获取、求解、验证链路，但 `solver.py` 参数针对 TikTok Android、Google Play、app 模式与国际验证域名。 | 不能直接接到当前国内抖音网页搜索验证会话，不作为主选。 |
| [onurkun/puzzle-captcha-resolver](https://github.com/onurkun/puzzle-captcha-resolver) | 较早的图片拼图求解项目；GitHub 元数据最后推送为 2020-01-09。 | 算法参考，不能补齐当前页面适配及会话验证。 |

公开源码核对固定到了这些版本：gbiz123 `fdda3e26b6cd8eb7caab0f6f84c02d9fe6db6ce1`；Hiram-Wong `80a61e09bc88145d49bdc148f8ef55865dc25768`；slidex `5d26af863102becd7a0d7354decf8f9b52f67d46`；yym `33cb8a459f49e1ad1bbbda52a9b161faab6b9bb8`。GitHub 元数据存于 `artifacts/captcha-project-research-20260911/`；元数据中的最近推送日期不等于功能已经实测通过。

主要源码证据：[DouyinPuzzle 定义](https://github.com/gbiz123/tiktok-captcha-solver/blob/fdda3e26b6cd8eb7caab0f6f84c02d9fe6db6ce1/src/tiktok_captcha_solver/selectors.py)、[Playwright 抖音分支](https://github.com/gbiz123/tiktok-captcha-solver/blob/fdda3e26b6cd8eb7caab0f6f84c02d9fe6db6ce1/src/tiktok_captcha_solver/playwrightsolver.py)、[slidex 内置 provider](https://github.com/dengyie/slidex/blob/5d26af863102becd7a0d7354decf8f9b52f67d46/slidex/providers/builtin.py)、[本地滑块实现](https://github.com/Hiram-Wong/captcha-bypass/blob/80a61e09bc88145d49bdc148f8ef55865dc25768/src/captcha/slide.ts)、[本地旋转实现](https://github.com/Hiram-Wong/captcha-bypass/blob/80a61e09bc88145d49bdc148f8ef55865dc25768/src/captcha/rotate.ts)、[TikTok App 参数](https://github.com/Gisnsl/tiktok-captcha-solver/blob/7df641456cb408f1d93a8c64217e3a13da984686/solver.py)。

## CSDN 与外部服务

本次找到可查看原创示例 [DrissionPage + ddddocr 滑块验证方案](https://blog.csdn.net/qq_27275851/article/details/148473945)。它展示监听验证码请求、提供图片给算法、控制滑块的组合，但控件仍留有 `xxx` 占位，图片尺寸比例需要针对页面处理。它是实现思路，不能视作当前抖音可直接部署的项目；文章所说成功情况不是我们账号的通过率。

[SadCaptcha 官网](https://www.sadcaptcha.com/) 提供 REST API、浏览器集成和按次数购买方案，并宣传准确率；这些是供应商声明。本次仅查阅公开资料，没有调用服务，也没有把验证码图片或登录态交给第三方。网站演示成功、图片定位正确或验证码弹窗消失，都不能替代我们原采集请求重新成功的验收。

## 补充筛选与复核

本轮重新搜索了 GitHub 与 CSDN，并复核上述固定版本的抖音 iframe 选择器和 Playwright 分支。补充发现 [justscrapeme/tiktok-captcha-solver](https://github.com/justscrapeme/tiktok-captcha-solver)：公开仓库说明它是外部服务的 Python 客户端，包含图片求解和验证接口；README 当前标注 RapidAPI 暂不可用，需要联系供应商取得私有 API 地址与密钥。公开说明针对 TikTok，未提供足以确认当前国内抖音网页适配的证据，因此不优先于已有明确 Douyin 分支的候选。

另一个搜索结果 [xtekky/TikTok-Captcha-Solver 的 updated.py](https://github.com/xtekky/TikTok-Captcha-Solver/blob/main/updated.py) 使用 TikTok Android User-Agent 和国际验证域名。它可以作为 HTTP 链路的研究材料，不能据此认定现有抖音网页会话能够直接接入。

这次复核没有安装新依赖、调用外部识别服务或执行真实验证码提交。当前推荐依据是源码与本地故障阶段的匹配程度；没有获得新的抖音实测通过率。

## 实施优先级

优先核对当前挑战所在 frame、图片是否加载、控件是否唯一可见，并把失败细分为具体缺失环节。按照实际结构补充独立适配，复用当前账号的浏览器会话和已有 ddddocr。滑块、旋转与点选分别路由；不把未支持题型当滑块处理。保留有界提交与原请求恢复验证，分别记录取图成功率、识别结果、实际提交和平台恢复。只有完成真实样本才报告通过率。

外部 SDK 的代码可见不等于许可无限制；复制代码或打包前单独核实版本许可证。当前只参考结构和公开行为，不引入第三方源码。
