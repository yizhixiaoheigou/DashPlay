# DashPlay

[English](README.en.md)

把带 Wi-Fi 和蓝牙的 Linux 小盒子变成 CarPlay 接收端：iPhone 用数据线插盒子（有线 CarPlay），或经蓝牙引导连上 Wi-Fi（无线 CarPlay）；车机或任何设备用浏览器打开盒子的网页，就能显示画面、播放声音并触控操作。没有 CarPlay 的车，只要车机有浏览器、能连 WiFi，就可以试。

DashPlay 基于 [PlayPort](https://github.com/youcci/playport)，提供补丁和配套运行程序：有线 CarPlay（盒子作 USB 主机）、Linux BlueZ 蓝牙桥接、面向车机浏览器的精简界面与系统媒体控制、首次设置与设置页、开机自启与监管等。

> **这不是 Apple 认证产品。** 运行需要你自己提供配件认证身份，见下文“认证身份”。刷机有风险，操作不当可能变砖。

## 怎么装

**不懂技术也能装**：把下面这句话发给你的 AI 助手（Claude Code、Codex、WorkBuddy、豆包、DeepSeek 等，能在电脑上运行命令的都行）。它会一个个问你：什么设备、现在是什么系统、什么车、车机能不能开热点、要不要上网，然后一步步装好，只在需要你插拔、在手机上点选时叫你：

> 请在终端用 curl 下载 https://raw.githubusercontent.com/yizhixiaoheigou/DashPlay/main/skills/dashplay-install/SKILL.md 的原文并完整读完（不要用网页摘要工具，它可能给出旧版或删节版），再按它一步步帮我把 DashPlay 装好。

N1 是斐讯原厂系统、安卓电视或开不了机，都直接告诉它。N1 实测的是已刷 Linux 的路线；两条原厂路线（2.19 及以下、高于 2.19）来自公开实践，本项目尚未实测。

## 选哪台

| | ZTE F50 插卡版 | 斐讯 N1 |
|---|---|---|
| 价格（参考） | 约 400–500 元（含散热器），SIM 卡流量费另算（只有车机自带应用上网用） | 盒子约 100 元，另配 5 V 升 12 V 升压线 |
| 车机上网 | 插卡就有网，车机自带应用都能用 | 盒子自己没网：车机能开热点就连车机；不能就用随身 WiFi、连一个 WiFi 热点，或 USB 共享网络 |
| 不上网也能用 | 能（不插卡；车机自带应用不能联网，待实车验证） | 能（实车通过） |
| iPhone | 只有无线（F50 的 USB 口不能插 iPhone） | 有线、无线都行，见下表 |
| 要准备 | 8 GB 以上可清空的 SD 卡、能传数据的 USB 线、散热器；要车机上网再加能上网的 SIM 卡 | 8 GB 以上可清空的 U 盘、一根网线（电脑连盒子 WiFi 时靠网线上网）；原厂机另要显示器、USB 鼠标，高于 2.19 还要双公头 USB 线 |
| 电脑 | Mac（Apple 芯片）或 64 位 Windows 10/11，空闲 15 GB | Mac / Windows / Linux |
| 安装 | AI 自己装，约 25 分钟（实测） | 约 30–60 分钟 |

玩客云和 N1 跑同一套程序、连法相同，另要 RTL8821CU USB 网卡（WiFi + 蓝牙二合一）和 12 V 电源。其他已装 OpenWrt 25.12 的路由器用姊妹仓库 [openwrt-dashplay](https://github.com/yizhixiaoheigou/openwrt-dashplay) 的插件。所有方案都还需要一部 iPhone。

## 在车上怎么连

**F50**：车机和 iPhone 连 F50 的热点，iPhone 走无线。插卡车机就有网；不插卡也能用，只是车机自带应用不能联网。

**N1 / 玩客云**：首次设置时按车机情况选：

| 情况 | 怎么连 | 车机自带应用能上网吗 | 有线 | 无线 |
|---|---|---|---|---|
| ① 车机能开热点（方式一） | 盒子连车机热点 | 能 | ✅ | N1：✅ iPhone 直连盒子热点（车机热点要 5 GHz）；其他盒子看网卡 |
| ② 有随身 WiFi（方式二） | 盒子、车机、iPhone 都连随身 WiFi | 能，走随身 WiFi | ✅ | ✅ |
| ③ 车机只能连 WiFi（方式三） | 车机、iPhone 连盒子热点 `DashPlay` | 不能 | ✅ | ✅ |
| ③ + 连接 WiFi 热点 | 盒子连手机热点或随身 WiFi，同时开 `DashPlay` 给车机 | 能，车机自带应用走它的流量 | ✅ | ✅ |
| ③ + USB 共享网络 | 手机或随身 WiFi 用数据线插盒子（iPhone 开个人热点、安卓开 USB 网络共享），盒子开 `DashPlay` 给车机 | 能 | ✅ | ✅ |

- ① 的无线：iPhone 不连车机热点（画面要经它中转，小鹏实测只有约 0.1 Mbit/s），而是直连盒子同时开的自己的热点。盒子网卡要能同时连热点又开热点（N1 用程序自动启用的新无线固件；玩客云未实测），车机热点要在 5 GHz 的 36–48 或 149–165 信道；2.4 GHz（实测延迟 9–12 秒）或盒子开不了热点的信道时自动改用有线，设置页写明是哪个信道。N1 在家实测 13 分钟零断流，未上车。可选「iPhone 用车机流量上网」，默认关。
- 连接 WiFi 热点时热点要一直开着；找不到时自动退回 ③。开热点的可以就是跑 CarPlay 的那部 iPhone。
- iPhone 开热点时关掉「最大兼容性」（热点走 5 GHz，画面才不卡）；只有盒子网卡只支持 2.4 GHz 时才需要打开它。
- USB 共享网络和 CarPlay 走有线还是无线无关：iPhone 可以用同一根线既做有线 CarPlay 又给盒子上网，也可以线只上网、CarPlay 走无线；安卓手机或随身 WiFi 插另一个 USB 口。
- 选了 USB 共享网络时，USB 优先：拔掉数据线后盒子自动改连设置里填过的热点，热点也不在才暂时不上网；插回数据线再切回 USB。
- 流量：CarPlay 的画面和声音只在盒子与 iPhone 之间传，不经外网、不耗流量。在 CarPlay 里听音乐、导航时，iPhone 照常用自己的流量，和平时用手机一样。连 WiFi 热点、USB 共享网络（或 F50 插卡）后多出的流量，只来自车机自带的应用（如特斯拉的音乐、地图）上网。
- 有线、无线都能用时：插线默认走有线。CarPlay 画面里「盒子设置」每行是「有线 · 手机名」或「无线 · 手机名」，点哪行切到哪行（同一部手机有线↔无线、这部↔那部都行），不用改网络设置；线不做 CarPlay 时仍充电、仍可给盒子上网。拔线再插回到有线。设置页可选「只用无线」，线就永远不做 CarPlay。无线要先在设置页「添加 iPhone」蓝牙配对一次，以后自动连。
- ② 的随身 WiFi 如果开了设备隔离，无线会卡或连不上，只能有线。

**车机打不开画面或连不上时**，按现象处理，不分车型（设置页「车机打不开画面时」分组，步骤见 [AI 安装说明](docs/DashPlay-AI安装说明.md) 第 4 节）：

- 车机不肯连没网的 WiFi：盒子替它回答联网检查，CarPlay 照常用，连着时车机自带应用不能联网；要联网就插卡（F50），或 N1 / 玩客云连 WiFi 热点、用 USB 共享网络。
- 浏览器不让打开局域网地址，或证书警告没有「继续」：用自己的网址（免费 DuckDNS + 正式证书，AI 会带你注册）。
- 出 P 挡就断：在车机的 WiFi 设置里打开「行驶中保持连接」之类的选项。
- 搜不到盒子的 5 GHz 热点：F50 / OpenWrt 在后台把信道改成 149，还不行改 2.4 GHz 信道 6（N1 / 玩客云默认就是 149）。

特斯拉这几条都会遇到。装的时候告诉 AI 是特斯拉，它会打开「特斯拉模式」（一次处理前两条）并带你设好网址。

## 硬件与实测

### 硬件

- **玩客云（S805，Armbian / Debian 13 armhf）+ RTL8821CU USB 网卡**（Wi-Fi 与蓝牙二合一，`0bda:c820`），12 V 供电。有线 CarPlay 用远离网口的 USB 口（OTG，盒子切成 USB 主机）。线刷固件后手机连盒子热点 `DashPlay` 完成首次设置，由用户自己的 AI 助手放入认证文件即可使用。刷机与使用见 [固件使用说明](docs/DashPlay玩客云固件使用说明.md)，给 AI 的步骤见 [AI 安装说明](docs/DashPlay-AI安装说明.md)；固件构建工具见 `scripts/onecloud/firmware/`。
- **斐讯 N1（S905D，aarch64，内置 BCM43455 Wi-Fi/蓝牙）**：刷机包是预装 DashPlay 的 U 盘镜像（基于 [ophub amlogic-s9xxx-armbian](https://github.com/ophub/amlogic-s9xxx-armbian) 的 Debian trixie），U 盘启动后可用 `armbian-install` 装进 eMMC；步骤同见 [固件使用说明](docs/DashPlay玩客云固件使用说明.md)。两个 USB 口都是主机口，iPhone 插哪个都行。镜像由 `scripts/onecloud/firmware/make-n1-image.sh` 在一台 N1 上生成。
- **已经装好 OpenWrt 25.12 的路由器或随身 WiFi（aarch64 / x86_64，含 ZTE F50）**：OpenWrt 插件在姊妹仓库 [openwrt-dashplay](https://github.com/yizhixiaoheigou/openwrt-dashplay)（apk 包与软件源），网络照常在 OpenWrt 后台设置。插件运行的就是本仓库的程序：每次发布附带的 `dashplay-bundle-<版本>.tar`。原厂 F50 B16 的备份、Root 与 SD 卡 OpenWrt 路线见 [装机 skill](skills/dashplay-install/SKILL.md)，包含经校验的辅助脚本；会降低启动校验，最坏需要拆机。非 B16 没有已核实降级路线时必须停止。


### 实测情况

**实车**

- 小鹏，玩客云 ① 有线：首次设置、连续约 27 分钟、断电重开、切换画面后 8–12 秒恢复，30–50 帧、0 丢帧。
- 特斯拉，F50 插卡（无线）；N1 ③ 不上网（有线、无线都出画，重启、忘记 WiFi 后重新添加也通过）与 ③ + 连手机热点（有线）。
- F50 从原厂 B16 按装机 skill 装到特斯拉模式，Mac、Windows 11 各走通一次。

**在家**

- 玩客云三种方式各从恢复出厂走完首次设置（用手机热点代替车机热点和随身 WiFi），插拔线有线/无线互切、断电重启、重置。
- N1 ① 的画面、点按、声音、封面；N1 借同一部 iPhone 的热点时，无线 CarPlay 约 30 帧。
- F50 通电约 37–44 秒进入 CarPlay；iPhone 离开 WiFi 后不再邀请，连回约 4 秒恢复；重启后自动邀请上次那部。
- 2026-10-31（N1，本版新功能）：有线↔无线按行切换约 3–8 秒；拔掉只供网的数据线，无线 CarPlay 不断；USB 共享网络下 iPhone 与安卓可同时供网；USB 取下后自动改连已保存热点，插回自动切回；冷启动（USB 共享 + 无线）约 45 秒，USB 供网压测时画面丢 3 帧。

**声音**：有线音乐为无损 48 kHz（网页以 FLAC 交给媒体元素播放），专辑信息与封面进入系统媒体控制，方向盘可切歌；多部 iPhone 轮流使用（无线）。

**还没实车验证**：F50 不插卡；② 上车；连手机热点时的无线；USB 共享网络给车机上网、车里有线↔无线切换（只在家用 N1 测过）；斐讯原厂 N1 刷机路线。

### 换其他 Linux 盒子

需要：Wi-Fi 客户端（方式一、二）或热点（方式三，hostapd）；经典蓝牙（BlueZ，仅无线 CarPlay 需要）；一个 USB 主机口（有线 CarPlay）；Java 21（Debian trixie / Ubuntu noble）。`scripts/onecloud/platform_api.py` 是平台接口，`platform_debian.py` 是 Debian/Armbian 实现；`scripts/onecloud/board-probe.sh` 只读检查新板子的这些条件。

## 认证身份

iPhone 只会连接出示有效配件证书、并能用对应私钥签名的接收端。DashPlay 与 PlayPort 一样**不附带也不分发任何身份**。仓库、安装包、文档和测试都不包含真实身份文件；测试所需身份只在运行时生成合成数据。本包的本地认证模式缺少以下两个文件时接收服务拒绝启动：

```
identity/offline-mfi/identity.pk8       # PKCS#8 EC P-256 私钥
identity/offline-mfi/certificate.p7b    # 对应的配件证书
```

最省事的来源是 [DiPlay](https://github.com/shihabal3amri/DiPlay/releases) 发布的安卓安装包：取发布列表里最新一个 `DiPlay-*.apk`，包里 `assets/offline-mfi/` 下就是这两个文件。步骤（含核对私钥与证书配对）见 [AI 安装说明](docs/DashPlay-AI安装说明.md) 第 2 节，交给你的 AI 助手即可代办。其他途径见 [PlayPort README 的 Accessory identity 一节](https://github.com/youcci/playport#accessory-identity-required)：Carlinkit 固件、你自己作为 MFi 授权方拥有的身份，以及 `--mfi-server` 远程认证服务（上游提供；DashPlay 的启动脚本使用上述本地文件）。DashPlay 程序本身不附带、不自动下载身份。请注意：

- 公开可得的身份是共享的实验身份，并非签发给本项目或 PlayPort，Apple 随时可能使其失效；
- 只用于个人实验，不要出售，不要再分发身份文件，不要提交到任何仓库；
- 身份文件应只保存在设备私有目录、仅 root 可读。

## 构建

需要 JDK 25（构建）、Node.js（网页）和 Git。

```sh
git clone https://github.com/youcci/playport.git
git -C playport checkout 9a0882dd0ffe48e467b59d58b12d81391df55ade
sh scripts/apply-playport-patches.sh playport
(cd playport && ./gradlew :protocol:test :server:test :server:installDist)
(cd playport/web && npm ci && npm test && npm run build)   # FLAC 检查需要 ffmpeg，没有则跳过
sh scripts/onecloud/firmware/build-bundle.sh dashplay-bundle.tar   # 程序包（盒子安装与 OpenWrt 包都用它）
```

`scripts/build-playport.sh` 可在隔离 Docker 容器中完成同样的 Gradle 构建与测试。盒子上运行需要 Java 21 和带 asyncio 的 Python 3：玩客云/N1 由 `install.sh` 从 Debian 安装，OpenWrt 由插件的 `dashplay-jre`（Temurin 21 musl 版）与系统软件源提供。

## 运行（玩客云 / 斐讯 N1）

- `scripts/onecloud/dashplay.service` 启动 `supervisor.py`（安装在 `/opt/dashplay`）：按所选使用方式联网（连车机热点或随身 WiFi、或开盒子热点；连不上时临时开盒子热点进设置）、蓝牙、接收服务与设置页（80 端口，`http://dashplay.local/`）。
- `setup_wizard.py`、`settings_page.py`、`settings_ui.py`：首次设置（使用方式 → 网络 → iPhone）与盒子设置页；`phone-presence.py`、`pairing_window.py`、`pair-phone.py`：多部 iPhone 的添加与轮换（无线）。
- `linux/bt-bridge/`：PlayPort 的 Linux 蓝牙桥，通过 BlueZ D-Bus 建立 iAP2 RFCOMM；`BT_BRIDGE_READY_WAIT` 允许在蓝牙晚于接收服务就绪时等待。
- `iphone-usb-bridge.py`：有线 CarPlay 的 USB 侧——把 iPhone 切到 CarPlay 配置、转发 USBMUX、在用户态桥接 NCM 网卡；iPhone 第一次插上时点「信任」，信任记录按 iPhone 分别保存。
- `run-receiver.py` 生成接收配置并通过 `launcher/onecloud/Launcher.java` 启动 PlayPort；`diagnostics.py` 每 10 秒把网卡、温度、播放统计写入 `state/diag/`，每次开机的完整日志存在 `state/logs/`（断电不丢，设置页可下载诊断记录）。
- 固件与安装：`scripts/onecloud/firmware/README.md`。`build-bundle.sh` 只用本仓库打安装包，`install.sh` 装到 Armbian 上，`prepare-image.sh` 清除所有个人数据，`pack-image.sh` 重建玩客云线刷包。

## 运行（OpenWrt）

- `supervisor_openwrt.py` 与 `platform_openwrt.py`：OpenWrt 上的主控与平台层，只读取 OpenWrt 的网络（`ubus`），把选中的 WiFi 经蓝牙交给 iPhone，WiFi 改了自动跟上；没有蓝牙时只提供有线。`board_zte_f50.py`：ZTE F50 的内置蓝牙初始化与独立 BlueZ。打包、安装与使用见 [openwrt-dashplay](https://github.com/yizhixiaoheigou/openwrt-dashplay)。
- `scripts/onecloud/firmware/build-bundle.sh` 生成的程序包（发布页的 `dashplay-bundle-<版本>.tar`）就是 OpenWrt 包的内容。
- `scripts/carplay-night-probe.mjs`：截取当前 CarPlay 画面（夜间模式检查）。

## 测试

```sh
python3 -m unittest discover -s tests/bluetooth     # 需要 dbus-daemon 与 dbus-next，测试身份均为合成数据
python3 -m unittest discover -s tests/onecloud      # 设置、多手机、平台、诊断、固件打包；证书测试需 OpenSSL 3
```

## 已知问题

- 有线 CarPlay 首次插上时，iPhone 有时 8 秒内不打开画面，盒子会自动重来一次，插线到出画约 50 秒（无线约 45 秒）。
- 播放页窗口每换一次大小（普通、全屏、分屏、竖屏）都按新尺寸重连一次（无线约 5–6 s，有线约 10 s），每个尺寸用它最清楚的分辨率等比显示、不拉伸；重连期间保留最后一帧。
- 方式一的无线直连要求车机热点在 5 GHz 的 36–48 或 149–165 信道，且尚未实车验证；方式三车机连着盒子热点时不能上网；方式二若随身 WiFi 开了设备隔离，无线会卡或连不上，只能有线。
- 车机浏览器第一次打开会提示证书不安全，点「继续访问」即可；车机 IP 变化、浏览器直接拦截自签证书、不让打开局域网地址时，设置页「车机打不开画面时」分组有对应选项，处理步骤见 [AI 安装说明](docs/DashPlay-AI安装说明.md) 第 4 节。
- 打电话和 Siri 用车机的麦克风（经浏览器）、从车机的媒体声道出声（不是头枕喇叭）；车机麦克风给浏览器的音量很小，盒子放大后对方能听清，但可能听到自己的回声。iPhone 同时连着车机自带蓝牙时，车机自己也进入通话、占住麦克风，对方听不到你：用 CarPlay 打电话时请断开 iPhone 与车机自带蓝牙。
- 车机自己的应用（如短视频）放声音时会让 iPhone 暂停，回来后不会自动继续：按一次方向盘或车机音乐卡片的播放键即可。
- 玩客云供电不足（普通 5 V USB 转 DC 线）会让 USB 网卡反复掉线；请用 12 V 电源或 5→12 V 升压线。
- 斐讯 N1 的 5 GHz 盒子热点只能 40 MHz。
- SSH 默认 `root` / `1234`，方便用户的 AI 助手放入认证文件；连上同一网络的人都能登录。
- 网页夜间模式指令已接入，但尚未证实 iPhone 会据此切换外观。

## 盒子设置图标

![盒子设置](assets/box-settings.png)

图标由项目几何绘图脚本重新生成，不使用第三方图标素材。可编辑来源、复现方法与许可见 [图标来源](assets/ICON-SOURCE.md)。接收端内部资源名保留 `f50-settings.png` 以兼容已有代码，产品名为 DashPlay。

## 许可与商标

GPL-3.0，见 [LICENSE](LICENSE) 与 [NOTICE.md](NOTICE.md)。CarPlay、iPhone、AirPlay 和 MFi 是 Apple Inc. 的商标；本项目与 Apple、PlayPort、DiPlay、xcertplay、WheelPlay 均无从属或背书关系。
