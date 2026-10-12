"""From the repository root: python3 -m unittest -v. No remote needed: tests/sample.jsonl is a real capture
(gen-1 Siri Remote A1962, guided steps; report bytes, relative time and step labels only, no audio). The voice tests
use synthetic frames only; the ones that need libopus skip without it, and the Linux ones skip elsewhere."""
import array
import contextlib
import ctypes
import errno
import io
import json
import math
import os
import random
import select
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest
import wave
from unittest import mock

from appleremote import device, gestures, hcitap, voice
from appleremote.__main__ import main, pump
from appleremote.decode import Decoder
from appleremote.device import REMOTE, hidiocsfeature, replay
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

    def test_hci_ioctl_numbers(self):  # _IOR('H', 210 | 212, int), asm-generic layout
        self.assertEqual((hcitap.HCIGETDEVLIST, hcitap.HCIGETCONNLIST), tuple(2 << 30 | 4 << 16 | ord("H") << 8 | nr
                                                                               for nr in (210, 212)))


# The voice: HCI monitor packets made here, as the remote and other devices send them. Never a recorded voice.
R, O = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"  # the remote, another device
END = b"\x10\x00"
rng = random.Random(1)


def rbytes(n):
    return bytes(rng.getrandbits(8) for _ in range(n))


def bdaddr(a):
    return bytes.fromhex(a.replace(":", ""))[::-1]


def evt(code, prm, idx=0):  # an HCI event as the monitor shows it: opcode 3, index, length; code, length, parameters
    return struct.pack("<HHHBB", 3, idx, 2 + len(prm), code, len(prm)) + prm


def le_conn(h, addr=R, idx=0, sub=0x0A, status=0):  # LE Connection Complete (0x01), Enhanced v1 (0x0A), v2 (0x29)
    return evt(0x3E, bytes([sub, status]) + struct.pack("<H", h) + b"\0\0" + bdaddr(addr) +
               bytes({0x01: 7, 0x0A: 19, 0x29: 22}[sub]), idx)


def br_conn(h, addr=O):  # BR/EDR Connection Complete: status, handle, address, link type ACL, no encryption
    return evt(0x03, b"\0" + struct.pack("<H", h) + bdaddr(addr) + b"\x01\0")


def disconn(h):
    return evt(0x05, b"\0" + struct.pack("<H", h) + b"\x13")


def enc(h, on=1, status=0, code=0x08, idx=0):  # Encryption Change v1 (0x08), v2 (0x59: a key size more)
    return evt(code, bytes([status]) + struct.pack("<H", h) + bytes([on]) + (b"\x10" if code == 0x59 else b""), idx)


def acl(payload, cid=4, handle=0x200, idx=0, frag=27):
    """An L2CAP frame arriving as the monitor shows it: ACL RX packets of at most `frag` bytes (27: LE without Data
    Length Extension, as from the remote)."""
    f = struct.pack("<HH", len(payload), cid) + payload
    return [struct.pack("<HHHHH", 5, idx, 4 + len(c), handle | (0x1000 if i else 0x2000), len(c)) + c
            for i, c in ((i, f[i:i + frag]) for i in range(0, len(f), frag))]


def notify(v, handle=0x200, idx=0, frag=27, head=hcitap.ATT_NOTIFY, cid=4):  # v notified on 0x0023
    return acl(head + v, cid, handle, idx, frag)


def frame(seq, pkt=None, held=True, level=2260):
    """A 101-byte voice value in the remote's layout: header, an Opus packet (default: 80 made-up bytes), filler."""
    pkt = pkt or bytes([0xB8]) + rbytes(79)
    return bytes([0x10, 0x10 if held else 0, level & 255, level >> 8 & 255, seq & 255, seq >> 8, len(pkt)]) + pkt + \
        rbytes(94 - len(pkt))


def run(tap, packets):
    return [v for v in map(tap.packet, packets) if v is not None]


def snoop(path, packets, dt=.005):
    """Monitor packets -> a btmon -w file, dt s apart."""
    with open(path, "wb") as f:
        f.write(b"btsnoop\0" + struct.pack(">II", 1, 2001))
        for i, p in enumerate(packets):
            op, idx, n = struct.unpack_from("<HHH", p)
            f.write(struct.pack(">IIIIq", n, n, idx << 16 | op, 0, 0x00DCDDB30F2F8000 + round(i * dt * 1e6)) + p[6:])


def quiet():
    return contextlib.redirect_stderr(io.StringIO())


def connect(path):
    """A client of the tap serving on `path`, once it listens (Linux: SOCK_SEQPACKET)."""
    for _ in range(500):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        c.settimeout(5)
        try:
            c.connect(path)
            return c
        except (FileNotFoundError, ConnectionRefusedError):
            c.close()
            time.sleep(.01)
    raise AssertionError("nothing listens on " + path)


