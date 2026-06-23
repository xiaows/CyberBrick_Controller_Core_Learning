# webcar.py - WiFi 网页遥控 (CyberBrick ESP32-C3 小车)
#
# 复用官方 bbl 硬件驱动 (motors/servos/leds)，完全绕过 RC(espnow) 协议栈。
# 用法 (在 REPL 里)：
#     import webcar
#     webcar.run()
# 然后手机连热点 MyCar(密码 12345678)，浏览器打开 http://192.168.4.1/
#
# 安全特性：超过 FAILSAFE_MS 没收到指令(断连/卡顿) 自动把油门归零刹车。

import sys
import time
import network
import binascii
import hashlib
import uasyncio as asyncio

# bbl 既可能在根路径也可能在 /bbl，两种 import 方式都兜底
for _p in ('/', '/bbl', '/app'):
    if _p not in sys.path:
        sys.path.append(_p)
try:
    from bbl.motors import MotorsController
    from bbl.servos import ServosController
    from bbl.leds import LEDController
except ImportError:
    from motors import MotorsController
    from servos import ServosController
    from leds import LEDController

# ----------------- 可调参数 -----------------
AP_SSID = "MyCar"
AP_PASS = "12345678"          # 至少 8 位
MOTOR_IDX = 1                 # 油门用哪个电机 (你的"货车"配置是 MOTOR1)
MOTOR_INVERT = False          # 若前进/后退反了，改成 True
SERVO_IDX = 1                 # 转向用哪个舵机 (你的配置是 PWM1)
STEER_MIN, STEER_MID, STEER_MAX = 50, 90, 130   # 与货车配置一致，防止打死卡舵机
LIGHT_CH = "LED2"             # 车灯通道
LIGHT_RGB = 0xFFFFFF          # 车灯颜色
FAILSAFE_MS = 600             # 多久没指令就刹车
# --------------------------------------------

motors = MotorsController()
servos = ServosController()
light = LEDController(LIGHT_CH)

_state = {"t": 0, "s": STEER_MID, "l": 0}
_last_seen = time.ticks_ms()


def _apply_throttle(t):
    t = max(-100, min(100, int(t)))
    if MOTOR_INVERT:
        t = -t
    motors.set_speed(MOTOR_IDX, int(t * 2047 / 100))


def _apply_steer(s):
    s = max(STEER_MIN, min(STEER_MAX, int(s)))
    servos.set_angle(SERVO_IDX, s)


def _apply_light(on):
    if on:
        light.set_led_effect(0, 0, 0xFF, 0x0F, LIGHT_RGB)   # 常亮，全部 4 颗
    else:
        light.set_led_effect(0, 0, 0xFF, 0x0F, 0x000000)    # 熄灭


def _handle_msg(msg):
    # 协议: "t,s,l"  t=油门(-100~100) s=转向(50~130) l=车灯(0/1)
    global _last_seen
    try:
        a = msg.split(",")
        t, s, l = int(a[0]), int(a[1]), int(a[2])
    except Exception:
        return
    _last_seen = time.ticks_ms()
    if t != _state["t"]:
        _state["t"] = t
        _apply_throttle(t)
    if s != _state["s"]:
        _state["s"] = s
        _apply_steer(s)
    if l != _state["l"]:
        _state["l"] = l
        _apply_light(l)


# ----------------- WebSocket -----------------
_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept(key):
    d = hashlib.sha1(key.encode() + _WS_GUID).digest()
    return binascii.b2a_base64(d).strip()


async def _readn(reader, n):
    buf = b""
    while len(buf) < n:
        c = await reader.read(n - len(buf))
        if not c:
            break
        buf += c
    return buf


async def _ws_read(reader):
    hdr = await _readn(reader, 2)
    if len(hdr) < 2:
        return None
    b1, b2 = hdr[0], hdr[1]
    opcode = b1 & 0x0F
    ln = b2 & 0x7F
    if ln == 126:
        ext = await _readn(reader, 2)
        ln = (ext[0] << 8) | ext[1]
    elif ln == 127:
        ext = await _readn(reader, 8)
        ln = 0
        for x in ext:
            ln = (ln << 8) | x
    mask = await _readn(reader, 4) if (b2 & 0x80) else None
    data = await _readn(reader, ln) if ln else b""
    if opcode == 0x8:          # close
        return None
    if mask:
        data = bytes(data[i] ^ mask[i % 4] for i in range(len(data)))
    return (opcode, data)


