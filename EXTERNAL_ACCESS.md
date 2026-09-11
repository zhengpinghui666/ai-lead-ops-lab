# 外网访问：当前工作台直接展示

2026-09-11 首屏性能接续：针对用户反馈已压缩电脑到网关的响应，583,514 字节状态降为 56,087 字节（Base64 前），没有删除字段或缓存业务数据。公网同样接口由一次 5.515 秒变为三次 2.719–3.828 秒；样本与网络边界见 ACCESS_PERFORMANCE.md。页面提示改为“正在连接工作台”。当前网关版本为 `f5f39eba-8032-49a7-9cf1-4e4a9b4f4bc9`，旧版本记载保留为历史。

2026-09-11 接续：用户回答“可以打开工作台”，未注明蜂窝或 Wi-Fi，不据此标记两种网络分别验收。恢复本机服务后，新增 `/api/live-history` 已加入本机与 Worker 白名单；网关版本 `484618f6-8007-44f9-86f4-2d24581a47fa`。新版手机界面和直播候选仍通过同一固定入口提供，不增加登录。新一轮资源字节及历史接口核对见 `artifacts/successor-live-20260911/validation-summary.json`。

2026-09-11：按用户最新要求，不增加工作台登录，不修改现有页面。固定入口为 https://clubops-team-4ff187.pages.dev/#monitor 。此选择取代此前“内部人员登录后使用”的方案。

已部署独立的 Pages 入口和 `clubops-team-gateway`，与短信中转分开。Pages 直接绑定 SQLite Durable Object；本机主动建立 TLS WebSocket，把请求转发给固定 `127.0.0.1:8765`。访客无需处于同一网络，也无需安装客户端。电脑须保持开机、联网并运行 ClubOps；停止服务时外网返回电脑离线，不展示伪造缓存数据。

首页、app.js、app.css 通过外网取得的字节与本机完全一致；实际 Chromium 打开监控页，返回 25 条评论、总计 265 条去重观察，未出现网页异常或额外登录。此数目包括过滤观察，不等同正式库的 70 条评论。公网接口已返回 10 个作品和 70 条正式评论。手机蜂窝／Wi-Fi 对此新网址的单独验收仍待用户确认，之前短信入口的成功不代替此次结果。

电脑连接凭证独立生成，用 DPAPI 存于 `data/private/team-access.dpapi`，云端只配置 `CONNECTOR_TOKEN` secret，不复用手机、抖音或模型凭证。外网无网站登录；保留应用现有来源与 CSRF 检查，连接器只转发允许的业务路径，不提供任意主机代理、文件读取或远程关闭服务。请求和响应只在内存转发，云端不保存业务数据库。连接中断不重复提交已有操作，响应不明确时需核对记录。

部署脚本：`scripts/deploy-team-access.py`。依赖使用既有锁定 Wrangler 4.130.0 和 Miniflare；Python 连接依赖 `config/team-access-requirements.txt`，安装到项目 `.tools/team-access-libs`，不修改 Codex 自带 Python。服务自动加载已启用的配对连接，正常关闭时一并断开。

本次 Pages 项目创建被 Wrangler 自动转向 Workers，导致 Pages 项目不存在；随后使用该版本明确支持的 `pages project create --force` 创建 Pages，正常发布。此选项用于选择 Pages 产品，不是权限或安全检查绕过。首次失败记录保留。实际部署与公网检查见 `artifacts/team-access-20260911/`，不得以预览域名代替固定生产网址。

当前使用免费档，没有升级付费。免费额度不是无限容量；现有页面轮询会计入请求额度，多人长期同时打开的容量尚未压测。调研及官方额度来源见 `research/EXTERNAL_ACCESS_20260911.md`。
