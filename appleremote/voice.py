"""The Siri Remote's voice: its whole frames (live through hcitap.py, from a socket hcitap.py serves, or from a btmon
capture) -> one Press per press of Siri -> 16 kHz mono PCM through libopus -> a WAV file.

A frame's value, as the remote sends it (docs/PROTOCOL.md, Voice), 101 bytes: [0] 0x10, [1] buttons (0x10: Siri held),
[2:4] level, [4:6] seq (0 at each press, +1 a frame), [6] NN, [7:7+NN] an Opus packet (TOC 0xB8: CELT, 16 kHz, 20 ms,
mono), then stale filler. Two bytes, 10 00 or 10 10, end a stream. The remote goes on for about a second after Siri is
let go (buttons 0x00): that tail is part of the press.

libopus is the one thing here outside the standard library: a system library (libopus0 on Debian and Ubuntu), loaded
through ctypes when something is first decoded. Nothing else in the package needs it.

    from appleremote import voice
    for press in voice.presses(voice.replay("siri.snoop", "AA:BB:CC:DD:EE:FF")):
        voice.write_wav("press.wav", voice.decode(press.packets()))
"""
import ctypes
import ctypes.util
import select
import socket
import struct
import sys
import time
import wave

from . import hcitap

RATE, FRAME, TOC = 16000, 320, 0xB8  # 16 kHz, 20 ms a frame
MAX_FRAMES = 3000                    # a minute; a longer press goes on in the next Press
GAP = .5                             # s without a frame that end a press whose end marker never came
_lib = None


def parse(v):
    """A voice value -> (seq, buttons, level, Opus packet); an end marker -> (None, buttons, 0, b""); anything else
    (touch, buttons, a value cut short as hidraw has it) -> None."""
    if len(v) == 2 and v[0] == 0x10:
        return None, v[1], 0, b""
    if len(v) > 7 and v[0] == 0x10 and v[7] == TOC and 0 < v[6] <= len(v) - 7:
        return v[4] | v[5] << 8, v[1], v[2] | v[3] << 8, bytes(v[7:7 + v[6]])
    return None


class Press:
    """One press's frames, by seq. held: the frames sent with Siri held; level: the highest level the remote reported;
    ended: its end marker came (else it was cut: by the next press, GAP s of silence, MAX_FRAMES or the source's end);
    last: when its last frame came."""

    def __init__(self):
        self.seqs, self.pkts, self.held, self.level, self.last, self.ended, self.done = [], [], 0, 0, 0.0, False, False

    def add(self, t, v):
        """-> True when v (a value that came at time t) was this press's: a frame kept, or its end marker. False: not
        voice, or this press is over; a frame refused at a new start (seq back down) is the next press's first."""
        p = None if self.done else parse(v)
        if p is None:
            return False
        seq, buttons, level, pkt = p
        if seq is None:                    # an end marker; before any frame it is the last press's
            self.done = self.ended = bool(self.seqs)
            return self.done
        if self.seqs and (seq <= self.seqs[-1] or seq - self.seqs[0] >= MAX_FRAMES):
            self.done = True               # (one L2CAP channel never reorders: a lower seq is a new press)
            return False
        self.seqs.append(seq)
        self.pkts.append(pkt)
        self.held += bool(buttons & 0x10)
        self.level, self.last = max(self.level, level), t
        return True

    def packets(self):
        """The Opus packets from the first frame kept to the last, in seq order; None for each one lost."""
        out = [None] * (self.seqs[-1] - self.seqs[0] + 1) if self.seqs else []
        for s, p in zip(self.seqs, self.pkts):
            out[s - self.seqs[0]] = p
        return out


def presses(values, gap=GAP):
    """(time, value) pairs, value None for a tick -> each Press as it ends: at its end marker, when the next one starts,
    after `gap` s without a frame (its end marker lost, or the link), or when the values end."""
    p = Press()
    for t, v in values:
        if p.seqs and t - p.last > gap:
            yield p
            p = Press()
        if v is None:
            continue
        took = p.add(t, v)
        if p.done:
            yield p
            p = Press()
            if not took:
                p.add(t, v)
    if p.seqs:
        yield p