async def _serve(reader, writer):
    try:
        req = await reader.readline()
        if not req:
            return
        headers = {}
        while True:
            h = await reader.readline()
            if not h or h == b"\r\n":
                break
            try:
                k, v = h.decode().split(":", 1)
                headers[k.strip().lower()] = v.strip()
            except Exception:
                pass

        if headers.get("upgrade", "").lower() == "websocket":
            accept = _ws_accept(headers.get("sec-websocket-key", ""))
            writer.write(b"HTTP/1.1 101 Switching Protocols\r\n"
                         b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            await writer.drain()
            while True:
                frame = await _ws_read(reader)
                if frame is None:
                    break
                opcode, data = frame
                if opcode == 0x1:          # text
                    _handle_msg(data.decode())
        else:
            body = PAGE.encode()
            writer.write(b"HTTP/1.1 200 OK\r\n"
                         b"Content-Type: text/html; charset=utf-8\r\n"
                         b"Content-Length: " + str(len(body)).encode() +
                         b"\r\nConnection: close\r\n\r\n")
            writer.write(body)
            await writer.drain()
    except Exception as e:
        print("[web] conn err:", e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ----------------- 后台泵: 舵机步进 + 灯效 + 失联刹车 -----------------
async def _pump():
    while True:
        servos.timing_proc()
        light.timing_proc()
        if time.ticks_diff(time.ticks_ms(), _last_seen) > FAILSAFE_MS:
            if _state["t"] != 0:
                _state["t"] = 0
                _apply_throttle(0)
        await asyncio.sleep_ms(15)


def _start_ap():
    ap = network.WLAN(network.AP_IF)
    ap.active(False)
    ap.active(True)
    try:
        ap.config(essid=AP_SSID, password=AP_PASS)
    except Exception:
        ap.config(ssid=AP_SSID, password=AP_PASS)
    # 关闭 WiFi 省电：否则 C3 会自动 light-sleep 挂起 USB-JTAG，
    # 既让 REPL 失联、也使 AP 响应迟钝。对实时遥控必须关掉。
    try:
        ap.config(pm=network.WLAN.PM_NONE)
    except Exception:
        try:
            ap.config(pm=0)
        except Exception:
            pass
    t0 = time.ticks_ms()
    while not ap.active() and time.ticks_diff(time.ticks_ms(), t0) < 5000:
        time.sleep_ms(100)
    return ap


async def _amain():
    _apply_throttle(0)
    _apply_steer(STEER_MID)
    _apply_light(0)
    asyncio.create_task(_pump())
    await asyncio.start_server(_serve, "0.0.0.0", 80)
    print("[web] server up on port 80")
    while True:
        await asyncio.sleep(3600)


def run():
    ap = _start_ap()
    ip = ap.ifconfig()[0]
    print("[web] AP SSID:", AP_SSID, " PASS:", AP_PASS)
    print("[web] open http://%s/" % ip)
    try:
        # 持久状态文件，便于在 USB 被 WiFi 挂起时仍可事后核验
        open("/webcar_up.txt", "w").write("AP=%s ACTIVE=%s IP=%s" %
                                          (AP_SSID, ap.active(), ip))
    except Exception:
        pass
    try:
        asyncio.run(_amain())
    finally:
        asyncio.new_event_loop()


# ----------------- 网页 (内嵌) -----------------
PAGE = """<!DOCTYPE html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>CyberBrick WebCar</title>
<style>
 body{margin:0;font-family:sans-serif;background:#111;color:#eee;touch-action:none;-webkit-user-select:none;user-select:none}
 #wrap{display:flex;flex-direction:column;align-items:center;gap:22px;padding:20px}
 .ctl{display:flex;flex-direction:column;align-items:center}
 .lbl{font-size:14px;margin-top:6px;color:#9ab}
 input[type=range]{-webkit-appearance:none;appearance:none;background:#2a2a2a;border-radius:30px;outline:none}
 #thr{writing-mode:vertical-lr;direction:rtl;width:56px;height:260px}
 #steer{width:280px;height:56px}
 input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:52px;height:52px;border-radius:50%;background:#4af;box-shadow:0 0 8px #4af}
 input[type=range]::-moz-range-thumb{width:52px;height:52px;border:0;border-radius:50%;background:#4af}
 #light{padding:16px 28px;font-size:17px;border:0;border-radius:14px;background:#444;color:#fff}
 #light.on{background:#fd0;color:#000}
 #bar{font-size:18px;display:flex;align-items:center;gap:8px}
 #dot{width:13px;height:13px;border-radius:50%;background:#c33;display:inline-block}
 #dot.ok{background:#3c6}
</style></head><body>
<div id=wrap>
 <div id=bar><span id=dot></span><span id=st>未连接</span></div>
 <div class=ctl><input id=thr type=range min=-100 max=100 value=0><div class=lbl>油门 (松开回中)</div></div>
 <div class=ctl><input id=steer type=range min=50 max=130 value=90><div class=lbl>转向 (松开回正)</div></div>
 <button id=light>车灯</button>
</div>
<script>
let t=0,s=90,l=0,ws;
const thr=document.getElementById('thr'),steer=document.getElementById('steer'),
 light=document.getElementById('light'),dot=document.getElementById('dot'),st=document.getElementById('st');
function send(){if(ws&&ws.readyState===1)ws.send(t+','+s+','+l);}
function status(ok){dot.className=ok?'ok':'';st.textContent=ok?'已连接':'重连中...';}
thr.oninput=()=>{t=+thr.value;send();};
steer.oninput=()=>{s=+steer.value;send();};
function relT(){t=0;thr.value=0;send();}
function relS(){s=90;steer.value=90;send();}
['pointerup','pointercancel','touchend','mouseup'].forEach(e=>{
 thr.addEventListener(e,relT);steer.addEventListener(e,relS);});
light.onclick=()=>{l=l?0:1;light.className=l?'on':'';send();};
function connect(){
 ws=new WebSocket('ws://'+location.host+'/');
 ws.onopen=()=>status(true);
 ws.onclose=()=>{status(false);setTimeout(connect,1000);};
 ws.onerror=()=>{try{ws.close();}catch(e){}};
}
connect();
setInterval(send,200);   // 心跳，保持失联刹车不误触发
</script></body></html>"""
