"""
Android controller client (Kivy). Sends stick/button/pedal state to the PC
as small UDP packets, and vibrates the phone when the PC forwards rumble
state back from the game.

Two layouts, switchable in-app:
  - Standard gamepad (dual joysticks + face buttons)
  - Racing mode (steering wheel + gas/brake pedals), mapped to Forza
    Horizon 6's default Xbox controller layout: Gas = RT, Brake = LT,
    Handbrake = A, Camera = RB, Rewind = Y, Gear Up/Down = B/X.
"""

import socket
import struct
import threading
import time
import hashlib
import math

from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.widget import Widget
from kivy.uix.textinput import TextInput
from kivy.uix.label import Label
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.graphics import (Color, Ellipse, Line, RoundedRectangle,
                            PushMatrix, PopMatrix, Rotate)
from kivy.clock import Clock
from kivy.animation import Animation
from kivy.properties import NumericProperty
from kivy.core.window import Window

try:
    from plyer import vibrator
except Exception:
    vibrator = None

Window.clearcolor = (0.06, 0.06, 0.08, 1)

PORT = 7777
HEADER_FMT = "!BII"
INPUT_FMT = "!Hhhhh BB"
HEADER_SIZE = struct.calcsize(HEADER_FMT)

TYPE_INPUT = 0x01
TYPE_HEARTBEAT = 0x02
TYPE_RUMBLE = 0x03
TYPE_HELLO = 0x04

BUTTON_BITS = {
    "A": 0, "B": 1, "X": 2, "Y": 3,
    "LB": 4, "RB": 5, "LS": 6, "RS": 7,
    "START": 8, "BACK": 9,
    "UP": 10, "DOWN": 11, "LEFT": 12, "RIGHT": 13,
}


def token_for(pin: str) -> int:
    return int.from_bytes(hashlib.sha256(pin.encode()).digest()[:4], "big")


# ---------------------------------------------------------------------
# Custom rounded button: shadow + fill + top sheen + border, with a
# quick "press down" animation instead of a flat color swap.
# ---------------------------------------------------------------------
class RoundButton(Widget):
    press_offset = NumericProperty(3)

    def __init__(self, text="", on_press_cb=None, on_release_cb=None,
                 font_size="15sp", accent=(0.30, 0.47, 0.95, 1), **kwargs):
        super().__init__(**kwargs)
        self.on_press_cb = on_press_cb
        self.on_release_cb = on_release_cb
        self._touch_id = None
        self.fill_normal = (0.19, 0.20, 0.25, 1)
        self.fill_pressed = accent

        with self.canvas:
            Color(0, 0, 0, 0.35)
            self.shadow = RoundedRectangle(radius=[14])
            self.fill_color = Color(*self.fill_normal)
            self.fill = RoundedRectangle(radius=[14])
            Color(1, 1, 1, 0.06)
            self.top_sheen = RoundedRectangle(radius=[14, 14, 4, 4])
            Color(1, 1, 1, 0.12)
            self.border = Line(width=1)

        self.label = Label(text=text, font_size=font_size, bold=True,
                            color=(0.94, 0.95, 0.98, 1))
        self.add_widget(self.label)
        self.bind(pos=self._redraw, size=self._redraw, press_offset=self._redraw)

    def _redraw(self, *_a):
        x, y, w, h = self.x, self.y, self.width, self.height
        self.shadow.pos = (x, y - self.press_offset)
        self.shadow.size = (w, h)
        self.fill.pos = (x, y)
        self.fill.size = (w, h)
        self.top_sheen.pos = (x, y + h * 0.52)
        self.top_sheen.size = (w, h * 0.48)
        self.border.rounded_rectangle = (x, y, w, h, 14)
        self.label.pos = (x, y)
        self.label.size = (w, h)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos) and self._touch_id is None:
            self._touch_id = touch.uid
            Animation.cancel_all(self, "press_offset")
            Animation(press_offset=0, d=0.04).start(self)
            self.fill_color.rgba = self.fill_pressed
            if self.on_press_cb:
                self.on_press_cb()
            return True
        return super().on_touch_down(touch)

    def on_touch_up(self, touch):
        if touch.uid == self._touch_id:
            self._touch_id = None
            Animation.cancel_all(self, "press_offset")
            Animation(press_offset=3, d=0.09, t="out_quad").start(self)
            self.fill_color.rgba = self.fill_normal
            if self.on_release_cb:
                self.on_release_cb()
            return True
        return super().on_touch_up(touch)


