---
name: dashplay-android-install
description: 在现有安卓电视盒子或安卓手机上安装、配置和排查 DashPlay，完成首次 USB/VPN 授权、持久设置、开机自启与电视盒子自动打开，并通过真实断电后的零点击验收。适用于保留 Android 的 APK 路线，不用于刷 Linux/OpenWrt。
---

# DashPlay 安卓安装

目标：首次通过屏幕、遥控器、手机管理页或 ADB 配置一次；以后上电自动启动，按保存的配置连接 iPhone 和借网，电视盒子自动打开主界面。不能把“每次由 AI 点授权、启动 App、拔插线”作为完成安装。

在现有 Android 上工作。ADB、adb root、应用内 su 是三种不同能力；只有 adb root 不代表应用能运行 Root 功能。先用普通应用能力完成安装。刷机、改系统分区、替换桌面或修改系统启动脚本不属于默认安装步骤。

## 先识别设备与安装包

- 核实目标设备、Android/API、ROM、USB Host/数据口、当前网络以及已有 DashPlay 配置。多台 ADB 设备必须用 `adb -s SERIAL` 明确选择；每次重启重新核对，IP 可能变化。
- 优先用电脑已有 ADB；缺失时使用 Android 官方 Platform Tools 的项目内副本，不自行安装全局工具。首次 ADB 连接授权需用户在设备端同意；未授权时改走屏幕/遥控器。
- APK 来自项目实际提供的 Android 发布资产或用户指定的构建。核对签名、版本和 SHA-256；不能把 Linux 发布包当 APK。修复可能尚未进入发布包，不凭版本名称推断已含某项修复。
- 已安装时覆盖升级并保留数据，签名不兼容时先说明并处理迁移，不能直接卸载或清空数据。记录前后配置与授权是否保留，不输出凭据。
- 安装成功必须核实包版本/实际运行，不能只看 `adb install` 退出码。正在运行的 VPN 被替换时，ADB 可能断开，而包已经安装成功。

可用的只读起点（`SERIAL` 换成已核实的设备）：

```sh
adb devices -l
adb -s SERIAL shell getprop ro.product.model
adb -s SERIAL shell getprop ro.build.version.sdk
adb -s SERIAL shell getprop ro.build.fingerprint
adb -s SERIAL shell id
adb -s SERIAL shell command -v su
adb -s SERIAL shell pm path com.dashplay.android
```

## 首次配置：做完后才开始验收

1. 打开 DashPlay，完成向导，电视盒子选择“安卓电视盒子”。保存实际使用的网络和有线/无线方式。热点能力以当前 ROM 实测为准；无系统热点且应用自动热点不支持时，使用外部网络，不能反复强开热点。
2. 身份仅提示：**本软件不附带 CarPlay 身份，请自行导入**。通过应用入口导入用户提供的身份，不在 Skill、报告或日志中复制身份、密码或 token。
3. 连接 iPhone，完成 iPhone 上实际出现的信任/CarPlay 同意。Android USB 弹窗选择“一律/默认使用 DashPlay”并确认；CarPlay 模式切换可能导致 USB 重新枚举，初设时观察是否还有第二次授权。
4. 完成 VPN 授权和所选功能实际需要的权限。保存 USB 借网开关：开启后立即应用，断开后自动重连；下次插手机或上电仍按这个选择工作。手机管理页能改应用设置，不能替 Android 确认系统权限。
5. 开启应用的“开机启动”。电视盒子要求同时验证后台服务和主界面；手机保持后台启动。检查 ROM 自带的自启动/后台运行管理，仅配置 DashPlay 所需条目。
6. 无 HDMI 但已有授权 ADB 时，用 `uiautomator dump` 或截图识别当前窗口与控件后操作；每一步核实结果，不沿用另一分辨率的坐标，不修改输入法设置来消除弹窗。初设的 ADB 点击属于人工配置。

## 开机启动与自动打开

应用的 `BootReceiver` 负责启动接收服务；完成向导的盒子模式还请求打开 `MainActivity`。先验这个应用内路径，不因为有 Root 就写系统文件。

Android 10 起限制后台打开界面，各 ROM 还可能限制自启动。若后台服务正常而前台仍是桌面，继续查系统日志及 ROM 的“开机打开应用”功能，并在首次配置时设置。仅有 ADB 的 `am start` 成功不证明下次开机会自动打开。系统不提供持久方案时，报告该 ROM 的前台自启未通过；需要改默认桌面、设备所有者或 Root 启动机制时，先形成该设备专用、可恢复的具体方案，不抄用其他盒子的系统命令。

Root/ADB 不是免点击承诺：安装后的日常运行不能依赖测试电脑持续连接。加密设备若需要本机首次解锁，明确记录为自动启动限制，不能绕过锁屏或把身份搬到未解锁存储。

参考：[Android 后台启动限制](https://developer.android.com/guide/components/activities/secure-bal)、[Direct Boot](https://developer.android.com/privacy-and-security/direct-boot)。遇到“默认 USB 已选但冷启动仍弹框”，读 [USB 冷启动排查](references/usb-coldboot.md)。

## 零点击验收

初设完成后保持 iPhone 插着，用户真正断电再上电。ADB 仅能只读观察，不能 `am start`、点击、重启服务或补授权。一次性调试点过确认的启动要记为失败；修复后重做完整启动。ADB 整机重启与真实断电测试分别记录。

逐项核实：

- Android 已启动；没有 USB、VPN、运行权限或向导挡住。电视盒子前台确实是 DashPlay，而不只是后台有进程。
- 服务自动启动、`sessionActive=true`，日志持续收到真实视频；有条件时再验浏览器画面、触控和声音。端口可达或 iPhone 显示“已连接”不能独立证明接收成功。
- 借网按保存的开关生效。开启时核对隧道 IPv4、路由及经隧道的实际联网；不要把盒子原有 Wi-Fi 上网当成借网成功。
- USB 重连/开关切换后隧道和读线程不累积。保存开机、授权、服务、CarPlay、借网的耗时；超出观察期限如实标记，不降低验收条件。

常用只读命令：

```sh
adb -s SERIAL shell getprop sys.boot_completed
adb -s SERIAL shell cat /proc/uptime
adb -s SERIAL shell pidof com.dashplay.android
adb -s SERIAL shell dumpsys activity activities
adb -s SERIAL shell ip -o link show
adb -s SERIAL shell ip -o addr show
```

根据真实隧道名用 `ping -I 接口 -c 3 测试地址` 检查借网；受网络策略影响的失败需要结合路由/收发日志解释。HTTPS 状态接口为应用实际地址的 `/api/status`，管理入口 `/manage`。完整系统日志、偏好 XML 和管理 URL 可能含设备标识或凭据，保留私有，仅报告必要字段。

还要分别验：先开盒子后插手机、网络晚到、覆盖升级保留设置后的启动，以及借网开关立即生效后无需拔线的恢复。尚未完成的组合列为待验，不能用一次成功代替全部。交付说明设备/ROM/APK、完成的首次设置、已通过与未通过的组合及日常使用方式。
