# WebCar — WiFi 网页遥控

基于 CyberBrick ESP32-C3 小车的自制网页控制器。**复用官方 `bbl` 硬件驱动，绕过 RC(espnow) 协议栈**——不需要官方遥控器，用手机/电脑浏览器直接控车。

## 原理

```
手机/电脑浏览器 ──WiFi──> 小车(开热点 + WebSocket 服务器)
   摇杆HTML/JS              └ 直接调 MotorsController / ServosController / LEDController
```

- 油门 → `MotorsController().set_speed(1, -2048..2048)`
- 转向 → `ServosController().set_angle(1, 50..130)`
- 车灯 → `LEDController("LED2").set_led_effect(...)`
- 后台 `uasyncio` 任务每 15ms 调 `timing_proc()` 驱动舵机平滑步进与灯效
- **失联保护**：超过 600ms 没收到指令自动油门归零刹车

## 部署

板子是 Secure Download Mode，**不能用 esptool 刷固件**，但可通过 REPL 往文件系统写文件：

```bash
# 用 mpremote 上传
mpremote connect COM9 fs cp webcar.py :webcar.py
```

或用本仓库根目录的串口工具（带 Ctrl-C 抢 REPL 逻辑）。

## ⚠️ 重要：必须开机代替官方程序运行

官方固件开机时 `rc_slave_init()` 会用 **espnow 占用 WiFi 射频**，之后在 REPL 里 `import webcar; webcar.run()`
会因射频被占而在 `ap.active(True)` 处**卡死**。所以 webcar **不能在官方程序起来后再启动**，必须在 `boot.py`
阶段、官方 `rc_main` 之前就接管（这样射频是空闲的）。

## 条件启动机制（已部署）

`boot.py` 改成了条件分支：

- **默认**（无标志文件）→ 跑官方 `rc_main`，官方遥控正常
- **存在 `/webcar.flag`** → 跑 webcar 网页控制（开机留 3 秒 Ctrl-C 中断窗口作安全门）

原版 boot.py 已备份为板上的 `boot_official.py`。

### 切换到网页模式

在 REPL 里建标志文件后复位：

```python
f = open('/webcar.flag', 'w'); f.write('1'); f.close()
import machine; machine.reset()
```

启动后手机连热点 **MyCar**（密码 `12345678`），浏览器打开 **http://192.168.4.1/**。

### 切回官方遥控

WiFi 开启后 USB-JTAG 可能被挂起，需用「复位 + 3 秒内狂发 Ctrl-C」抢回 REPL，然后删标志：

```python
import os; os.remove('/webcar.flag')
import machine; machine.reset()
```

或恢复原版 boot：`open('/boot.py','w').write(open('/boot_official.py').read())`。

## C3 的一个坑：WiFi 与 USB-JTAG

ESP32-C3 开 WiFi 后会自动 light-sleep 挂起 USB 串口（REPL 失联）。webcar 在起 AP 后立刻设
`ap.config(pm=network.WLAN.PM_NONE)` 关闭省电来缓解，同时也让 AP 实时响应（对遥控很重要）。

## 可调参数（webcar.py 顶部）

| 参数 | 说明 |
|------|------|
| `AP_SSID` / `AP_PASS` | 热点名/密码 |
| `MOTOR_INVERT` | 前进后退反了就改 `True` |
| `STEER_MIN/MID/MAX` | 转向角度范围（防打死卡舵机） |
| `LIGHT_RGB` | 车灯颜色 |
| `FAILSAFE_MS` | 失联刹车时间 |
