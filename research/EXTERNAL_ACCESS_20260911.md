# 免费外网访问方案核对

核对日期：2026-09-11。目标为现有 Windows/Python/SQLite/浏览器工作台跨网络访问；用户最终指定无需新登录，直接展示当前页面。不能仅上传静态 HTML 并声称本机后端已部署。

| 方案 | 本项目的适用性 |
| --- | --- |
| Cloudflare Pages＋自建有界连接器 | 已采用。已有账号，Pages 默认 HTTPS 域名，不需购买域名；本机向外连接，保留现有 Python 和加密会话。是本项目实现的转发服务，不是 Pages 自带完整 Python 托管。 |
| Cloudflare Named Tunnel | 现成、维护成熟；公开应用通常需要托管在 Cloudflare 的域名。当前用户没有自有域名，不能把现有 pages.dev 当成自有 DNS 区域使用。 |
| Cloudflare Quick Tunnel | 免费临时测试入口，随机域名、无 SLA，官方不建议作为生产方案；不满足稳定官网地址的长期目标。 |
| frp／rathole | 客户端和服务端开源；通常仍要有公网服务器。软件免费不等于服务器免费。 |
| ngrok 免费档 | 有开发域名，但当前免费限制为月度 20,000 HTTP 请求及 1 GB 出站。现有工作台仅一路 2.5 秒轮询就约 34,560 次／天，不适合持续监控多人看板。 |
| Tailscale Funnel | 可向普通浏览器公开 HTTPS；当前 Personal 免费档限非商业用途，不能直接作为商业内部工作台永久免费方案。 |
| OpenFrp 免费节点 | 可作为备选，需账号和实际节点可用性核对。官方文档列出大陆节点实名、HTTP(S) 自有域名等条件；没有实测该用户网络与节点持续性，未采用。 |

主要来源：[Cloudflare Tunnel 配置](https://developers.cloudflare.com/tunnel/setup/)、[Quick Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)、[frp](https://github.com/fatedier/frp)、[rathole](https://github.com/rathole-org/rathole)、[ngrok 免费档限制](https://ngrok.com/docs/pricing-limits/free-plan-limits)、[Tailscale 定价](https://tailscale.com/pricing)、[OpenFrp 文档](https://docs.openfrp.net/use/configuration/bt)。上表的轮询数量为按当前代码频率计算，不是压测结果。

本次实现中，Pages 直接绑定独立 Worker 的 Durable Object，减少额外 Worker 转发；电脑作为 WebSocket 客户端向外连接。DO 使用 Hibernation API 接收连接，空闲可休眠；有请求在途时保存有界内存状态，返回后释放，不把 HTTP 内容写入云端数据库。[Pages 绑定说明](https://developers.cloudflare.com/pages/functions/bindings/)、[DO WebSocket 说明](https://developers.cloudflare.com/durable-objects/best-practices/websockets/)。

Cloudflare 当前 Pages Functions 与 Workers 免费请求额度共享 100,000 次／天。SQLite DO 免费档另有 100,000 请求／天、13,000 GB-s／天和存储读写限制；超过免费额度会报错，不能据此承诺无限访问或永久稳定。尚未测试多人同时持续访问的容量。[Pages Functions 计费](https://developers.cloudflare.com/pages/functions/pricing/)、[DO 计费](https://developers.cloudflare.com/durable-objects/platform/pricing/)。

验证范围：真实云端部署、电脑出站连接、公网数据读取及实际浏览器渲染已经通过；新网址手机直连仍待用户确认。当前采用的是免费额度内的固定访问入口，电脑服务依然是运行依赖。
