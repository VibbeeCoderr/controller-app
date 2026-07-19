"""
Android controller client (Kivy). Sends stick/button state to the PC as
small UDP packets, and vibrates the phone when the PC forwards rumble
state back from the game.

This version has a redone UI: layered/shadowed joystick with a spring-back
animation, and custom rounded buttons with a press animation, so it reads
as a real product instead of a placeholder.
"""

import socket
import struct
import threading
import time
import hashlib

from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.widget import Widget
from kivy.uix.textinput import TextInput
from kivy.uix.label import Label
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.graphics import Color, Ellipse, Line, RoundedRectangle
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
# Joystick: concave "socket" base (layered shading for depth), a raised
# knob with its own drop shadow + gloss highlight, and a spring-back
# animation when released instead of an instant snap.
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
            # soft glow ring — brightens while the stick is being held
            self.glow_color = Color(0.30, 0.55, 1, 0)
            self.glow = Ellipse()
            # socket drop shadow (depth cue)
            Color(0, 0, 0, 0.30)
            self.socket_shadow = Ellipse()
            # concave socket — each layer slightly darker toward the center
            Color(0.15, 0.15, 0.18, 1)
            self.base_outer = Ellipse()
            Color(0.115, 0.115, 0.14, 1)
            self.base_mid = Ellipse()
            Color(0.085, 0.085, 0.105, 1)
            self.base_inner = Ellipse()
            Color(1, 1, 1, 0.05)
            self.base_ring = Line(width=1.1)
            # knob drop shadow
            Color(0, 0, 0, 0.4)
            self.knob_shadow = Ellipse()
            # knob body + lighter cap (subtle dome look) + gloss highlight
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
            # spring back with a slight overshoot — feels tactile instead
            # of a robotic snap to center
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

    def _tick(self, _dt):
        self.net.send_input(
            self.pressed,
            self.left_stick.x_val, self.left_stick.y_val,
            self.right_stick.x_val, self.right_stick.y_val,
            0.0, 0.0,
        )


class ControllerApp(App):
    def build(self):
        self.net = Net()
        sm = ScreenManager()
        sm.add_widget(ConnectScreen(self.net, self._go_to_controller, name="connect"))
        self.controller_screen = ControllerScreen(self.net, name="controller")
        sm.add_widget(self.controller_screen)
        self.sm = sm
        return sm

    def _go_to_controller(self):
        self.sm.current = "controller"


if __name__ == "__main__":
    ControllerApp().run()
