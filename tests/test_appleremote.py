"""From the repository root: python3 -m unittest -v. No remote needed: tests/sample.jsonl is a real capture
(gen-1 Siri Remote A1962, guided steps; report bytes, relative time and step labels only, no audio)."""
import contextlib
import io
import json
import os
import struct
import unittest

from appleremote import gestures
from appleremote.__main__ import pump
from appleremote.decode import Decoder
from appleremote.device import hidiocsfeature, replay
from appleremote.keys import EVENT, UI_DEV_CREATE, UI_DEV_SETUP, UI_SET_EVBIT, UI_SET_KEYBIT, Keyboard, keymap

SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.jsonl")


def lines():
    with open(SAMPLE) as f:
        return [json.loads(line) for line in f]


def run_sample():
    """Replay the sample through the decoder and the gesture engine -> [(step number, event dict)]."""
    marks = [(e["t"], int(e["step"].split(":")[0])) for e in lines() if e["kind"] == "mark"]
    now, out = [0.0], []

    def source():
        for t, r in replay(SAMPLE):
            now[0] = t
            yield t, r
    pump(source(), lambda ev: out.append((max(s for t, s in marks if t <= now[0]), ev)), gestures=True)
    return out


class Decode(unittest.TestCase):
    def test_touch_report(self):
        ev, = Decoder()(bytes.fromhex("fc010032e0ba019fcdcd5c81218b"))
        self.assertEqual((ev.t, ev.x, ev.y, ev.down, ev.fingers), (0.0, -609, -804, True, ((-609, -804, 3),)))

    def test_two_fingers(self):
        ev, = Decoder()(bytes.fromhex("fc020032e6d81944ffd51f280a8b5fcbc7313c0f83"))
        self.assertEqual(ev.fingers, ((-188, -673, 3), (-1185, -900, 3)))

    def test_buttons_from_every_report_kind(self):
        d = Decoder()
        self.assertEqual(d(bytes.fromhex("fce0226002"))[0][:3], (0x22, ("volup", "menu"), ()))  # unlock reply
        self.assertEqual(d(bytes.fromhex("fc0020"))[0][:3], (0x20, (), ("volup",)))
        touch_click = d(bytes.fromhex("fc018032e0ba019fcdcd5c81218c"))  # the click rides on touch reports
        self.assertEqual([type(e).__name__ for e in touch_click], ["Buttons", "Touch"])
        self.assertEqual(touch_click[0][:3], (0x80, ("click",), ("menu",)))

    def test_link_drop_releases_everything(self):
        d = Decoder()
        d(bytes.fromhex("fc018032e0ba019fcdcd5c81218c"))
        buttons, touch = d(b"")
        self.assertEqual((buttons.mask, buttons.released, touch.down), (0, ("click",), False))
        self.assertEqual(d(b""), [])

    def test_voice(self):  # synthetic: the layout of docs/PROTOCOL.md with the audio bytes zeroed
        evs = Decoder()(bytes.fromhex("fc1010" "0000" "00" "00" "44" "b8") + bytes(12))
        self.assertEqual([type(e).__name__ for e in evs], ["Buttons", "VoiceChunk"])
        self.assertEqual(evs[0].pressed, ("siri",))

    def test_ignores_other_reports(self):
        self.assertEqual(Decoder()(b"\x01\x02\x03"), [])
        self.assertEqual(Decoder()(None), [])


