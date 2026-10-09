"""Decode the gen-1 Siri Remote's reports (as read from hidraw) into events.

Every report has ID 0xFC. Byte 1 says what follows, byte 2 holds the buttons that are down right now:

    fc 00 BB                                buttons only
    fc NN BB 32 CC CC SS + NN x 7 bytes     NN fingers on the touch surface; CC CC is the remote's clock
    fc 10 BB ...                            audio while Siri is held (research: not decodable through hidraw)

docs/PROTOCOL.md has the full layout, and says what is measured and what is a guess.
"""
from collections import namedtuple

BUTTONS = {0x01: "tv", 0x02: "volup", 0x04: "voldown", 0x08: "playpause", 0x10: "siri", 0x20: "menu", 0x80: "click"}
TICKS = 2000  # the remote's clock, per second
STATES = ("gone", "start", "hover", "make", "touching", "break", "linger", "lifted")  # finger state, low 3 bits

Touch = namedtuple("Touch", "t x y down fingers raw")  # t: s on the remote's clock; x, y, down: the first finger;
#                                                        fingers: ((x, y, state), ...) for every finger
Buttons = namedtuple("Buttons", "mask pressed released raw")  # mask: byte 2; pressed, released: button names
VoiceChunk = namedtuple("VoiceChunk", "raw")


def s12(v):
    return v - 4096 if v >= 2048 else v


def names(bits):
    return tuple(BUTTONS.get(1 << i, "0x%02x" % (1 << i)) for i in range(8) if bits >> i & 1)


class Decoder:
    """Call it with each report, get a list of events. It keeps the clock and the buttons between reports.

    None (no report) gives nothing. b"" means the link dropped: whatever was held is released."""

    def __init__(self):
        self.t, self.clock, self.mask, self.touch = 0.0, None, 0, None

    def __call__(self, r):
        if r is None:
            return []
        r = bytes(r)
        if not r:
            out = [self._buttons(0, r)] if self.mask else []
            if self.touch and self.touch.down:
                out.append(self.touch._replace(down=False, fingers=(), raw=r))
            self.touch = None
            return out
        if len(r) < 3 or r[0] != 0xFC:
            return []
        out = [self._buttons(r[2], r)] if r[2] != self.mask else []
        if r[1] == 0x10:
            out.append(VoiceChunk(r))
        elif r[1] and len(r) >= 14 and r[3] == 0x32:
            clock = r[4] | r[5] << 8
            if self.clock is not None:  # unwrap: 16 bits wrap every 32.768 s; a longer silence loses whole wraps
                self.t += ((clock - self.clock) & 0xFFFF) / TICKS
            self.clock = clock
            n = min(r[1], (len(r) - 7) // 7)  # 7-byte finger records from byte 7: x, x|y, y, 3 size-like bytes, state
            fingers = tuple((s12(f[0] | (f[1] & 15) << 8), s12(f[1] >> 4 | f[2] << 4), f[6] & 7)
                            for f in (r[o:o + 7] for o in range(7, 7 + 7 * n, 7)))
            x, y, state = fingers[0]
            self.touch = Touch(round(self.t, 4), x, y, 3 <= state <= 6, fingers, r)
            out.append(self.touch)
        return out

    def _buttons(self, mask, r):
        old, self.mask = self.mask, mask
        return Buttons(mask, names(mask & ~old), names(old & ~mask), r)
