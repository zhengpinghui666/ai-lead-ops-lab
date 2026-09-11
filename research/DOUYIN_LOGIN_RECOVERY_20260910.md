# 抖音登录恢复：GitHub 调研与本机方案

调研日期：2026-09-10。对象：普通个人抖音号、Windows 后端采集，验证码接收设备为用户自己的 iPhone。以下是源码／官方文档审查，不是这些项目在本账号上的通过率测试。

**建议采用：专用浏览器登录态复用 + iPhone 快捷指令转发本次登录短信 + 本地登录任务自动填写与独立身份复核。** 日常读取继续走 HTTP；需要重新登录时，由认证浏览器完成正常登录。短信转发可以减少人工抄码，但不能保证平台永远不要求其他验证。

## 现成项目做了什么

| 项目 | 核对的实现 | 对本项目的价值与边界 |
|---|---|---|
| [MediaCrawler 登录代码](https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/douyin/login.py) | 支持 Cookie、扫码、手机号登录；手机登录轮询缓存中的验证码，取得后填入浏览器 | 可参考“等待验证码→填写→复核”流程；不会自行生成验证码，也不是免短信登录 |
| [MediaCrawler 短信接收端](https://github.com/NanmiCoder/MediaCrawler/blob/main/recv_sms.py) | HTTP 接收手机转发内容，提取六位数字，暂存到按平台和号码索引的缓存 | 证明源码有短信接入结构；仍需手机提供短信，且原样运行存在缓存共享问题，见下文 |
| [F2 FAQ](https://github.com/Johnserf-Seed/f2/blob/main/docs/faq.md)／[发布记录](https://github.com/Johnserf-Seed/f2/releases) | 建议从已登录浏览器提取 Cookie；发布记录包含弃用抖音 SSO 扫码登录 | Cookie 提取适合复用已有会话；旧协议扫码实现不适合当作长期稳定的自动登录保障 |
| [douyin-DL-skills](https://github.com/amibaren/douyin-DL-skills#cookie-管理所有使用方式共享) | 共享本地 Cookie 文件，失效后通过可见浏览器扫码刷新 | `refresh-cookie` 包含重新获取 Cookie 的人工登录过程，不能据此理解成服务器自动续期 |
| [forwarder-sms](https://github.com/lengmuning/forwarder-sms#ios-快捷指令) | iOS 快捷指令将短信通过 JSON POST 提交；服务端提供 Bearer 认证、提码、去重及消息推送 | 最贴合 iPhone→HTTP 这段。我们只需要传入自己的登录任务，不需要它的群机器人转发通道 |
| [sms_automation](https://github.com/Thesaru-p/sms_automation#connect-iphone-shortcuts) | iPhone“信息”自动化立即运行，将快捷指令输入 POST 给 Flask | 可参考手机配置方式；它实际处理水电账单并再次发短信，不能直接当成抖音登录模块 |

MediaCrawler 当前读取到的 `login.py` 和 `recv_sms.py` 都调用 `CacheFactory.create_cache(config.CACHE_TYPE_MEMORY)`，工厂为 memory 创建新的 `ExpiringLocalCache`，实例保存各自的字典。因此，**接收端和登录端原样作为两个进程启动时，内存不会自动共享**。代码里的 Redis 注释不能代替实际配置。接入时应使用同一个登录任务服务管理短期验证码，或明确配置共享缓存。依据：[工厂](https://github.com/NanmiCoder/MediaCrawler/blob/main/cache/cache_factory.py)、[内存缓存](https://github.com/NanmiCoder/MediaCrawler/blob/main/cache/local_cache.py)。这是源码推断，未对该项目做手机实测。

`sms_automation` 的 [server.py](https://github.com/Thesaru-p/sms_automation/blob/main/server.py) 还会输出收到的短信全文；本项目的验证码接入不应照搬这一行为。MediaCrawler 文件标注非商业学习许可，本次仅参考流程，不复制源码进 ClubOps。

## iPhone 路线的依据

Apple 官方说明，“信息”自动化可按发件人或短信包含的文字触发，并可以设为无需逐次询问运行；“获取 URL 的内容”支持 POST 和 JSON 请求体。这几项能力组合支持手机主动向自己的接收端提交短信，而不需要 Windows 读取 iPhone 整个短信库。依据：[通信触发](https://support.apple.com/zh-cn/guide/shortcuts/apdd711f9dff/ios)、[自动运行设置](https://support.apple.com/zh-cn/guide/shortcuts/apd602971e63/ios)、[网络 API 操作](https://support.apple.com/zh-cn/guide/shortcuts/apd58d46713f/ios)。

用户还没有在这台 iPhone 上配置或测试该自动化。锁屏、重启后首次解锁、手机离开电脑所在网络、请求权限和短信实际格式必须实测；现阶段不报告成功率。

安卓的 [SmsForwarder](https://github.com/pppscn/SmsForwarder) 和 [SMS Gateway for Android](https://github.com/capcom6/android-sms-gateway) 支持短信转发／Webhook，但不适用于当前 iPhone，因此不作为首选，也无需为这个方案换手机。

## 拟接入 ClubOps 的具体流程

1. 常规采集复用项目专用浏览器配置与 DPAPI 加密会话。HTTP 身份探针核对账号，不能仅判断 Cookie 文件存在或页面显示昵称。
2. 登录失效时保留采集进度，暂停需要该账号的请求，创建唯一且有时限的登录任务，打开同一专用认证窗口。
3. iPhone 的“信息”自动化只匹配抖音登录短信，设为立即运行。优先在手机提取本次验证码，通过 HTTPS 或受保护的私有网络发给自己的接收端。
4. 接收端校验专用认证令牌、设备、当前登录任务及有效时间；不接受没有待处理登录任务的验证码，不将正文或验证码写入常规日志，不重复消费。
5. 认证窗口填写一次验证码；登录成功后重新获取 URL 范围内的 Cookie，并通过独立 HTTP 身份检查确认仍为目标账号，再保存加密会话。
6. 验证成功后从已有断点恢复；短信超时、登录失败或出现其他挑战则保留状态并通知用户，不循环申请短信。

这是一份接入设计，**短信 Webhook、iPhone 快捷指令和自动填码尚未实现或实机验收**。`data/browser-profile`、加密会话、身份检查及人工完成后的恢复已有实现。本次还修正了显式一键登录触发短信时认证页过早关闭的问题，并增加了针对匿名身份响应的等待测试。

当前服务绑定电脑的 `127.0.0.1:8765`，手机不能用该地址访问电脑。正式接入需要单独、受认证保护的可达入口：同一局域网测试可部署专用接收端，跨网络持续运行则使用自己的 HTTPS 入口或私有网络。研究阶段未修改监听地址、开放防火墙或部署公网端点。

## 与本次真实故障的关系

此前独立身份请求返回 HTTP 200，但业务码为 8、没有用户对象；随后专用窗口出现登录提示，一键登录触发了短信。用户输入验证码后，身份校验恢复成功，后续 HTTP 评论读取成功。因此本次卡点确实包含登录态失效／重新认证；仅刷新页面或改成本地 HTTP 请求不能保证消除它。尚未查明平台撤销旧登录态的具体原因，不将其归因于某个浏览器、IP 或固定过期时长。

恢复登录后，任务 21 读取到 12 条有文字评论，新增 5 条、重复 7 条，并自动完成 5 项 API 分析。另有 1 条无文字表情评论被跳过，触发了已修复的计数和状态问题。关键词搜索任务 20 仍返回 `verify_check`：**短信登录成功与搜索挑战解除是两个独立结果**，这份登录方案不能用来宣称搜索验证或私信发送已全部解决。

如果仍要求完全不依赖手机，又遇到平台强制短信认证，本次查到的项目没有提供经验证可用的普通个人号免短信方案。对现有 iPhone 与 Windows 组合，登录态复用配合手机自动转发，是目前最值得做小范围验证的实现方向。