class Sample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events = run_sample()

    def test_buttons(self):
        def pressed(step):
            return [n for s, ev in self.events if s == step and ev["kind"] == "buttons" for n in ev["pressed"]]
        self.assertEqual(pressed(0)[:2], ["volup", "menu"])  # the unlock reply carries the buttons held at the time
        for step, want in ((10, ["click"] * 3), (11, ["menu"] * 3), (12, ["tv"] * 3), (13, ["playpause"] * 5),
                           (14, ["volup"] * 3), (15, ["voldown"] * 5), (16, ["siri"] * 2)):
            self.assertEqual(pressed(step), want, step)
        # Only the first of the three clicks has a 3-byte button report; byte 2 of the touch reports has them all.
        click_reports = [e for e in lines() if e["kind"] == "report" and len(e["hex"]) == 6 and int(e["hex"][4:], 16) & 0x80]
        self.assertEqual(len(click_reports), 1)

    def test_touches(self):
        touches = [ev for s, ev in self.events if ev["kind"] == "touch"]
        clocks = [int(ev["raw"][10:12] + ev["raw"][8:10], 16) for ev in touches]
        self.assertTrue(any(b < a for a, b in zip(clocks, clocks[1:])))  # the sample holds a clock wrap...
        self.assertTrue(all(b["t"] > a["t"] for a, b in zip(touches, touches[1:])))  # ...and t still only grows
        downs, was = {}, False
        for s, ev in self.events:
            if ev["kind"] == "touch":
                if ev["down"] and not was:
                    downs[s] = downs.get(s, 0) + 1
                was = ev["down"]
        # 3 up swipes, 9 down swipes, 2 small touches + 5 flicks (a hover-only contact doesn't count), 5 taps
        # (one hover doesn't count), a brush + 3 clicks, 4 two-finger touches
        self.assertEqual(downs, {6: 3, 7: 9, 8: 7, 9: 5, 10: 4, 17: 4})

    def test_gestures(self):
        moves, step = {}, 0
        for s, ev in self.events:  # count each move in the step where its touch began (coasting outlives a step)
            if ev["kind"] == "gesture" and ev["type"] == "touch" and ev["down"]:
                step = s
            if ev["kind"] == "gesture" and ev["type"] == "move":
                moves.setdefault(step, []).append(ev["dir"])
        self.assertEqual((set(moves[6]), set(moves[7]), set(moves[8])), ({"up"}, {"down"}, {"right"}))
        self.assertTrue(1 <= len(moves[6]) <= 2 * 3 and 5 <= len(moves[7]) <= 2 * 9)  # at most 2 per swipe
        self.assertTrue(5 <= len(moves[8]) <= 5 * (1 + gestures.COAST_MAX))  # each flick steps, then coasts
        self.assertNotIn(9, moves)   # taps don't move focus
        self.assertNotIn(10, moves)  # neither do clicks

    def test_engine_selftest(self):
        with contextlib.redirect_stdout(io.StringIO()):
            gestures._selftest()


class Kernel(unittest.TestCase):
    def test_ioctl_numbers(self):
        def ioc(direction, kind, nr, size):  # the kernel's _IOC, asm-generic layout
            return direction << 30 | size << 16 | ord(kind) << 8 | nr
        self.assertEqual(hidiocsfeature(2), 0xC0024806)  # HIDIOCSFEATURE(2)
        self.assertEqual((UI_SET_EVBIT, UI_SET_KEYBIT, UI_DEV_CREATE), (ioc(1, "U", 100, 4), ioc(1, "U", 101, 4), ioc(0, "U", 1, 0)))
        self.assertEqual(UI_DEV_SETUP, ioc(1, "U", 3, struct.calcsize("HHHH80sI")))  # sizeof(struct uinput_setup) = 92

    def test_keymap(self):
        m = keymap({"siri": "KEY_C", "tv": None, "click": 57})
        self.assertEqual((m["siri"], m["click"], m["up"], m["menu"]), (46, 57, 103, 1))
        self.assertNotIn("tv", m)
        for bad in ({"jump": "KEY_A"}, {"click": "KEY_NOPE"}, {"click": True}, {"click": [1]}, {"click": 0}):
            with self.assertRaises(ValueError):
                keymap(bad)

    def test_keyboard_writes_input_events(self):
        r, w = os.pipe()
        kb = Keyboard(keymap(), fd=w)
        kb({"kind": "buttons", "mask": 0x80, "pressed": ["click"], "released": []})
        kb({"kind": "buttons", "mask": 0x10, "pressed": ["siri"], "released": ["click"]})  # siri: unmapped
        kb({"kind": "gesture", "type": "move", "dir": "up", "src": "slide"})
        os.close(w)
        data, size = os.read(r, 4096), struct.calcsize(EVENT)
        os.close(r)
        got = [struct.unpack(EVENT, data[i:i + size])[2:] for i in range(0, len(data), size)]
        self.assertEqual(got, [(1, 28, 1), (0, 0, 0), (1, 28, 0), (0, 0, 0), (1, 103, 1), (0, 0, 0), (1, 103, 0), (0, 0, 0)])


if __name__ == "__main__":
    unittest.main()
