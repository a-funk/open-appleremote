"""Optional: the remote as a keyboard, through a virtual uinput device (Linux), so it works in any app.

Slides and flicks tap the arrow keys; buttons press and release keys. Override any of DEFAULT with a JSON file:
{"click": "KEY_SPACE", "siri": "KEY_C", "tv": null}. Values are KEY_ names from NAMES, or numbers from
linux/input-event-codes.h; null leaves that input unmapped.
"""
import fcntl
import os
import struct

DEFAULT = {"up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT", "click": "KEY_ENTER",
           "menu": "KEY_ESC", "tv": "KEY_HOMEPAGE", "playpause": "KEY_PLAYPAUSE", "volup": "KEY_VOLUMEUP",
           "voldown": "KEY_VOLUMEDOWN", "siri": None}

NAMES = dict(KEY_ESC=1, KEY_BACKSPACE=14, KEY_TAB=15, KEY_ENTER=28, KEY_SPACE=57, KEY_HOME=102, KEY_UP=103,
             KEY_PAGEUP=104, KEY_LEFT=105, KEY_RIGHT=106, KEY_END=107, KEY_DOWN=108, KEY_PAGEDOWN=109, KEY_DELETE=111,
             KEY_MUTE=113, KEY_VOLUMEDOWN=114, KEY_VOLUMEUP=115, KEY_POWER=116, KEY_LEFTMETA=125, KEY_STOP=128,
             KEY_MENU=139, KEY_BACK=158, KEY_FORWARD=159, KEY_NEXTSONG=163, KEY_PLAYPAUSE=164, KEY_PREVIOUSSONG=165,
             KEY_HOMEPAGE=172, KEY_SELECT=353, KEY_INFO=358, KEY_CONTEXT_MENU=438, KEY_VOICECOMMAND=582)
NAMES.update(("KEY_" + c, code) for row, first in (("1234567890", 2), ("QWERTYUIOP", 16), ("ASDFGHJKL", 30),
                                                   ("ZXCVBNM", 44)) for code, c in enumerate(row, first))
NAMES.update(("KEY_F%d" % n, code) for n, code in enumerate(list(range(59, 69)) + [87, 88], 1))

EV_SYN, EV_KEY = 0, 1
# ponytail: asm-generic ioctl numbers (x86, ARM, RISC-V); powerpc, mips and sparc differ
UI_SET_EVBIT, UI_SET_KEYBIT = 0x40045564, 0x40045565  # _IOW('U', 100 | 101, int)
UI_DEV_SETUP, UI_DEV_CREATE = 0x405C5503, 0x5501      # _IOW('U', 3, struct uinput_setup), _IO('U', 1)
EVENT = "llHHi"  # struct input_event: timeval (two C longs), type, code, value


def keymap(overrides=None):
    """DEFAULT updated with `overrides` -> {input: key code}. Unknown inputs or keys raise ValueError."""
    m, out = dict(DEFAULT), {}
    m.update(overrides or {})
    for name, key in m.items():
        if name not in DEFAULT:
            raise ValueError("unknown remote input %r; use one of: %s" % (name, ", ".join(DEFAULT)))
        if key is None:
            continue
        code = NAMES.get(key) if isinstance(key, str) else key
        if type(code) is not int or not 0 < code <= 0x2FF:  # KEY_MAX
            raise ValueError("unknown key %r for %r: use a KEY_ name or a number from linux/input-event-codes.h"
                             % (key, name))
        out[name] = code
    return out


class Keyboard:
    """Call it with the event dicts from the CLI's pump (buttons and gestures). fd: for tests; default: a new
    virtual keyboard. Closing the fd (or exiting) removes the device and releases its keys."""

    def __init__(self, keys, fd=None):
        self.keys = keys
        self.fd = self.create(sorted(set(keys.values()))) if fd is None else fd

    @staticmethod
    def create(codes):
        fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        for code in codes:
            fcntl.ioctl(fd, UI_SET_KEYBIT, code)
        fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack("HHHH80sI", 0x06, 0, 0, 1, b"open-appleremote", 0))  # BUS_VIRTUAL
        fcntl.ioctl(fd, UI_DEV_CREATE)
        return fd

    def send(self, code, *values):
        os.write(self.fd, b"".join(struct.pack(EVENT, 0, 0, EV_KEY, code, v) + struct.pack(EVENT, 0, 0, EV_SYN, 0, 0)
                                   for v in values))

    def __call__(self, ev):
        if ev["kind"] == "buttons":
            for name in ev["released"]:
                if name in self.keys:
                    self.send(self.keys[name], 0)
            for name in ev["pressed"]:
                if name in self.keys:
                    self.send(self.keys[name], 1)
        elif ev["kind"] == "gesture" and ev["type"] == "move" and ev["dir"] in self.keys:
            self.send(self.keys[ev["dir"]], 1, 0)