class HciTap(unittest.TestCase):
    def tap(self, remote=R, links=()):
        return hcitap.Tap(remote, links, info=lambda s: None)

    def test_reassembly(self):
        f = [frame(i) for i in range(4)]
        self.assertEqual([len(p) for p in notify(f[0])], [6 + 4 + 27] * 4)  # 108 bytes of L2CAP, as on the air
        t = self.tap()
        self.assertEqual(run(t, [le_conn(0x200), enc(0x200)] + notify(f[0]) + notify(END)), [f[0], END])
        # one fragment (with Data Length Extension), and 3-byte ones that split the L2CAP header; a start flagged PB 00
        # or 11 is a start too, as in the kernel
        one = notify(f[2], frag=251)[0]
        pb = [one[:7] + bytes([one[7] & 0xCF | b << 4]) + one[8:] for b in (0, 3)]
        self.assertEqual(run(t, notify(f[0], frag=251) + notify(f[1], frag=3) + pb), [f[0], f[1], f[2], f[2]])
        # a new start replaces an unfinished frame; a frame too long, a rest without its start and malformed packets
        # are dropped, and the remote's next frame still comes out
        l2 = struct.pack("<HH", 104, 4) + hcitap.ATT_NOTIFY + f[0] + b"xx"
        over = [struct.pack("<HHHHH", 5, 0, 4 + len(c), 0x200 | b, len(c)) + c
                for b, c in ((0x2000, l2[:55]), (0x1000, l2[55:]))]
        bad = [b"", b"\x05\0\0\0", struct.pack("<HHH", 5, 0, 50) + bytes(10), struct.pack("<HHH", 5, 0, 2) + b"\0\x02",
               evt(0x3E, b"\x0a\0"), evt(0x05, b""), b"\x03\0\0\0\x01\0\x3e",
               struct.pack("<HHH", 5, 0, 4) + struct.pack("<HH", 0x200, 9)]
        bad += [p[:-5] for p in notify(f[2])] + over + notify(f[2])[1:]
        self.assertEqual(run(t, notify(f[0])[:2] + notify(f[1]) + bad + notify(f[3])), [f[1], f[3]])
        self.assertEqual(run(t, over + notify(f[2])[1:]), [])
        self.assertTrue(all(len(b) <= 108 for b in t.part.values()))  # what it holds is bounded
        with self.assertRaises(ValueError):
            hcitap.Tap("AA:BB:CC:DD:EE")
        self.assertEqual(hcitap.HID_ID, REMOTE)

    def test_only_the_remote_s_voice(self):
        f = [frame(i) for i in range(2)]
        t = self.tap()
        run(t, [le_conn(0x200), enc(0x200), le_conn(0x201, O), enc(0x201)])
        # another device (its address known, its link encrypted) sends a look-alike on 0x0023, and a link nobody
        # announced a 104-byte frame on a dynamic channel, between the remote's fragments: only the remote's comes out
        mixed = zip(notify(f[0]), notify(f[1], 0x201), acl(bytes(100), 0x40, 0x202))
        self.assertEqual(run(t, [p for trio in mixed for p in trio]), [f[0]])
        self.assertEqual(run(t, notify(END, 0x201) + notify(f[1], 0x201, frag=251)), [])
        # from the remote, all that isn't its voice: other channels, opcodes and handles; what hidraw gets (20 bytes); a
        # touch report; a button report; another kind; another TOC; 102 bytes; its own frames as sent (ACL TX)
        junk = [notify(f[0], cid=6), notify(f[0], head=b"\x1d\x23\x00"), notify(f[0], head=b"\x1b\x28\x00"),
                notify(f[0][:22], head=b"\x0b"), notify(f[0][:20]), notify(bytes.fromhex("0100320c340187acef42611584")),
                notify(b"\x00\x10"), notify(b"\x11" + f[0][1:]), notify(f[0][:7] + b"\xb9" + f[0][8:]),
                notify(f[0] + b"\0"), [b"\x04" + p[1:] for p in notify(f[0])]]
        self.assertEqual(run(t, [p for j in junk for p in j]), [])
        self.assertFalse(t.part)

    def test_encryption(self):
        # the remote's link counts from its Encryption Change (v1 or v2) on: a borrowed address can't make one; a failed
        # one, or encryption off, drops its frames again; so does a reconnect, until its own
        f = [frame(i) for i in range(4)]
        self.assertEqual(run(self.tap(), [le_conn(0x200)] + notify(f[0]) + [enc(0x200, status=6)] + notify(f[1]) +
                             [enc(0x200, code=0x59)] + notify(f[2]) + [enc(0x200, on=0)] + notify(f[3]) + [enc(0x200)] +
                             notify(END) + [disconn(0x200), le_conn(0x201)] + notify(f[0], 0x201) +
                             [enc(0x201, idx=1)] + notify(f[1], 0x201) + [enc(0x201)] + notify(f[2], 0x201)),
                         [f[2], END, f[2]])

    def test_links_up_before_the_tap(self):
        # no connect on the monitor: the link is unknown and its voice dropped, however well forged (a frame's layout
        # names nobody), unless the kernel's list names it the remote's, and encrypted. Nothing of another link is held.
        f = [frame(i) for i in range(4)]
        forged = b"\x10\x10\xff\x7f\0\0\x50\xb8" + bytes(93)
        t = self.tap(links=[(0, 0x41, bdaddr(R), True), (0, 0x42, bdaddr(O), True), (0, 0x43, bdaddr(R), False)])
        self.assertEqual((t.link, t.enc), ({(0, 0x41): True, (0, 0x42): False, (0, 0x43): True}, {(0, 0x41), (0, 0x42)}))
        self.assertEqual(run(t, notify(forged, 0x40) + notify(f[0], 0x40) + notify(END, 0x40) + notify(forged, 0x42) +
                             notify(f[1], 0x43) + notify(f[2], 0x41) + [enc(0x43)] + notify(f[3], 0x43)), [f[2], f[3]])
        run(t, notify(forged, 0x42)[:1] + notify(forged, 0x40)[:1])  # other links' start fragments
        self.assertFalse(t.part)
        self.assertNotIn((0, 0x40), t.link)

    def test_connects_and_disconnects(self):
        f = [frame(i) for i in range(3)]
        t = self.tap()
        run(t, [le_conn(0x200), enc(0x200)])
        # the link drops mid-frame: the rest after a reconnect completes nothing; another device that gets its handle
        # isn't the remote; the remote back on a new handle is, from its next whole frame
        self.assertEqual(run(t, notify(f[0])[:2] + [disconn(0x200), le_conn(0x200), enc(0x200)] + notify(f[0])[2:]), [])
        self.assertEqual(run(t, [disconn(0x200), le_conn(0x200, O), enc(0x200)] + notify(f[1]) + notify(END) +
                             [le_conn(0x203), enc(0x203)] + notify(f[2], 0x203)[1:]), [])
        self.assertEqual(run(t, notify(f[2], 0x203) + notify(END, 0x203)), [f[2], END])
        self.assertEqual(t.link, {(0, 0x200): False, (0, 0x203): True})
        self.assertEqual(run(t, [disconn(0x203)] + notify(END, 0x203)), [])  # gone: its handle is nobody's
        self.assertNotIn((0, 0x203), set(t.link) | t.enc)
        # LE Connection Complete and Enhanced v2 count too; a failed connect makes no link; BR/EDR is never the remote;
        # links on two controllers with the same handle are told apart
        t = self.tap()
        run(t, [le_conn(0x10, sub=0x01), le_conn(0x11, sub=0x29), le_conn(0x12, status=0x3E), br_conn(0x13),
                le_conn(0x200), le_conn(0x200, O, idx=1), enc(0x11), enc(0x13), enc(0x200), enc(0x200, idx=1)])
        self.assertEqual(t.link, {(0, 0x10): True, (0, 0x11): True, (0, 0x13): False, (0, 0x200): True,
                                  (1, 0x200): False})
        self.assertEqual(run(t, notify(f[0], 0x13) + notify(f[1], idx=1) + notify(f[2], 0x11) + notify(f[0])),
                         [f[2], f[0]])

    def test_random_packets(self):
        # many shaped like the real thing: nothing raises, and only voice-shaped values come out
        f0, t = frame(0), self.tap()
        run(t, [le_conn(0x200), le_conn(0x201, O), enc(0x200), enc(0x201)])
        shapes = [hcitap.ATT_NOTIFY + f0, hcitap.ATT_NOTIFY + END, hcitap.ATT_NOTIFY + f0[:20], b"\x1b\x23\x00\x00\x10"]
        for i in range(10000):
            k = rng.random()
            if k < .6:
                f = struct.pack("<HH", rng.choice((5, 104, 104, rng.randrange(300))), rng.choice((4, 4, 4, 6)))
                f += rng.choice(shapes)
                j = rng.randrange(len(f))
                c = bytearray(f[j:j + rng.randrange(1, 60)])
                if rng.random() < .3:
                    c[rng.randrange(len(c))] = rng.randrange(256)
                h = rng.choice((0x200, 0x201, 0x40)) | rng.randrange(4) << 12
                p = struct.pack("<HHHHH", 5, rng.randrange(2), 4 + len(c), h, len(c)) + bytes(c)
            elif k < .8:
                p = evt(rng.choice((0x3E, 0x05, 0x03, 0x08, 0x59)), bytes([rng.choice((1, 0x0A, 0))]) +
                        rbytes(rng.randrange(20)))
            else:
                p = rbytes(rng.randrange(40))
            v = t.packet(p)
            self.assertTrue(v is None or v[0] == 0x10 and (len(v) == 2 or len(v) == 101 and v[7] == 0xB8), (i, p.hex()))
        self.assertEqual(run(t, [le_conn(0x300), enc(0x300)] + notify(f0, 0x300)), [f0])

    def test_giving_up_privileges(self):
        # dumpable goes off before the capabilities (no moment when a process of this uid could take the socket); as
        # plain root, with no sudo to go back from, nothing is given up: it refuses
        calls, libc = [], mock.Mock()
        libc.prctl.side_effect = lambda *a: calls.append(("prctl",) + a) or 0
        libc.capset.side_effect = lambda *a: calls.append("capset") or 0
        with mock.patch("ctypes.CDLL", return_value=libc), mock.patch.object(os, "getuid", return_value=1000):
            hcitap.unprivileged()
        self.assertEqual(calls, [("prctl", 4, 0, 0, 0, 0), "capset"])  # PR_SET_DUMPABLE 0
        del calls[:]
        with mock.patch("ctypes.CDLL", return_value=libc), mock.patch.object(os, "getuid", return_value=0), \
                mock.patch.dict(os.environ, {"SUDO_UID": ""}), self.assertRaises(PermissionError) as e:
            hcitap.unprivileged()
        self.assertEqual(calls, [])
        self.assertIn("with sudo", str(e.exception))

    def test_wait(self):
        # without --remote: what the monitor gets until the remote's hidraw node shows up is dropped unparsed, then
        # the kernel's link list, read after that, says which link is the remote's; a closed monitor ends the wait
        feed, mon = socket.socketpair()
        found = iter([None, None, R])
        with feed, mon, mock.patch.object(hcitap, "WAIT", 0), mock.patch.object(hcitap, "address", lambda: next(found)), \
                mock.patch.object(hcitap, "links", lambda: [(0, 0x41, bdaddr(R), True)]):
            for p in [le_conn(0x200), enc(0x200)] + notify(frame(0)):
                feed.send(p)
            t = hcitap.wait(mon, info=lambda s: None)
            self.assertEqual((t.link, t.enc, select.select([mon], [], [], 0)[0]), ({(0, 0x41): True}, {(0, 0x41)}, []))
            feed.close()
            with self.assertRaises(OSError):
                hcitap.wait(mon, info=lambda s: None)