# ---------------------------------------------------------------------
# Joystick: concave "socket" base with a raised knob, spring-back on
# release. Used by the standard gamepad layout.
# ---------------------------------------------------------------------
class Joystick(FloatLayout):
    knob_x = NumericProperty(0)
    knob_y = NumericProperty(0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.radius = 92
        self.knob_radius = 70
        self.x_val = 0.0
        self.y_val = 0.0
        self._touch_id = None

        with self.canvas:
            self.glow_color = Color(0.30, 0.55, 1, 0)
            self.glow = Ellipse()
            Color(0, 0, 0, 0.30)
            self.socket_shadow = Ellipse()
            Color(0.15, 0.15, 0.18, 1)
            self.base_outer = Ellipse()
            Color(0.115, 0.115, 0.14, 1)
            self.base_mid = Ellipse()
            Color(0.085, 0.085, 0.105, 1)
            self.base_inner = Ellipse()
            Color(1, 1, 1, 0.05)
            self.base_ring = Line(width=1.1)
            Color(0, 0, 0, 0.4)
            self.knob_shadow = Ellipse()
            Color(0.26, 0.28, 0.34, 1)
            self.knob_body = Ellipse()
            Color(0.37, 0.40, 0.48, 1)
            self.knob_cap = Ellipse()
            Color(1, 1, 1, 0.20)
            self.knob_highlight = Ellipse()
            Color(1, 1, 1, 0.10)
            self.knob_ring = Line(width=1)

        self.bind(pos=self._layout_base, size=self._layout_base,
                  knob_x=self._layout_knob, knob_y=self._layout_knob)
        Clock.schedule_once(self._layout_base)

    def _layout_base(self, *_a):
        r = self.radius
        cx, cy = self.center_x, self.center_y
        self.socket_shadow.pos = (cx - r * 1.05, cy - r * 1.12)
        self.socket_shadow.size = (r * 2.1, r * 2.1)
        self.base_outer.pos = (cx - r, cy - r)
        self.base_outer.size = (r * 2, r * 2)
        self.base_mid.pos = (cx - r * 0.82, cy - r * 0.82)
        self.base_mid.size = (r * 1.64, r * 1.64)
        self.base_inner.pos = (cx - r * 0.62, cy - r * 0.62)
        self.base_inner.size = (r * 1.24, r * 1.24)
        self.base_ring.circle = (cx, cy, r)
        self.glow.pos = (cx - r * 1.18, cy - r * 1.18)
        self.glow.size = (r * 2.36, r * 2.36)
        if self.knob_x == 0 and self.knob_y == 0:
            self.knob_x, self.knob_y = cx, cy
        else:
            self._layout_knob()

    def _layout_knob(self, *_a):
        kr = self.knob_radius
        kx, ky = self.knob_x, self.knob_y
        self.knob_shadow.pos = (kx - kr / 2 + 3, ky - kr / 2 - 4)
        self.knob_shadow.size = (kr, kr)
        self.knob_body.pos = (kx - kr / 2, ky - kr / 2)
        self.knob_body.size = (kr, kr)
        cap = kr * 0.86
        self.knob_cap.pos = (kx - cap / 2, ky - cap / 2 + kr * 0.03)
        self.knob_cap.size = (cap, cap)
        self.knob_highlight.pos = (kx - kr * 0.24, ky + kr * 0.06)
        self.knob_highlight.size = (kr * 0.36, kr * 0.26)
        self.knob_ring.circle = (kx, ky, kr / 2)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos) and self._touch_id is None:
            self._touch_id = touch.uid
            Animation.cancel_all(self, "knob_x")
            Animation.cancel_all(self, "knob_y")
            Animation(rgba=(0.30, 0.55, 1, 0.35), d=0.12).start(self.glow_color)
            self._move_knob(touch)
            return True
        return super().on_touch_down(touch)

    def on_touch_move(self, touch):
        if touch.uid == self._touch_id:
            self._move_knob(touch)
            return True
        return super().on_touch_move(touch)

    def on_touch_up(self, touch):
        if touch.uid == self._touch_id:
            self._touch_id = None
            self.x_val = 0.0
            self.y_val = 0.0
            Animation(knob_x=self.center_x, knob_y=self.center_y,
                      d=0.22, t="out_back").start(self)
            Animation(rgba=(0.30, 0.55, 1, 0), d=0.25).start(self.glow_color)
            return True
        return super().on_touch_up(touch)

    def _move_knob(self, touch):
        dx, dy = touch.x - self.center_x, touch.y - self.center_y
        dist = (dx ** 2 + dy ** 2) ** 0.5
        max_dist = self.radius - self.knob_radius / 2 - 3
        if dist > max_dist:
            dx, dy = dx / dist * max_dist, dy / dist * max_dist
        self.knob_x = self.center_x + dx
        self.knob_y = self.center_y + dy
        self.x_val = dx / max_dist if max_dist else 0
        self.y_val = dy / max_dist if max_dist else 0


