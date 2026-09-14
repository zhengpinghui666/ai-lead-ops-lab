# 手机锁屏通知

1. iPhone 安装 Bark 并允许通知（含锁定屏幕）；在 Bark 首页复制默认服务器推送地址。
2. 打开 ClubOps 账号登录→手机通知，粘贴地址、保存配置。
3. 锁屏后在电脑发送测试，手机实际收到后点击电脑“我已收到”。

确认前不自动发故障提醒。确认后，后台处理被标记为 needs_user 的有效故障会通知手机；同一故障不重复发送。临时网络结果不明保留 unknown，避免重复推送。配置仅当前Windows用户DPAPI解密，地址不回显、不进源码备份。默认Bark官方服务器，HTTPS POST /push；通知只有固定渠道和人工处理说明。

当前通知尚未包含手机验证码操作链接：原挑战保留与远程人工输入流程独立开发中。Bark服务端接受不代表手机收到；专注模式、通知权限和网络需真实锁屏测试。

参考：[Bark](https://github.com/Finb/Bark)、[API V2](https://github.com/Finb/bark-server/blob/master/docs/API_V2.md)、[Scriptable Notification](https://docs.scriptable.app/notification/)。