class Voice(unittest.TestCase):
    def test_parse(self):
        pkt = bytes([0xB8]) + bytes(range(1, 80))
        v = frame(0x105, pkt, level=0x1234)
        self.assertEqual((len(v), voice.parse(v)), (101, (0x105, 0x10, 0x1234, pkt)))
        self.assertEqual((voice.parse(b"\x10\x00"), voice.parse(b"\x10\x10")), ((None, 0, 0, b""), (None, 0x10, 0, b"")))
        # what hidraw gets (20 bytes: no whole packet), a touch report, the unlock reply, another TOC, NN 0, NN past the
        # value's end; NN up to it is fine
        for bad in (v[:20], bytes.fromhex("0100320c340187acef42611584"), b"\xe0\x20\x60\x02", v[:7] + b"\xb9" + v[8:],
                    v[:6] + b"\x00" + v[7:], v[:6] + b"\x5f" + v[7:]):
            self.assertIsNone(voice.parse(bad))
        self.assertEqual(voice.parse(v[:6] + b"\x5e" + v[7:])[3], v[7:])

    def test_presses(self):
        def vals(seqs, t=0.0, tail=0):  # a frame each 20 ms; the last `tail` sent with Siri let go
            return [(t + .02 * i, frame(q, held=i < len(seqs) - tail, level=100 * q)) for i, q in enumerate(seqs)]
        # Siri held, then the tail, then the end marker: one press. A 10 10 before any frame (the last press's), other
        # reports and ticks change nothing
        got = list(voice.presses([(0, b"\x10\x10"), (0, b"\x01\x00" + bytes(11)), (0, None)] + vals(range(10), tail=4)
                                 + [(.2, END)]))
        self.assertEqual([(p.seqs, p.held, p.level, p.ended) for p in got], [(list(range(10)), 6, 900, True)])
        # a lost frame is a gap in the packets
        v = vals([0, 1, 3, 4])
        p, = voice.presses(v + [(.1, END)])
        pk = [voice.parse(x)[3] for _, x in v]
        self.assertEqual(p.packets(), pk[:2] + [None] + pk[2:])
        # Siri pressed again: 10 10, or no end marker and seq starting over (at 1 when that frame was lost); the frame
        # that starts over is the next press's first; the values' end ends the last one
        for between, first in (([(.04, b"\x10\x10")], 0), ([], 0), ([], 1), ([], 7)):
            got = list(voice.presses(vals([5, 9]) + between + vals([first, first + 1], t=.06)))
            self.assertEqual([(p.seqs, p.ended) for p in got], [([5, 9], bool(between)), ([first, first + 1], False)])
        # no end marker and no more frames: over after GAP s, ticks included, without waiting for more values
        src = iter(vals(range(3)) + [(.3, None), (.6, None), (.7, None)])
        self.assertEqual(next(voice.presses(src)).seqs, [0, 1, 2])
        self.assertEqual(list(src), [(.7, None)])
        # a minute at most: a longer press goes on in the next one
        p, q = voice.presses(vals([9, 8 + voice.MAX_FRAMES, 9 + voice.MAX_FRAMES]))
        self.assertEqual((len(p.packets()), q.seqs), (voice.MAX_FRAMES, [9 + voice.MAX_FRAMES]))

    def test_wav(self):
        pcm, f = array.array("h", range(-1000, 1000, 7)).tobytes(), io.BytesIO()
        voice.write_wav(f, pcm)
        b = f.getvalue()
        self.assertEqual(struct.unpack("<4sI4s4sIHHIIHH4sI", b[:44]), (b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1, 1,
                                                                       16000, 32000, 2, 16, b"data", len(pcm)))
        self.assertEqual(b[44:], pcm)

    def test_replay(self):
        # btmon -w begun with the remote connected (no connect event): a re-grip's blip (3 frames, then 10 10), then a
        # press (6 frames held, 2 of tail, its end marker); between them, another device's look-alike frames
        a, b = [frame(q) for q in range(3)], [frame(q, held=q < 6) for q in range(8)]
        mon = [p for v in a + [b"\x10\x10"] for p in notify(v)] + [le_conn(0x201, O), enc(0x201)] + notify(frame(0), 0x201)
        mon += [p for v in b + [END] for p in notify(v)]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.snoop")
            snoop(path, mon)
            got = list(voice.btsnoop(path))
            self.assertEqual(([p for _, p in got], got[1][0]), (mon, .005))
            self.assertEqual([v for _, v in voice.replay(path, info=lambda s: None)], a + [b"\x10\x10"] + b + [END])
            with contextlib.redirect_stdout(io.StringIO()) as out, quiet():
                main(["voice", "--replay", path])
            self.assertEqual([json.loads(x) for x in out.getvalue().splitlines()], [
                {"kind": "press", "wav": None, "frames": 3, "lost": 0, "held": 3, "seconds": .06, "level": 2260,
                 "ended": True},
                {"kind": "press", "wav": None, "frames": 8, "lost": 0, "held": 6, "seconds": .16, "level": 2260,
                 "ended": True}])
            # the remote connected during the capture: its address says which link is its
            snoop(path, [le_conn(0x200), enc(0x200)] + mon)
            with self.assertRaises(SystemExit) as e, quiet():
                main(["voice", "--replay", path])
            self.assertIn("give --remote", str(e.exception.code))
            with contextlib.redirect_stdout(io.StringIO()) as out, quiet():
                main(["voice", "--replay", path, "--remote", R])
            self.assertEqual(len(out.getvalue().splitlines()), 2)
            with open(path, "r+b") as f:
                f.write(b"btsnoop\0" + struct.pack(">II", 1, 1002))  # an H4 capture, not btmon's
            with self.assertRaises(ValueError):
                next(voice.btsnoop(path))

    def test_without_libopus(self):
        with mock.patch.object(voice, "_lib", None), mock.patch.object(voice.ctypes, "CDLL", side_effect=OSError("no")):
            with self.assertRaises(RuntimeError) as e:
                voice.libopus()
            self.assertIn("libopus0", str(e.exception))
            with tempfile.TemporaryDirectory() as d, self.assertRaises(SystemExit) as e:
                main(["voice", "--replay", os.path.join(d, "c.snoop"), "--out", d])
            self.assertIn("libopus0", str(e.exception.code))

    def test_saves_private_wavs(self):
        # a WAV per press, yours alone (decode mocked: no libopus needed); none for a press with no frame held, as
        # what's left of a Siri press that woke the remote
        with tempfile.TemporaryDirectory() as d:
            cap, out = os.path.join(d, "c.snoop"), os.path.join(d, "wav")
            snoop(cap, [p for v in [frame(q, held=False) for q in range(6, 10)] + [END] + [frame(q) for q in range(5)]
                        + [END] for p in notify(v)])
            with mock.patch.object(voice, "libopus", lambda: None), contextlib.redirect_stdout(io.StringIO()) as o, \
                    mock.patch.object(voice, "decode", lambda pk: bytes(2 * voice.FRAME * len(pk))):
                main(["voice", "--replay", cap, "--out", out])
            got = [json.loads(x) for x in o.getvalue().splitlines()]
            self.assertEqual([(g["held"], g["wav"]) for g in got], [(0, None), (5, os.path.join(out, "c-02.wav"))])
            self.assertEqual([stat.S_IMODE(os.stat(p).st_mode) for p in (out, got[1]["wav"])], [0o700, 0o600])