# ---------------------------------------------------------------------
# Steering wheel: a real rotating wheel graphic (rim + spokes + hub).
# Drag around it to turn; it springs back to center on release, like a
# self-centering wheel.
# ---------------------------------------------------------------------
class SteeringWheel(FloatLayout):
    angle = NumericProperty(0)   # degrees, -MAX_ANGLE..+MAX_ANGLE
    MAX_ANGLE = 150

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.radius = 115
        self.value = 0.0  # -1..1 steering output
        self._touch_id = None
        self._start_touch_angle = 0.0
        self._start_wheel_angle = 0.0

        with self.canvas.before:
            Color(0, 0, 0, 0.3)
            self.shadow = Ellipse()
            self.glow_color = Color(0.30, 0.55, 1, 0)
            self.glow = Ellipse()
            PushMatrix()
            self.rotate = Rotate(angle=0, origin=(0, 0))

        with self.canvas:
            Color(0.12, 0.12, 0.15, 1)
            self.hub_backing = Ellipse()
            Color(0.85, 0.86, 0.9, 0.9)
            self.rim = Line(width=16, cap="round", joint="round")
            Color(0.30, 0.32, 0.38, 1)
            self.spoke1 = Line(width=10, cap="round")
            self.spoke2 = Line(width=10, cap="round")
            self.spoke3 = Line(width=10, cap="round")
            Color(0.20, 0.21, 0.26, 1)
            self.hub = Ellipse()
            Color(1, 1, 1, 0.10)
            self.hub_ring = Line(width=1.2)
            Color(1, 1, 1, 0.18)
            self.grip_left = Ellipse()
            self.grip_right = Ellipse()

        with self.canvas.after:
            PopMatrix()

        self.bind(pos=self._layout, size=self._layout, angle=self._on_angle)
        Clock.schedule_once(self._layout)

    def _layout(self, *_a):
        r = self.radius
        cx, cy = self.center_x, self.center_y
        self.rotate.origin = (cx, cy)

        self.shadow.pos = (cx - r * 1.02, cy - r * 1.1)
        self.shadow.size = (r * 2.04, r * 2.04)
        self.glow.pos = (cx - r * 1.15, cy - r * 1.15)
        self.glow.size = (r * 2.3, r * 2.3)

        self.hub_backing.pos = (cx - r, cy - r)
        self.hub_backing.size = (r * 2, r * 2)
        self.rim.circle = (cx, cy, r - 10)

        for line, deg in ((self.spoke1, 90), (self.spoke2, 210), (self.spoke3, 330)):
            rad = math.radians(deg)
            ex = cx + (r - 14) * math.cos(rad)
            ey = cy + (r - 14) * math.sin(rad)
            line.points = [cx, cy, ex, ey]

        hub_r = 26
        self.hub.pos = (cx - hub_r, cy - hub_r)
        self.hub.size = (hub_r * 2, hub_r * 2)
        self.hub_ring.circle = (cx, cy, hub_r)

        grip_r = 9
        for grip, deg in ((self.grip_left, 180), (self.grip_right, 0)):
            rad = math.radians(deg)
            gx = cx + (r - 10) * math.cos(rad)
            gy = cy + (r - 10) * math.sin(rad)
            grip.pos = (gx - grip_r, gy - grip_r)
            grip.size = (grip_r * 2, grip_r * 2)

    def _on_angle(self, *_a):
        self.rotate.angle = self.angle

    def on_touch_down(self, touch):
        dx, dy = touch.x - self.center_x, touch.y - self.center_y
        if (dx ** 2 + dy ** 2) ** 0.5 <= self.radius and self._touch_id is None:
            self._touch_id = touch.uid
            Animation.cancel_all(self, "angle")
            Animation(rgba=(0.30, 0.55, 1, 0.3), d=0.12).start(self.glow_color)
            self._start_touch_angle = math.degrees(math.atan2(dy, dx))
            self._start_wheel_angle = self.angle
            return True
        return super().on_touch_down(touch)

    def on_touch_move(self, touch):
        if touch.uid == self._touch_id:
            dx, dy = touch.x - self.center_x, touch.y - self.center_y
            current_angle = math.degrees(math.atan2(dy, dx))
            delta = current_angle - self._start_touch_angle
            while delta > 180:
                delta -= 360
            while delta < -180:
                delta += 360
            new_angle = self._start_wheel_angle + delta
            new_angle = max(-self.MAX_ANGLE, min(self.MAX_ANGLE, new_angle))
            self.angle = new_angle
            self.value = new_angle / self.MAX_ANGLE
            return True
        return super().on_touch_move(touch)

    def on_touch_up(self, touch):
        if touch.uid == self._touch_id:
            self._touch_id = None
            self.value = 0.0
            Animation(angle=0, d=0.25, t="out_back").start(self)
            Animation(rgba=(0.30, 0.55, 1, 0), d=0.25).start(self.glow_color)
            return True
        return super().on_touch_up(touch)


