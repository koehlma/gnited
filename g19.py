#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2015, Maximilian Köhl <mail@koehlma.de>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

__author__ = 'koehlma'
__version__ = '0.0.1'

import enum
import threading

import dbus
import dbus.service

import usb

BUS_NAME = 'de.koehlma.G19'

FRAME_PREAMBLE = [0x10, 0x0f, 0x00, 0x58, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00,
                  0x00, 0x3f, 0x01, 0xef, 0x00, 0x0f]
FRAME_PREAMBLE.extend(range(16, 256))
FRAME_PREAMBLE.extend(range(256))


class ControlKey(enum.IntEnum):
    SETTINGS = 0x01
    BACK = 0x02
    MENU = 0x04

    OK = 0x08

    RIGHT = 0x10
    LEFT = 0x20
    DOWN = 0x40
    UP = 0x80


class GameKey(enum.IntEnum):
    G01 = 0x0001
    G02 = 0x0002
    G03 = 0x0004
    G04 = 0x0008
    G05 = 0x0010
    G06 = 0x0020
    G07 = 0x0040
    G08 = 0x0080
    G09 = 0x0100
    G10 = 0x0200
    G11 = 0x0400
    G12 = 0x0800

    M1 = 0x1000
    M2 = 0x2000
    M3 = 0x4000

    MR = 0x8000

    LIGHT = 0x80000


class LightKeys(enum.IntEnum):
    M1 = 0x80
    M2 = 0x40
    M3 = 0x20

    MR = 0x10


def convert_color(red, green, blue):
    red_bits = int(red * (2 ** 5 - 1) / 255)
    green_bits = int(green * (2 ** 6 - 1) / 255)
    blue_bits = int(blue * (2 ** 5 - 1) / 255)
    return (red_bits << 11) | (green_bits << 5) | blue_bits


class Event():
    def __init__(self):
        self._handlers = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def __isub__(self, handler):
        self._handlers.remove(handler)
        return self

    def fire(self, *arguments, **keywords):
        for handler in self._handlers:
            handler(*arguments, **keywords)


class G19():
    def __init__(self):
        self._device = usb.core.find(idVendor=0x046d, idProduct=0xc229)
        self._device.reset()
        if self._device.is_kernel_driver_active(0):
            self._device.detach_kernel_driver(0)
        if self._device.is_kernel_driver_active(1):
            self._device.detach_kernel_driver(1)
        self._device.set_configuration()
        self._color = (0, 0, 0)
        self._brightness = 100
        self._thread = None
        self._running = False
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._display_keys = dict((key, False) for key in ControlKey)
        self._game_keys = dict((key, False) for key in GameKey)
        self.key_up = Event()
        self.key_down = Event()

    @property
    def color(self):
        return self._color

    @color.setter
    def color(self, color):
        self._color = color
        data = [7] + list(color)
        with self._lock:
            self._device.ctrl_transfer(usb.TYPE_CLASS | usb.RECIP_INTERFACE,
                                       0x09, 0x307, 0x01, data, 10)

    @property
    def brightness(self):
        return self._brightness

    @brightness.setter
    def brightness(self, brightness):
        assert 0 <= brightness <= 100
        self._brightness = brightness
        data = [brightness, 0xe2, 0x12, 0x00, 0x8c, 0x11, 0x00, 0x10, 0x00]
        with self._lock:
            self._device.ctrl_transfer(usb.TYPE_VENDOR | usb.RECIP_INTERFACE,
                                       0x0a, 0, 0, data, 10)

    def light(self, *keys):
        data = [5, 0]
        for key in keys:
            data[1] |= key
        with self._lock:
            self._device.ctrl_transfer(usb.TYPE_CLASS | usb.RECIP_INTERFACE,
                                       0x09, 0x305, 0x01, data, 10)

    def show(self, data):
        frame = []
        for x in range(320):
            for y in range(240):
                index = y * 320 * 3 + x * 3
                red, green, blue = data[index:index + 3]
                color = convert_color(red, green, blue)
                frame.append(color & 0xff)
                frame.append(color >> 8)
        with self._lock:
            self._device.write(2, FRAME_PREAMBLE + frame, 10)

    def _run(self):
        while not self._stopped.wait(0.05):
            try:
                with self._lock:
                    data = self._device.read(0x83, 20, 10)
                if data[0] == 2 and len(data) == 4:
                    keys = data[3] << 16 | data[2] << 8 | data[1]
                    for key in GameKey:
                        if keys & key:
                            if not self._game_keys[key]:
                                self.key_down.fire(key)
                            self._game_keys[key] = True
                        else:
                            if self._game_keys[key]:
                                self.key_up.fire(key)
                            self._game_keys[key] = False
            except usb.USBError:
                pass
            try:
                with self._lock:
                    keys = self._device.read(0x81, 2, 10)[0]
                for key in ControlKey:
                    if keys & key:
                        if not self._display_keys[key]:
                            self.key_down.fire(key)
                        self._display_keys[key] = True
                    else:
                        if self._display_keys[key]:
                            self.key_up.fire(key)
                        self._display_keys[key] = False
            except usb.USBError:
                pass
        self._running = False

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run)
        self._thread.start()

    def stop(self):
        self._stopped.set()
        self._thread.join()


class Service(dbus.service.Object):
    def __init__(self, path=None, bus=None):
        self.bus = bus or dbus.SessionBus()
        self.path = path or '/de/koehlma/G19'
        self.name = dbus.service.BusName(BUS_NAME, self.bus)
        self.g19 = G19()
        self.g19.start()
        self.g19.key_down += self._key_down
        self.g19.key_up += self._key_up
        super().__init__(self.bus, self.path)

    def _key_down(self, key):
        self.key_down(str(key))

    def _key_up(self, key):
        self.key_up(str(key))

    @dbus.service.method(dbus_interface=BUS_NAME, in_signature='yyy')
    def set_color(self, red, green, blue):
        self.g19.color = (red, green, blue)

    @dbus.service.method(dbus_interface=BUS_NAME, out_signature='nnn')
    def get_color(self):
        return self.g19.color

    @dbus.service.method(dbus_interface=BUS_NAME, in_signature='y')
    def set_brightness(self, brightness):
        self.g19.brightness = brightness

    @dbus.service.method(dbus_interface=BUS_NAME, out_signature='n')
    def get_brightness(self):
        return self.g19.brightness

    @dbus.service.method(dbus_interface=BUS_NAME, in_signature='ay')
    def show(self, frame):
        assert len(frame) == 3 * 320 * 240
        self.g19.show(frame)

    @dbus.service.method(dbus_interface=BUS_NAME, in_signature='y')
    def light(self, keys):
        self.g19.light(keys)

    @dbus.service.signal(dbus_interface=BUS_NAME, signature='s')
    def key_down(self, name):
        pass

    @dbus.service.signal(dbus_interface=BUS_NAME, signature='s')
    def key_up(self, name):
        pass

if __name__ == '__main__':
    from dbus.mainloop.glib import DBusGMainLoop

    from gi.repository import GObject as gobject

    loop = gobject.MainLoop()
    DBusGMainLoop(set_as_default=True)
    service = Service(bus=dbus.SystemBus())
    try:
        loop.run()
    except KeyboardInterrupt:
        service.g19.stop()