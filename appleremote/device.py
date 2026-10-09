"""Where reports come from: the remote's hidraw node (Linux), or a recorded capture (anywhere).

Both sources yield (time, report) pairs: report is the raw bytes, None after a quiet `idle` interval (a tick for
timers such as gesture coasting), or b"" when the link drops (the remote sleeps after a few idle minutes and
disconnects; any button wakes it).
"""
import errno
import fcntl
import glob
import json
import os
import select
import sys
import time

REMOTE = "0005:0000004C:0000026D"  # HID_ID: Bluetooth, Apple, gen-1 Siri Remote (tested: A1962)
UNLOCK = b"\xf0\xaf"               # feature report 0xF0 <- 0xAF: starts the touch, button and audio reports


def hidiocsfeature(size):
    # _IOWR('H', 0x06, size). ponytail: asm-generic ioctl layout (x86, ARM, RISC-V); powerpc, mips, sparc differ
    return 3 << 30 | size << 16 | ord("H") << 8 | 0x06


def find():
    """[(path, hid_id, name)] for every Apple Bluetooth HID node. A remote names itself after its serial number."""
    found = []
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(node + "/device/uevent") as f:
                info = dict(line.split("=", 1) for line in f.read().splitlines() if "=" in line)
        except OSError:
            continue
        if info.get("HID_ID", "").startswith("0005:0000004C:"):
            found.append(("/dev/" + os.path.basename(node), info["HID_ID"], info.get("HID_NAME", "")))
    return found


def reports(path=None, idle=0.01, info=None):
    """Live reports from `path`, or from the first gen-1 Siri Remote found. Unlocks it on every (re)connect."""
    say, last = info or (lambda s: print(s, file=sys.stderr, flush=True)), [None]

    def note(msg):  # say each state once, not on every retry
        if msg != last[0]:
            last[0] = msg
            say(msg)

    while True:
        dev = path or next((p for p, hid, _ in find() if hid == REMOTE), None)
        if not dev or not os.path.exists(dev):
            note("waiting for the remote: pair it, or press a button to wake it")
            time.sleep(1)
            yield time.monotonic(), None
            continue
        try:
            fd = os.open(dev, os.O_RDWR)
        except OSError as e:
            hint = " (install udev/71-appleremote.rules, or run as root)" if e.errno == errno.EACCES else ""
            note("cannot open %s: %s%s" % (dev, e.strerror, hint))
            time.sleep(2)
            yield time.monotonic(), None
            continue
        try:
            note("connected: " + dev)
            try:
                fcntl.ioctl(fd, hidiocsfeature(len(UNLOCK)), UNLOCK)
            except OSError as e:
                note("unlock failed: %s" % e.strerror)
            while True:
                if not select.select([fd], [], [], idle)[0]:
                    yield time.monotonic(), None
                    continue
                r = os.read(fd, 1024)
                if not r:
                    raise OSError(errno.EIO, "end of file")
                yield time.monotonic(), r
        except OSError as e:
            note("disconnected (%s): press a button to wake the remote" % e.strerror)
            yield time.monotonic(), b""
            time.sleep(1)
        finally:
            os.close(fd)


def replay(path, realtime=False, idle=0.01):
    """Reports from a capture: JSON lines with "kind": "report", "t" and "hex", as `monitor --raw` writes them.
    Gives the idle ticks a live remote would, so gestures coast as they did; realtime=True also keeps the pace."""
    now = None
    with open(path) as f:
        for line in f:
            e = json.loads(line)
            if e.get("kind") != "report":
                continue
            t = e["t"]
            if now is not None:
                end = min(t, now + 1)  # ponytail: silences are cut to 1 s; coasting is over by then
                while now + idle < end:
                    now += idle
                    if realtime:
                        time.sleep(idle)
                    yield now, None
                if realtime and end == t:
                    time.sleep(max(0.0, t - now))
            now = t
            yield now, bytes.fromhex(e["hex"])
    for _ in range(int(1 / idle)):
        now = (now or 0) + idle
        if realtime:
            time.sleep(idle)
        yield now, None