# ---------------------------------------------------------------------
# Vertical pedal slider — used for Gas and Brake. Snaps back to 0 the
# moment you lift your finger, like a real spring-loaded pedal.
# ---------------------------------------------------------------------
class PedalSlider(FloatLayout):
    value = NumericProperty(0)  # 0..1

    def __init__(self, label="", color=(0.3, 0.8, 0.4, 1), **kwargs):
        super().__init__(**kwargs)
        self._touch_id = None
        self.track_color = color

        with self.canvas:
            Color(0, 0, 0, 0.3)
            self.shadow = RoundedRectangle(radius=[16])
            Color(0.14, 0.14, 0.18, 1)
            self.track_bg = RoundedRectangle(radius=[16])
            Color(1, 1, 1, 0.06)
            self.track_border = Line(width=1)
            self.fill_color = Color(*color)
            self.fill = RoundedRectangle(radius=[16])
            Color(1, 1, 1, 0.16)
            self.fill_sheen = RoundedRectangle(radius=[16, 16, 0, 0])

        self.label = Label(text=label, font_size="12sp", bold=True,
                            color=(0.85, 0.86, 0.9, 1))
        self.add_widget(self.label)
        self.bind(pos=self._redraw, size=self._redraw, value=self._redraw)

    def _redraw(self, *_a):
        x, y, w, h = self.x, self.y, self.width, self.height
        self.shadow.pos = (x, y - 3)
        self.shadow.size = (w, h)
        self.track_bg.pos = (x, y)
        self.track_bg.size = (w, h)
        self.track_border.rounded_rectangle = (x, y, w, h, 16)
        fill_h = h * self.value
        self.fill.pos = (x, y)
        self.fill.size = (w, max(fill_h, 1))
        sheen_h = min(10, fill_h)
        self.fill_sheen.pos = (x, y + max(fill_h - sheen_h, 0))
        self.fill_sheen.size = (w, sheen_h)
        self.label.pos = (x, y + h + 4)
        self.label.size = (w, 20)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos) and self._touch_id is None:
            self._touch_id = touch.uid
            Animation.cancel_all(self, "value")
            self._update_value(touch)
            return True
        return super().on_touch_down(touch)

    def on_touch_move(self, touch):
        if touch.uid == self._touch_id:
            self._update_value(touch)
            return True
        return super().on_touch_move(touch)

    def on_touch_up(self, touch):
        if touch.uid == self._touch_id:
            self._touch_id = None
            Animation(value=0, d=0.15, t="out_quad").start(self)
            return True
        return super().on_touch_up(touch)

    def _update_value(self, touch):
        rel = (touch.y - self.y) / self.height
        self.value = max(0.0, min(1.0, rel))