def encode(pcm, bitrate=32000, cap=94):
    """16 kHz mono PCM -> Opus packets as the remote makes them: CELT only (RESTRICTED_LOWDELAY), wideband, 20 ms, VBR
    around `bitrate` (the remote's NN averages 75 to 80 bytes), at most `cap` bytes."""
    lib, err, vp, i = voice.libopus(), ctypes.c_int(), ctypes.c_void_p, ctypes.c_int
    for fn, res, args in (("opus_encoder_create", vp, [ctypes.c_int32, i, i, ctypes.POINTER(i)]),
                          ("opus_encode", ctypes.c_int32, [vp, ctypes.POINTER(ctypes.c_int16), i, ctypes.c_char_p,
                                                           ctypes.c_int32]),
                          ("opus_encoder_ctl", i, [vp, i]),  # variadic: the fixed arguments only
                          ("opus_encoder_destroy", None, [vp])):
        f = getattr(lib, fn)
        f.restype, f.argtypes = res, args
    enc = lib.opus_encoder_create(voice.RATE, 1, 2051, ctypes.byref(err))  # OPUS_APPLICATION_RESTRICTED_LOWDELAY
    assert not err.value and enc, err.value
    pcm, buf, out = bytes(pcm) + bytes(-len(pcm) % (2 * voice.FRAME)), ctypes.create_string_buffer(cap), []
    try:
        lib.opus_encoder_ctl(enc, 4002, ctypes.c_int32(bitrate))  # OPUS_SET_BITRATE_REQUEST
        for o in range(0, len(pcm), 2 * voice.FRAME):
            n = lib.opus_encode(enc, (ctypes.c_int16 * voice.FRAME).from_buffer_copy(pcm, o), voice.FRAME, buf, cap)
            assert n > 0, n
            out.append(buf.raw[:n])
    finally:
        lib.opus_encoder_destroy(enc)
    return out


