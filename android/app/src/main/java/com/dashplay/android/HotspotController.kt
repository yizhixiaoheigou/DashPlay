package com.dashplay.android

/** Main-thread state machine. The driver owns Android callbacks; this class owns retries and stale results. */
internal class HotspotController<C>(private val driver: Driver<C>, private val clock: () -> Long,
    private val later: (Long, () -> Unit) -> Cancel, private val changed: () -> Unit) {
    enum class Phase { OFF, STARTING, READY, FAILED, STOPPING }
    data class Group<C>(val owned: Boolean, val connection: C?)
    fun interface Cancel { fun cancel() }
    interface Driver<C> {
        fun unavailable(): String?
        fun inspect(done: (Result<Group<C>?>) -> Unit)
        fun create(done: (Result<Unit>) -> Unit)
        fun remove(done: (Result<Unit>) -> Unit)
        fun close()
    }
    @Volatile var connection: C? = null; private set
    @Volatile var phase = Phase.OFF; private set
    @Volatile var message = ""; private set
    private var wanted = false
    private var disposed = false
    private var finished = false
    private var pending = false
    private var serial = 0L
    private var failures = 0
    private var waitingSince: Long? = null
    private var stopDeadline = Long.MAX_VALUE
    private var tick: Cancel? = null
    private var timeout: Cancel? = null
    private val closed = mutableListOf<() -> Unit>()

    fun configure(enabled: Boolean, retry: Boolean = false) {
        if (disposed) return
        wanted = enabled
        if (retry) failures = 0
        if (!enabled) update(Phase.STOPPING, "正在关闭本地热点")
        schedule(0)
    }
    fun disconnected() {
        if (finished) return
        serial++; pending = false; timeout?.cancel()
        update(Phase.FAILED, "Wi-Fi 服务已断开，正在等待恢复")
        schedule(2000)
    }
    fun close(done: () -> Unit = {}) {
        if (finished) { done(); return }
        closed += done
        if (disposed) return
        wanted = false; disposed = true; stopDeadline = clock() + 30_000
        update(Phase.STOPPING, "正在关闭本地热点")
        schedule(0)
    }
    private fun update(next: Phase, text: String, value: C? = null) {
        val different = phase != next || message != text || connection != value
        connection = value; phase = next; message = text
        if (different) changed()
    }
    private fun schedule(delay: Long) {
        if (finished) return
        tick?.cancel(); tick = later(delay) { tick = null; drive() }
    }
    private fun failed(text: String) {
        update(Phase.FAILED, text)
        val delay = longArrayOf(2000, 5000, 15000, 30000, 60000)[failures.coerceAtMost(4)]
        failures++; schedule(delay.coerceAtMost((stopDeadline - clock()).coerceAtLeast(0)))
    }
    private fun <T> request(invoke: ((Result<T>) -> Unit) -> Unit, complete: (Result<T>) -> Unit) {
        pending = true
        val id = ++serial
        timeout?.cancel()
        timeout = later(10_000) {
            if (id == serial && pending) {
                serial++; pending = false
                failed("系统未响应热点操作，将自动重试；也可切回系统热点。")
            }
        }
        val done: (Result<T>) -> Unit = done@ { result ->
            if (finished) return@done
            if (id != serial) { schedule(0); return@done }
            pending = false; timeout?.cancel(); timeout = null
            complete(result)
        }
        try { invoke(done) } catch (error: RuntimeException) { done(Result.failure(error)) }
    }
    private fun drive() {
        if (finished) return
        if (disposed && clock() >= stopDeadline) {
            update(Phase.FAILED, "未能确认本地热点已关闭；恢复权限后重新打开应用可继续清理。")
            finish(); return
        }
        if (pending) return
        val unavailable = runCatching { driver.unavailable() }.getOrElse { "无法访问系统 Wi-Fi，正在等待恢复。" }
        if (unavailable != null) {
            update(Phase.FAILED, unavailable)
            schedule(5000L.coerceAtMost((stopDeadline - clock()).coerceAtLeast(0)))
            return
        }
        request(driver::inspect) { result ->
            if (result.isFailure) { failed("无法读取本地热点状态，正在等待系统恢复。"); return@request }
            val group = result.getOrNull()
            if (!wanted) {
                if (group?.owned == true) {
                    update(Phase.STOPPING, "正在关闭本地热点")
                    request(driver::remove) { removed ->
                        if (removed.isSuccess) schedule(0)
                        else failed("系统未能关闭本地热点，将重试清理；请保留附近设备权限。")
                    }
                } else {
                    update(Phase.OFF, "")
                    driver.close()
                    waitingSince = null; failures = 0
                    if (disposed) finish()
                }
                return@request
            }
            when {
                group == null -> {
                    update(Phase.STARTING, "正在创建本地热点")
                    waitingSince = clock()
                    request(driver::create) { created ->
                        if (created.isSuccess) schedule(1000)
                        else { waitingSince = null; failed(created.exceptionOrNull()?.message ?: "系统未能创建热点，将自动重试。") }
                    }
                }
                !group.owned -> {
                    waitingSince = null
                    update(Phase.FAILED, "Wi-Fi Direct 正被其他直连占用；结束投屏后会自动重试，也可使用系统热点。")
                    schedule(5000)
                }
                group.connection != null -> {
                    failures = 0; waitingSince = null
                    update(Phase.READY, "本地热点已开启，不提供互联网", group.connection)
                    schedule(2000)
                }
                else -> {
                    val since = waitingSince ?: clock().also { waitingSince = it }
                    if (clock() - since >= 30_000) {
                        update(Phase.STOPPING, "热点地址尚未就绪，正在重新创建")
                        request(driver::remove) { removed ->
                            waitingSince = null
                            if (removed.isSuccess) failed("等待本地热点地址超时，将自动重试。")
                            else failed("系统未能清理未就绪的热点，将重试。")
                        }
                    } else {
                        update(Phase.STARTING, "正在等待本地热点地址和信道")
                        schedule(2000)
                    }
                }
            }
        }
    }
    private fun finish() {
        finished = true; serial++; pending = false
        tick?.cancel(); timeout?.cancel(); driver.close()
        val callbacks = closed.toList(); closed.clear()
        callbacks.forEach { it() }
    }
}