class Net:
    """Owns the UDP socket, sequence counter, and background threads."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addr = None
        self.token = 0
        self.seq = 0
        self.connected = False
        self._stop = False

    def connect(self, ip, pin, on_result):
        self.token = token_for(pin)
        self.addr = (ip, PORT)

        def worker():
            try:
                self.sock.settimeout(2.0)
                self.sock.sendto(struct.pack(HEADER_FMT, TYPE_HELLO, 0, self.token), self.addr)
                data, _ = self.sock.recvfrom(64)
                pkt_type, _, token = struct.unpack(HEADER_FMT, data[:HEADER_SIZE])
                ok = pkt_type == TYPE_HELLO and token == self.token
            except Exception:
                ok = False
            if ok:
                self.connected = True
                threading.Thread(target=self._heartbeat, daemon=True).start()
                threading.Thread(target=self._listen_rumble, daemon=True).start()
            Clock.schedule_once(lambda dt: on_result(ok))

        threading.Thread(target=worker, daemon=True).start()

    def _heartbeat(self):
        while self.connected and not self._stop:
            try:
                self.sock.sendto(struct.pack(HEADER_FMT, TYPE_HEARTBEAT, 0, self.token), self.addr)
            except Exception:
                pass
            time.sleep(0.5)

    def _listen_rumble(self):
        self.sock.settimeout(1.0)
        while self.connected and not self._stop:
            try:
                data, _ = self.sock.recvfrom(64)
            except socket.timeout:
                continue
            except Exception:
                break
            if len(data) < HEADER_SIZE + 2 or data[0] != TYPE_RUMBLE:
                continue
            large, small = struct.unpack("!BB", data[HEADER_SIZE : HEADER_SIZE + 2])
            if vibrator and (large > 10 or small > 10):
                try:
                    vibrator.vibrate(0.05 + (max(large, small) / 255) * 0.1)
                except Exception:
                    pass

    def send_input(self, buttons, lx, ly, rx, ry, lt, rt):
        if not self.connected:
            return
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        header = struct.pack(HEADER_FMT, TYPE_INPUT, self.seq, self.token)
        body = struct.pack(
            INPUT_FMT, buttons,
            int(lx * 32767), int(ly * 32767),
            int(rx * 32767), int(ry * 32767),
            int(lt * 255), int(rt * 255),
        )
        try:
            self.sock.sendto(header + body, self.addr)
        except Exception:
            pass


class ThemedInput(TextInput):
    def __init__(self, **kwargs):
        super().__init__(
            background_color=(0.14, 0.14, 0.18, 1),
            foreground_color=(0.95, 0.95, 0.98, 1),
            hint_text_color=(0.5, 0.5, 0.56, 1),
            cursor_color=(0.35, 0.55, 1, 1),
            padding=[14, 14, 14, 14],
            **kwargs,
        )


class ConnectScreen(Screen):
    def __init__(self, net, on_connected, **kwargs):
        super().__init__(**kwargs)
        self.net = net
        self.on_connected = on_connected
        layout = BoxLayout(orientation="vertical", padding=40, spacing=20)
        layout.add_widget(Label(text="PC controller", font_size="26sp",
                                 bold=True, color=(0.95, 0.95, 0.98, 1)))
        self.ip_input = ThemedInput(hint_text="PC IP address (e.g. 192.168.1.20)", multiline=False)
        self.pin_input = ThemedInput(hint_text="PIN shown on PC", multiline=False, password=True)
        layout.add_widget(self.ip_input)
        layout.add_widget(self.pin_input)
        self.status = Label(text="", color=(0.75, 0.76, 0.8, 1))
        layout.add_widget(self.status)
        connect_btn = RoundButton(text="CONNECT", size_hint_y=None, height=64,
                                   on_release_cb=self._connect)
        layout.add_widget(connect_btn)
        self.add_widget(layout)

    def _connect(self):
        self.status.text = "Connecting..."
        self.net.connect(self.ip_input.text.strip(), self.pin_input.text.strip(), self._result)

    def _result(self, ok):
        if ok:
            self.on_connected()
        else:
            self.status.text = "Couldn't connect. Check the IP and PIN, and that server.py is running."


class ControllerScreen(Screen):
    def __init__(self, net, **kwargs):
        super().__init__(**kwargs)
        self.net = net
        self.pressed = 0
        layout = FloatLayout()

        self.left_stick = Joystick(size_hint=(0.42, 0.42), pos_hint={"x": 0.03, "y": 0.06})
        self.right_stick = Joystick(size_hint=(0.42, 0.42), pos_hint={"x": 0.55, "y": 0.06})
        layout.add_widget(self.left_stick)
        layout.add_widget(self.right_stick)

        face_buttons = [("Y", 0.76, 0.58), ("X", 0.68, 0.48), ("B", 0.84, 0.48), ("A", 0.76, 0.38)]
        for name, x, y in face_buttons:
            layout.add_widget(self._make_button(name, x, y, w=0.1, h=0.09, font_size="16sp"))

        for name, x, y in [("LB", 0.03, 0.86), ("RB", 0.85, 0.86)]:
            layout.add_widget(self._make_button(name, x, y, w=0.12, h=0.08, font_size="14sp"))

        for name, x, y in [("BACK", 0.4, 0.9), ("START", 0.52, 0.9)]:
            layout.add_widget(self._make_button(name, x, y, w=0.1, h=0.06, font_size="11sp"))

        dpad = [("UP", 0.16, 0.7), ("DOWN", 0.16, 0.54), ("LEFT", 0.08, 0.62), ("RIGHT", 0.24, 0.62)]
        for name, x, y in dpad:
            layout.add_widget(self._make_button(name[0], x, y, w=0.08, h=0.06,
                                                  bit_name=name, font_size="13sp"))

        switch_btn = RoundButton(text="RACING MODE", font_size="12sp",
                                  size_hint=(0.36, 0.055),
                                  pos_hint={"center_x": 0.5, "y": 0.93},
                                  accent=(0.85, 0.45, 0.15, 1),
                                  on_release_cb=self._switch_mode)
        layout.add_widget(switch_btn)

        self.add_widget(layout)
        Clock.schedule_interval(self._tick, 1 / 60.0)

    def _make_button(self, label, x, y, w=0.1, h=0.09, bit_name=None, font_size="15sp"):
        bit_name = bit_name or label
        return RoundButton(
            text=label, size_hint=(w, h), pos_hint={"x": x, "y": y},
            font_size=font_size,
            on_press_cb=lambda: self._set_bit(bit_name, True),
            on_release_cb=lambda: self._set_bit(bit_name, False),
        )

    def _set_bit(self, name, down):
        bit = BUTTON_BITS[name]
        if down:
            self.pressed |= (1 << bit)
        else:
            self.pressed &= ~(1 << bit)

    def _switch_mode(self):
        if self.manager:
            self.manager.current = "racing"

    def _tick(self, _dt):
        if not (self.manager and self.manager.current == self.name):
            return
        self.net.send_input(
            self.pressed,
            self.left_stick.x_val, self.left_stick.y_val,
            self.right_stick.x_val, self.right_stick.y_val,
            0.0, 0.0,
        )


class RacingScreen(Screen):
    """FH6-mapped layout: steering wheel + gas/brake pedals + buttons.
    Gas = RT, Brake = LT, Handbrake = A, Camera = RB, Rewind = Y,
    Gear Up = B, Gear Down = X (classic Forza manual-shift convention —
    double check in-game control settings, since manual shift isn't
    bound by default until you switch off automatic)."""

    def __init__(self, net, **kwargs):
        super().__init__(**kwargs)
        self.net = net
        self.pressed = 0
        layout = FloatLayout()

        self.wheel = SteeringWheel(size_hint=(None, None), size=(260, 260),
                                    pos_hint={"x": 0.02, "y": 0.10})
        layout.add_widget(self.wheel)

        self.brake = PedalSlider(label="BRAKE", color=(0.82, 0.24, 0.24, 1),
                                  size_hint=(None, None), size=(64, 210),
                                  pos_hint={"right": 0.80, "y": 0.14})
        self.gas = PedalSlider(label="GAS", color=(0.24, 0.72, 0.38, 1),
                                size_hint=(None, None), size=(64, 210),
                                pos_hint={"right": 0.97, "y": 0.14})
        layout.add_widget(self.brake)
        layout.add_widget(self.gas)

        layout.add_widget(self._make_button(
            "E-BRAKE", "A", 0.03, 0.82, w=0.16, h=0.09,
            accent=(0.82, 0.28, 0.28, 1)))
        layout.add_widget(self._make_button(
            "CAMERA", "RB", 0.80, 0.82, w=0.16, h=0.08,
            accent=(0.30, 0.47, 0.95, 1)))
        layout.add_widget(self._make_button(
            "REWIND", "Y", 0.80, 0.68, w=0.16, h=0.08,
            accent=(0.85, 0.55, 0.15, 1)))
        layout.add_widget(self._make_button(
            "GEAR +", "B", 0.42, 0.60, w=0.14, h=0.09,
            accent=(0.35, 0.36, 0.42, 1)))
        layout.add_widget(self._make_button(
            "GEAR -", "X", 0.42, 0.46, w=0.14, h=0.09,
            accent=(0.35, 0.36, 0.42, 1)))

        switch_btn = RoundButton(text="GAMEPAD MODE", font_size="12sp",
                                  size_hint=(0.36, 0.055),
                                  pos_hint={"center_x": 0.5, "y": 0.93},
                                  accent=(0.30, 0.47, 0.95, 1),
                                  on_release_cb=self._switch_mode)
        layout.add_widget(switch_btn)

        self.add_widget(layout)
        Clock.schedule_interval(self._tick, 1 / 60.0)

    def _make_button(self, label, bit_name, x, y, w, h, accent):
        return RoundButton(
            text=label, size_hint=(w, h), pos_hint={"x": x, "y": y},
            font_size="12sp", accent=accent,
            on_press_cb=lambda: self._set_bit(bit_name, True),
            on_release_cb=lambda: self._set_bit(bit_name, False),
        )

    def _set_bit(self, name, down):
        bit = BUTTON_BITS[name]
        if down:
            self.pressed |= (1 << bit)
        else:
            self.pressed &= ~(1 << bit)

    def _switch_mode(self):
        if self.manager:
            self.manager.current = "controller"

    def _tick(self, _dt):
        if not (self.manager and self.manager.current == self.name):
            return
        self.net.send_input(
            self.pressed,
            self.wheel.value, 0.0,
            0.0, 0.0,
            self.brake.value, self.gas.value,
        )


class ControllerApp(App):
    def build(self):
        self.net = Net()
        sm = ScreenManager()
        sm.add_widget(ConnectScreen(self.net, self._go_to_controller, name="connect"))
        sm.add_widget(ControllerScreen(self.net, name="controller"))
        sm.add_widget(RacingScreen(self.net, name="racing"))
        self.sm = sm
        return sm

    def _go_to_controller(self):
        self.sm.current = "controller"


if __name__ == "__main__":
    ControllerApp().run()