def btsnoop(path):
    """A btmon -w capture (btsnoop, datalink 2001: HCI monitor records) -> (Unix time, monitor packet) per record."""
    with open(path, "rb") as f:
        h = f.read(16)
        if h[:8] != b"btsnoop\0" or h[12:16] != struct.pack(">I", 2001):
            raise ValueError("%s: not a btmon -w capture (btsnoop, datalink 2001)" % path)
        while True:
            r = f.read(24)
            if len(r) < 24:
                return
            _, n, flags, _, ts = struct.unpack(">IIIIq", r)
            d = f.read(n)                  # flags: controller index << 16 | monitor opcode; ts: us since year 0
            yield (ts - 0x00DCDDB30F2F8000) / 1e6, struct.pack("<HHH", flags & 0xFFFF, flags >> 16, len(d)) + d


class _Capture(hcitap.Tap):
    """The tap over a capture: a link it never saw connect (up before btmon started) is the remote's, encrypted, from
    its first voice frame. Offline only: the live tap never trusts a link it can't tie to the remote's address."""

    def _ours(self, key, start):
        if key not in self.link and start is not None and hcitap.voice_start(start):
            self.link[key] = True
            self.enc.add(key)
        return hcitap.Tap._ours(self, key, start)


def replay(path, remote=None, info=None):
    """A btmon -w capture -> (Unix time, value) for the remote's voice in it. remote: its address, for a link that
    connected during the capture; one up before it began is trusted from its first voice frame (_Capture)."""
    tap = _Capture(remote, info=info)
    for t, p in btsnoop(path):
        v = tap.packet(p)
        if v is not None:
            yield t, v


def client(path, idle=.25):
    """From a tap serving on `path` (hcitap.serve; Linux): (time.monotonic() when it came, value), and (time, None)
    when `idle` s pass without one. Ends when the tap closes the connection."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as s:
        try:
            s.connect(path)
        except OSError as e:
            raise OSError(e.errno, "%s: %s (no tap serving there?)" % (path, e.strerror))
        while True:
            if not select.select([s], [], [], idle)[0]:
                yield time.monotonic(), None
                continue
            m = s.recv(256)
            if not m:
                return
            if len(m) in (8 + 2, 8 + 101):
                yield struct.unpack_from("<Q", m)[0] / 1e9, m[8:]


def libopus():
    """libopus with the prototypes used here, loaded on first use; RuntimeError, naming the package, without it."""
    global _lib
    if _lib is None:
        name = "libopus.so.0" if sys.platform.startswith("linux") else ctypes.util.find_library("opus") or "libopus"
        vp, i, P = ctypes.c_void_p, ctypes.c_int, ctypes.POINTER
        try:
            lib = ctypes.CDLL(name)
            for fn, res, args in (("opus_decoder_create", vp, [ctypes.c_int32, i, P(i)]),
                                  ("opus_decode", i, [vp, ctypes.c_char_p, ctypes.c_int32, P(ctypes.c_int16), i, i]),
                                  ("opus_decoder_destroy", None, [vp])):
                f = getattr(lib, fn)
                f.restype, f.argtypes = res, args
        except (OSError, AttributeError) as e:
            raise RuntimeError("decoding the voice needs libopus: sudo apt install libopus0 (Debian, Ubuntu), "
                               "sudo dnf install opus (Fedora), brew install opus (macOS)") from e
        _lib = lib
    return _lib


def decode(packets):
    """Opus packets (None: lost) -> 16 kHz mono 16-bit PCM, 320 samples each. A lost packet, or one libopus refuses,
    becomes libopus's concealment (PLC), so the timing stays whole."""
    lib, err = libopus(), ctypes.c_int()
    dec = lib.opus_decoder_create(RATE, 1, ctypes.byref(err))
    if err.value or not dec:
        raise RuntimeError("libopus can't make a decoder (%d)" % err.value)
    buf, out = (ctypes.c_int16 * FRAME)(), bytearray()
    try:
        for p in packets:
            n = lib.opus_decode(dec, p, len(p), buf, FRAME, 0) if p else -1
            if n < 0:
                n = lib.opus_decode(dec, None, 0, buf, FRAME, 0)
            out += ctypes.string_at(buf, 2 * n) if n > 0 else bytes(2 * FRAME)
    finally:
        lib.opus_decoder_destroy(dec)
    return bytes(out)


def write_wav(f, pcm):
    """16 kHz mono 16-bit PCM -> a WAV file; f: a path, or a binary file object."""
    with wave.open(f, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
