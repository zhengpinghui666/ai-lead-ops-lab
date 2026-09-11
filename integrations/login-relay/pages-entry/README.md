# 可选的 Pages HTTPS 入口

2026-09-11 锁屏自动转发实测通过一轮：任务 `701afda582244c08a0812aca6ec826f2` 在电脑端为 `phone_tested`，测试码已取回并核对正确；用户随后确认接收 iPhone 全程锁屏、由短信自动触发，未手动运行 Scriptable。手机锁屏与触发方式依据用户确认，电脑取回依据后台记录；本轮网络类型未记录。使用普通测试短信，未请求真实抖音登录短信。真实验证码同页填写、独立身份核对和原任务恢复仍待自然需要登录时验收，自动恢复开关保持关闭。证据：`artifacts/iphone-forwarder-20260910/lockscreen-test-701afda582244c08a0812aca6ec826f2.json`。

2026-09-10：用户实测手机蜂窝网络无法直连现有 `workers.dev` 下载地址，开启 VPN 后才可访问。此前 Scriptable 在 Wi-Fi 下完成了鉴权配对；首次手机合成提交未成功，电脑任务超时。

本目录已部署一个同账号 Pages 入口 `clubops-login-4ff187`，通过固定 `RELAY` Service binding 调用既有 `clubops-login-relay` Worker。短信存储、180 秒期限、角色鉴权与一次取回仍由原 Worker 负责。入口不保存 secrets，不记录请求内容，不接受动态上游，也不把手机重定向回 `workers.dev`。

## 当前状态

- 本地路由隔离与真实 workerd Service binding 两项测试通过，包括鉴权分离、重复提交、跨入口一次取回与取消。
- 用户已明确允许的 Pages 部署授权已完成，保留原权限，只追加 `pages:write`。入口已部署到 `https://clubops-login-4ff187.pages.dev`，生产版本 `595a2dac-1589-4af2-be41-6caf2750b007`，配置的 Service binding 指向原 Worker。
- 电脑不使用项目代理直连 `/connection-check` 成功，约 1453 毫秒；12 项有鉴权的真实 HTTPS 检查通过，包括从新入口提交、在原 Worker 取回，以及跨入口拒绝第二次取回。用户已确认关闭 VPN 后蜂窝网络与 Wi-Fi 均可打开检测页。这是当前设备的网络结果，不承诺全国网络或长期可用。
- 2026-09-11：电脑配对已受控迁移，iPhone 已重新配置；任务 `8a4fe1d34e604413b0fbd4180270415a` 完成手机提交、电脑取回和核对。该往返的网络类型未记录；两种网络公开入口直连获用户确认。锁屏短信自动触发已通过一轮，真实登录恢复仍待验收，不能据此推断长期可用。

## 部署与验收

在本目录使用上一级锁定的 Wrangler 4.130.0。已有其他授权保留，只追加 `pages:write`；不申请全部默认权限。

1. Pages 项目 `clubops-login-4ff187` 已创建，生产分支名 `production`。首次创建使用 `--force` 保持 Pages，以获得不同的默认域名；项目已存在后直接部署，不再重复创建或加 `--force`。
2. 部署 `public/`，配置中的 `RELAY` 指向同账号现有 Worker。不要把项目根目录、业务数据或手机配置当作上传目录。
3. 部署生产域名为 `https://clubops-login-4ff187.pages.dev`。匿名 GET `/connection-check` 只说明入口可访问，不代表中转鉴权或手机收发成功。匿名 `/v1/pending` 为 401，已验证。
4. 电脑使用禁用代理继承的独立 HTTPS 客户端做只读检查；手机关闭 VPN，分别用蜂窝网络与 Wi-Fi 打开实际生产域名的 `/connection-check`。
5. 两种手机网络都可达后，验证入口调用原 Worker 的角色鉴权与合成提交／取回。不得为了测试而清除有效抖音会话或请求真实短信。
6. 只有确认新入口属于本次部署的同一个 Worker，才允许迁移本机加密配对并让手机重新配置。普通 `login_relay.bind` 仍拒绝跨地址重用凭证。新增的维护工具 `scripts/migrate-login-relay.py` 已通过隔离检查，正式迁移已执行，后端直连检查、iPhone 重新配置及一次手动合成往返均已通过；用法见根目录 `LOGIN_RECOVERY.md`。

参考：[Service bindings](https://developers.cloudflare.com/pages/functions/bindings/#service-bindings)、[Pages 免费额度](https://developers.cloudflare.com/pages/functions/pricing/)。若默认域名仍不能直连，使用用户自有域名或可访问地区的中转服务并重新实测；[Worker 自定义域名](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/)需要用户拥有的有效 Cloudflare zone，不能承诺换域名后必然可达。