try:
    voice.libopus()
    OPUS = True
except RuntimeError:
    OPUS = False


@unittest.skipUnless(OPUS, "libopus is not installed")
class Opus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):  # a voiced-like second (two harmonics and a sweep) -> packets like the remote's
        w = 2 * math.pi / voice.RATE
        cls.src = array.array("h", (int(6000 * math.sin(w * 220 * t) + 3000 * math.sin(w * 660 * t) +
                                        2000 * math.sin(w * (300 + 2.5 * t / 16) * t)) for t in range(voice.RATE)))
        cls.pk = encode(cls.src.tobytes())

    @staticmethod
    def corr(a, b, lag=40):  # at libopus's delay, 2.5 ms
        x, y = a[1600:-1600], b[1600 + lag:len(b) - 1600 + lag]
        return sum(p * q for p, q in zip(x, y)) / math.sqrt(sum(p * p for p in x) * sum(q * q for q in y))

    def test_round_trip(self):
        # values -> fragments, with another device's traffic between -> the tap -> a press -> PCM that matches
        self.assertTrue(len(self.pk) == 50 and all(p[0] == 0xB8 and 2 < len(p) <= 94 for p in self.pk))
        mon = [le_conn(0x200), enc(0x200), br_conn(0x50)]
        for v in [frame(q, p) for q, p in enumerate(self.pk)] + [END]:
            for p in notify(v):
                mon += [p] + acl(rbytes(79), 0x45, 0x50, frag=1021)
        press, = voice.presses((i * .005, v) for i, v in enumerate(run(hcitap.Tap(R, info=lambda s: None), mon)))
        out = array.array("h", voice.decode(press.packets()))
        self.assertEqual(len(out), 50 * voice.FRAME)
        self.assertGreater(self.corr(self.src, out), .9)

    def test_concealment(self):
        # three frames lost, and one libopus refuses (code 3, no frames): concealed, so the length and the rest hold
        clean, pk = array.array("h", voice.decode(self.pk)), list(self.pk)
        pk[20:23], pk[30] = [None] * 3, b"\xbb\x00"
        out = array.array("h", voice.decode(pk))
        self.assertEqual(len(out), len(clean))
        self.assertGreater(max(map(abs, out[20 * voice.FRAME:23 * voice.FRAME])), 0)
        self.assertEqual(out[:20 * voice.FRAME], clean[:20 * voice.FRAME])
        self.assertGreater(self.corr(self.src[:19 * voice.FRAME], out[:19 * voice.FRAME]), .9)

    def test_saves_wavs(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.snoop")
            snoop(path, [p for v in [frame(q, p) for q, p in enumerate(self.pk[:10])] + [END] for p in notify(v)])
            with contextlib.redirect_stdout(io.StringIO()) as out:
                main(["voice", "--replay", path, "--out", os.path.join(d, "wav")])
            wav = json.loads(out.getvalue())["wav"]
            self.assertEqual(wav, os.path.join(d, "wav", "c-01.wav"))
            with wave.open(wav) as w:
                self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()),
                                 (16000, 1, 2, 10 * voice.FRAME))
                self.assertEqual(w.readframes(10 * voice.FRAME), voice.decode(self.pk[:10]))


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
class LinuxTap(unittest.TestCase):
    def test_unprivileged(self):
        # in a child (as root: as if sudo ran it for nobody): no capability left, not dumpable, and the monitor can't
        # be opened: EPERM, or no Bluetooth here; never EINVAL, which would mean a wrong sockaddr_hci
        pid = os.fork()
        if pid == 0:
            code = 9
            try:
                if os.getuid() == 0:
                    os.environ.update(SUDO_UID="65534", SUDO_GID="65534", SUDO_USER="nobody")
                hcitap.unprivileged()
                with open("/proc/self/status") as f:
                    st = f.read()
                ok = [os.getuid() != 0, all("%s:\t0000000000000000" % k in st for k in ("CapPrm", "CapEff", "CapAmb")),
                      ctypes.CDLL(None).prctl(3, 0, 0, 0, 0) == 0]  # PR_GET_DUMPABLE
                try:
                    hcitap.monitor().close()
                    ok.append(False)
                except PermissionError:
                    ok.append(True)
                except (OSError, AttributeError) as e:
                    ok.append(getattr(e, "errno", 0) != errno.EINVAL)
                code = ok.index(False) + 1 if False in ok else 0  # which check failed
            finally:
                os._exit(code)
        self.assertEqual(os.waitpid(pid, 0)[1] >> 8, 0)

    def test_live(self):  # the CLI's live path, a socketpair standing in for the monitor (nothing drops privileges here)
        feed, mon = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        order = []

        def address():  # the remote shows up, and only then sends: 3 frames and an end marker
            order.append("look")
            for p in [le_conn(0x200), enc(0x200)] + [p for v in [frame(q) for q in range(3)] + [END] for p in notify(v)]:
                feed.send(p)
            feed.close()
            return R
        with contextlib.ExitStack() as stack:
            for obj, name, fake in ((hcitap, "monitor", lambda: order.append("open") or mon),
                                    (hcitap, "unprivileged", lambda: order.append("drop")), (hcitap, "address", address),
                                    (hcitap, "links", lambda: []),
                                    (device, "keep_unlocked", lambda info: order.append("unlock"))):
                stack.enter_context(mock.patch.object(obj, name, fake))
            out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(quiet())
            with self.assertRaises(SystemExit) as e:
                main(["voice", "--remote", "AA:BB:CC"])
            self.assertEqual((order, "not a Bluetooth address" in str(e.exception.code)), ([], True))  # nothing opened
            with self.assertRaises(SystemExit) as e:
                main(["voice"])
        self.assertIn("monitor socket closed", str(e.exception.code))
        self.assertEqual(json.loads(out.getvalue())["frames"], 3)
        # the monitor opens and every privilege goes before it waits for the remote; the thread that unlocks the remote
        # starts only then
        self.assertEqual(order, ["open", "drop", "unlock", "look"])

    def test_links(self):  # the kernel's list, as a user, or no Bluetooth here; never ENOTTY (a wrong ioctl)
        try:
            got = hcitap.links()
        except (OSError, AttributeError) as e:
            self.assertIn(getattr(e, "errno", errno.EAFNOSUPPORT), (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT))
        else:
            self.assertTrue(all(len(x) == 4 and len(x[2]) == 6 and x[3] in (True, False) for x in got), got)

    def test_serve(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, mark, f = os.path.join(tmp, "run", "voice.sock"), [0x700], [frame(i) for i in range(3)]
            feed, mon = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)  # stands in for the monitor socket
            tap = hcitap.Tap(R, info=lambda s: None)

            def serve(mon, tap, path, uid):  # as serve_forever() does, with a Tap made here
                with hcitap.listening(path, uid) as lst:
                    hcitap.serve(mon, tap, lst, uid)
            th = threading.Thread(target=serve, args=(mon, tap, path, os.getuid()), daemon=True)
            th.start()

            def say(*packets):
                for p in packets:
                    feed.send(p)

            def handled():  # every packet said so far has been read and parsed
                mark[0] += 1
                say(br_conn(mark[0]))
                for _ in range(500):
                    if (0, mark[0]) in tap.link:
                        return
                    time.sleep(.01)
                self.fail("serve() stalled")

            def refused(p=path):  # it connects, then the tap closes it at once
                with connect(p) as c:
                    return c.recv(200) == b""

            say(le_conn(0x200), enc(0x200), *notify(f[0]))
            handled()  # no client then: dropped, not kept for later
            a = connect(path)
            self.assertEqual((stat.S_IMODE(os.stat(path).st_mode), stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)),
                             (0o600, 0o700))
            say(*notify(END))
            self.assertEqual(a.recv(200)[8:], END)
            b, t0 = connect(path), time.monotonic_ns()
            say(*notify(f[1]))
            for c in (a, b):  # both; b nothing from before it connected
                m = c.recv(200)
                self.assertEqual(m[8:], f[1])
                self.assertTrue(t0 <= struct.unpack("<Q", m[:8])[0] <= time.monotonic_ns())
            s, c = connect(path), connect(path)  # 4 clients: a fifth is refused
            self.assertTrue(refused())
            for _ in range(2000):  # s never reads: once its queue is full it is dropped; the others get every message
                say(*notify(END))
                self.assertEqual([x.recv(200)[8:] for x in (a, b, c)], [END] * 3)
            n = 0
            while s.recv(200):  # what was queued for s, then its end
                n += 1
            self.assertTrue(0 < n < 2000, n)
            a.send(b"x")  # a client that sends is dropped (closed with that unread: a reset)
            with self.assertRaises(ConnectionResetError):
                a.recv(200)
            x = voice.client(path, idle=.05)  # a place is free again: voice.client() takes it
            self.assertIsNone(next(x)[1])
            say(*[p for v in [frame(q) for q in range(3)] + [END] for p in notify(v)])
            p = next(voice.presses(x))
            self.assertEqual((p.seqs, p.ended), ([0, 1, 2], True))
            f2, m2 = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            p2 = os.path.join(tmp, "other.sock")
            th2 = threading.Thread(target=serve, args=(m2, hcitap.Tap(R, info=lambda s: None), p2, os.getuid() + 1),
                                   daemon=True)
            th2.start()
            self.assertTrue(refused(p2))  # for another uid's processes (the unit's): this one is refused,
            self.assertEqual(stat.S_IMODE(os.stat(p2).st_mode), 0o666)  # though the socket lets anyone connect
            f2.close()
            feed.close()  # the monitor closed: serve() ends, and the socket is removed
            for t in (th, th2):
                t.join(5)
                self.assertFalse(t.is_alive())
            self.assertFalse(os.path.exists(path) or os.path.exists(p2))
            x.close()
            for y in (a, b, c, s, mon, m2):
                y.close()
            keep = os.path.join(tmp, "keep")  # a file that isn't a socket is never replaced, nor removed
            open(keep, "w").close()
            with self.assertRaises(OSError), hcitap.listening(keep, os.getuid()):
                pass
            self.assertTrue(os.path.isfile(keep))

    def test_serve_forever(self):
        # the socket is there before the remote is (a client waits with the tap), what the monitor gets meanwhile is
        # dropped, and once the remote's hidraw node shows up its voice comes through; the socket goes with the monitor
        with tempfile.TemporaryDirectory() as tmp:
            path, there, up, looks = os.path.join(tmp, "voice.sock"), threading.Event(), threading.Event(), []
            feed, mon = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            f = [frame(i) for i in range(2)]

            def address():
                looks.append(1)
                return R if there.is_set() else None

            def run():
                with contextlib.suppress(OSError):  # (its end: the monitor closed)
                    hcitap.serve_forever(mon, None, path, os.getuid(), lambda s: s.startswith("the remote is") and up.set())
            with contextlib.ExitStack() as stack:
                for name, fake in (("WAIT", .01), ("address", address), ("links", lambda: [(0, 0x41, bdaddr(R), True)]),
                                   ("signal", mock.Mock())):  # (a thread can't set a signal handler)
                    stack.enter_context(mock.patch.object(hcitap, name, fake))
                th = threading.Thread(target=run, daemon=True)
                th.start()
                c = connect(path)
                for p in [le_conn(0x200), enc(0x200)] + notify(f[0]):
                    feed.send(p)
                n, end = len(looks), time.monotonic() + 5
                while len(looks) < n + 2 and time.monotonic() < end:  # a look begun after these were sent, then a
                    time.sleep(.01)                                      # drop: they are gone
                there.set()
                self.assertTrue(up.wait(5))
                for p in notify(f[1], 0x41):
                    feed.send(p)
                self.assertEqual(c.recv(200)[8:], f[1])
                feed.close()
                th.join(5)
            self.assertFalse(th.is_alive() or os.path.exists(path))
            c.close()
            mon.close()


if __name__ == "__main__":
    unittest.main()
