# 视频半包读取期限

当 iPhone 发送了部分视频包后停止发送，但保持 TCP 连接时，原 `ScreenStream.readFully` 会一直等待。控制通道仍可用，刷新浏览器也无法让这个视频接收循环退出。

`playport-screen-read-deadline.patch` 为已经开始的包头和已声明的包体分别设置 10 秒绝对读取期限，使用单调时钟计时。持续发送少量字节不会延长期限。等待下一包的首字节时仍允许无限空闲，避免把静止画面判为断线。

超时沿现有 `onClosed` 路径关闭会话，由现有连接管理处理后续重连。10 秒是读取阶段的期限，并非画面恢复时间保证。该修改不处理完整包之间完全静默的情况，也不确定手机停止发送的初始原因。

## 回归测试

补丁同时加入 `ScreenStreamReadDeadlineTest`，通过项目原有的 JUnit 任务执行。测试仅使用回环 TCP、人工密钥与人工视频数据，不需要手机、证书或配对记录。

在 README 指定的 PlayPort 提交上应用 `patches/series` 后运行：

```sh
./gradlew :protocol:test --tests '*ScreenStreamReadDeadlineTest'
./gradlew :protocol:test :server:test :server:installDist
```

Windows 可使用 JDK 调用同一个 wrapper：

```powershell
java.exe -classpath gradle/wrapper/gradle-wrapper.jar org.gradle.wrapper.GradleWrapperMain :protocol:test --tests '*ScreenStreamReadDeadlineTest'
```

覆盖：半包头、半包体、完全缺失包体、包间空闲、正常分片、持续零碎数据、不同 nonce 的加密帧、无效认证标签，以及主动关闭空闲/半包连接。

为了仅绑定回环地址，测试通过反射调用实际接收循环。超时测试使用生产的 10 秒期限，完整测试组通常需要约一分钟。

验证测试有效性时，可暂时恢复补丁前的 `ScreenStream.kt`，保留测试并运行 `incompleteBodyTimesOut`：未修复版本应因接收循环未退出而失败；恢复修复后应通过。
