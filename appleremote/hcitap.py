"""The Siri Remote's whole voice frames, from the kernel's HCI monitor (Linux; opening it needs CAP_NET_RAW).

The remote sends each 20 ms Opus frame whole, as a 101-byte notification on ATT handle 0x0023, but bluetoothd reads
every ATT PDU into a 23-byte buffer, so hidraw gets only the first 20 bytes (docs/PROTOCOL.md, Voice). The HCI monitor
channel, which btmon reads, has every frame whole. It also has everything else the machine's Bluetooth carries:
keyboard input, pairing keys, every other device's traffic. So this file, the only code here that ever holds the
capability, keeps the remote's voice and nothing else:

- It opens the monitor, then gives up every capability (and root, under sudo) before it reads a packet.
- It reassembles ACL data only from the remote's own link, once that link is encrypted: the link whose LE connect named
  the remote's address or, for one up before the tap started, the one the kernel's connection list names. A frame's
  layout is public and an address can be borrowed, but the encryption takes the remote's bond.
- Another link's fragments are dropped as they are parsed, before anything of them is buffered. So is anything of the
  remote's that can't become a notification on 0x0023 holding a voice frame (101 bytes: 0x10, the Opus TOC 0xB8 at [7])
  or an end marker (2 bytes: 0x10). It logs the remote's connects and dropped voice streams, never an address or a
  payload.

It imports only the standard library, so it also runs alone, as systemd/appleremote-voice.service runs a root-owned
copy of it:
    python3 -I -S hcitap.py --serve /run/appleremote/voice.sock [--remote AA:BB:CC:DD:EE:FF]
python3 -m appleremote voice runs it in-process (README, Voice); appleremote/voice.py decodes what it keeps.
"""
import argparse
import errno
import glob
import os
import select
import signal
import socket
import stat
import struct
import sys
import time

HID_ID = "0005:0000004C:0000026D"  # the gen-1 Siri Remote's hidraw node, as device.REMOTE (this file imports nothing)
ATT_NOTIFY = b"\x1b\x23\x00"       # ATT Handle Value Notification on handle 0x0023: HID report 0xFC
SIZES = (5, 104)                   # L2CAP payloads kept: ATT_NOTIFY + a 2-byte end marker or a 101-byte voice frame
CLIENTS = 4                        # serve(): the most readers at once
# _IOR('H', 210 | 212, int). ponytail: asm-generic ioctl layout (x86, ARM, RISC-V); powerpc, mips, sparc differ
HCIGETDEVLIST, HCIGETCONNLIST = 0x800448D2, 0x800448D4


def _say(msg):
    print(msg, file=sys.stderr, flush=True)


def _addr(s):
    """AA:BB:CC:DD:EE:FF -> its 6 bytes as HCI carries them (little-endian). ValueError if it isn't an address."""
    try:
        b = bytes.fromhex(s.replace(":", ""))[::-1]
    except (AttributeError, ValueError):
        b = b""
    if len(b) != 6:
        raise ValueError("not a Bluetooth address: %r (expected AA:BB:CC:DD:EE:FF)" % (s,))
    return b


def voice_start(f):
    """Is f (an ACL start fragment's L2CAP bytes) a voice frame's first fragment: L2CAP length 104 on CID 4,
    ATT_NOTIFY, then a value with 0x10 at [0] and the Opus TOC 0xB8 at [7]?"""
    return len(f) >= 15 and f[:4] == b"\x68\x00\x04\x00" and f[4:7] == ATT_NOTIFY and f[7] == 0x10 and f[14] == 0xB8


class Tap:
    """HCI monitor packets in, the remote's voice values out (packet()). No I/O: tests and captures use it anywhere."""

    def __init__(self, remote, links=(), info=None):
        """remote: its address (None: nobody's). links: the LE links up before the monitor opened, as links() reads
        them: (controller index, handle, peer address as HCI carries it, encrypted). info: gets each log line."""
        self.say = info or _say
        self.remote = _addr(remote) if remote else None
        self.part, self.link, self.enc = {}, {}, set()  # (index, handle) -> the L2CAP frame so far; -> is it the
        for idx, h, addr, enc in links:                  # remote's link; the encrypted links
            self.link[idx, h] = addr == self.remote
            if enc:
                self.enc.add((idx, h))
            if addr == self.remote:
                self.say("the remote is connected (hci%d handle %d)%s" % (idx, h, "" if enc else ", not encrypted yet"))

    def packet(self, p):
        """One monitor packet (le16 opcode, controller index, length; the payload) -> a value to keep, or None."""
        if len(p) < 6:
            return None
        op, idx, n = struct.unpack_from("<HHH", p)
        d = p[6:6 + n]
        if op == 5:                                       # HCI_MON_ACL_RX_PKT
            return self._acl(idx, d)
        if op == 3 and len(d) >= 2:                       # HCI_MON_EVENT_PKT: code, length, parameters
            self._event(idx, d[0], d[2:2 + d[1]])
        return None

    def _event(self, idx, code, e):
        if code in (0x08, 0x59) and len(e) >= 4:         # Encryption Change v1, v2: status, handle, enabled
            key = (idx, e[1] | (e[2] & 15) << 8)
            (self.enc.add if e[0] == 0 and e[3] else self.enc.discard)(key)
            return
        if code == 0x3E and len(e) >= 12 and e[0] in (0x01, 0x0A, 0x29) and e[1] == 0:
            # LE Connection Complete, LE Enhanced Connection Complete v1 and v2 (the same prefix), success: subevent,
            # status, handle, role, peer address type, peer address
            key, remote = (idx, e[2] | (e[3] & 15) << 8), e[6:12] == self.remote
        elif code == 0x03 and len(e) >= 3 and e[0] == 0:  # BR/EDR Connection Complete: never the remote (LE only)
            key, remote = (idx, e[1] | (e[2] & 15) << 8), False
        elif code == 0x05 and len(e) >= 3 and e[0] == 0:  # Disconnection Complete
            key, remote = (idx, e[1] | (e[2] & 15) << 8), None
        else:
            return
        self.part.pop(key, None)
        self.enc.discard(key)
        if self.link.pop(key, None):
            self.say("the remote disconnected (hci%d handle %d)" % key)
        if remote is not None:
            self.link[key] = remote
        if remote:
            self.say("the remote connected (hci%d handle %d)" % key)

    def _acl(self, idx, d):
        if len(d) < 4:
            return None
        h, n = struct.unpack_from("<HH", d)
        key, frag, start = (idx, h & 0xFFF), d[4:4 + n], h >> 12 & 3 != 1
        if not self._ours(key, frag if start else None):  # another link's: nothing of it is kept, not a fragment
            return None
        buf = self.part.pop(key, None)
        if start:                                         # PB 00, 10 or 11 start a frame, as in l2cap_recv_acldata()
            buf = frag
        elif buf is None:                                 # the rest of something already dropped (or never seen)
            return None
        else:
            buf += frag
        if len(buf) >= 4:
            n, cid = struct.unpack_from("<HH", buf)
            if cid != 4 or n not in SIZES or len(buf) > 4 + n or not ATT_NOTIFY.startswith(buf[4:7]):
                return None                               # not the remote's voice: dropped, and so is its rest
            if len(buf) == 4 + n:
                v = buf[7:]
                return v if v[0] == 0x10 and (len(v) == 2 or v[7] == 0xB8) else None
        self.part[key] = buf
        return None

    def _ours(self, key, start):
        """Is key the remote's encrypted link? For another link's start fragment that begins a voice stream (seq 0,
        so once a press), say why it is dropped: a wrong --remote, or a lost event."""
        if self.link.get(key) and key in self.enc:
            return True
        if start is not None and voice_start(start) and start[11:13] == b"\0\0":
            it = self.link.get(key)
            self.say("a voice stream on hci%d handle %d dropped: %s" % (key + (
                "a link the tap never saw connect" if it is None else
                "not the remote's link (--remote)" if it is False else "the link isn't encrypted",)))
        return False


def address():
    """The connected remote's address, from its hidraw node as device.find() finds it: BlueZ's hog names the peer in
    HID_UNIQ (profiles/input/hog-lib.c). None while no gen-1 Siri Remote is connected."""
    for uevent in sorted(glob.glob("/sys/class/hidraw/hidraw*/device/uevent")):
        try:
            with open(uevent) as f:
                info = dict(line.split("=", 1) for line in f.read().splitlines() if "=" in line)
        except OSError:
            continue
        if info.get("HID_ID") == HID_ID and info.get("HID_UNIQ"):
            return info["HID_UNIQ"]
    return None


def monitor():
    """A socket on the HCI monitor channel (every controller). Python's bind() can't name an HCI channel (it leaves
    0, raw), so struct sockaddr_hci {family, dev, channel} goes through ctypes. Needs CAP_NET_RAW, else EPERM."""
    import ctypes
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    addr = struct.pack("=HHH", socket.AF_BLUETOOTH, 0xFFFF, 2)  # HCI_DEV_NONE, HCI_CHANNEL_MONITOR
    if ctypes.CDLL(None, use_errno=True).bind(s.fileno(), addr, len(addr)):
        err = ctypes.get_errno()
        s.close()
        raise OSError(err, os.strerror(err))
    return s


def links():
    """The LE links up now, from the kernel's list (HCIGETDEVLIST, then HCIGETCONNLIST per controller, on an unbound
    HCI socket: no capability, as hcitool con reads it): (index, handle, peer address, encrypted), for Tap(links=).
    ponytail: 64 links per controller at most; past that one counts as unknown, and its voice is dropped."""
    import fcntl
    out = []
    with socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI) as s:
        dl = bytearray(struct.pack("=H", 16) + bytes(2 + 16 * 8))  # dev_num, then 16 {u16 dev_id; u32 opt} from 4
        fcntl.ioctl(s, HCIGETDEVLIST, dl)
        for i in range(struct.unpack_from("=H", dl)[0]):
            dev = struct.unpack_from("=H", dl, 4 + 8 * i)[0]
            cl = bytearray(struct.pack("=HH", dev, 64) + bytes(64 * 16))  # dev_id, conn_num, 64 hci_conn_info
            try:
                fcntl.ioctl(s, HCIGETCONNLIST, cl)
            except OSError:                               # gone since the list was read
                continue
            for j in range(struct.unpack_from("=H", cl, 2)[0]):
                h, addr, kind, _, state, mode = struct.unpack_from("=H6sBBHI", cl, 4 + 16 * j)
                if kind == 0x80 and state == 1:           # LE_LINK, BT_CONNECTED
                    out.append((dev, h, addr, bool(mode & 4)))  # HCI_LM_ENCRYPT
    return out


def unprivileged():
    """Give up every capability, and under sudo root too (back to the user who ran it), then turn dumpable off so no
    process of that user can trace this one and take its socket. A bound monitor socket keeps working: the kernel
    checks CAP_NET_RAW only at bind. Capabilities are per thread: call it before starting any."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if os.getuid() == 0 and os.environ.get("SUDO_UID"):
        gid = int(os.environ["SUDO_GID"])
        os.initgroups(os.environ["SUDO_USER"], gid)
        os.setgid(gid)
        os.setuid(int(os.environ["SUDO_UID"]))           # (root to a user: every capability goes too)
    head = ctypes.create_string_buffer(struct.pack("=Ii", 0x20080522, 0))  # _LINUX_CAPABILITY_VERSION_3, this thread
    if libc.capset(head, bytes(24)) or libc.prctl(4, 0, 0, 0, 0):         # every set empty; PR_SET_DUMPABLE 0
        err = ctypes.get_errno()
        raise OSError(err, "giving up privileges: " + os.strerror(err))


def start(remote=None, info=None):
    """Open the HCI monitor for the remote's voice -> (monitor socket, Tap). Needs CAP_NET_RAW, which it gives up at
    once (unprivileged()). remote: its address; None: the connected gen-1 Siri Remote's, waiting until one is."""
    say, told = info or _say, False
    while remote is None:
        remote = address()
        if remote is None:
            if not told:
                say("waiting for the remote: press a button on it, or give its address (--remote)")
                told = True
            time.sleep(1)
    _addr(remote)                                         # (a ValueError before anything opens)
    try:
        mon = monitor()
    except OSError as e:
        raise OSError(e.errno, "opening the HCI monitor: %s%s" % (e.strerror, " (it needs CAP_NET_RAW: run it with "
                      "sudo, or see systemd/appleremote-voice.service)" if e.errno == errno.EPERM else ""))
    try:
        unprivileged()
        return mon, Tap(remote, links(), say)            # (after the bind: no link falls between the two)
    except BaseException:
        mon.close()
        raise


def frames(remote=None, idle=.25, info=None):
    """Live, from start(remote): (time.monotonic(), value) for each of the remote's voice values, and (time, None)
    when `idle` s pass without one."""
    mon, tap = start(remote, info)
    tap.say("listening: hold Siri on the remote and speak")
    with mon:
        last = time.monotonic()
        while True:
            v = None
            if select.select([mon], [], [], idle)[0]:
                p = mon.recv(4096)
                if not p:
                    raise OSError(errno.EPIPE, "the HCI monitor socket closed")
                v = tap.packet(p)
            now = time.monotonic()
            if v is not None or now - last >= idle:
                last = now
                yield now, v


def serve(mon, tap, path, uid):
    """Until `mon` closes: send each value `tap` keeps to every client of the unix socket `path`. Clients are processes
    of `uid` (SO_PEERCRED), CLIENTS at most, and never send. Each message (SOCK_SEQPACKET) is the CLOCK_MONOTONIC ns
    when the value came (8 bytes, little-endian), then the value: a 101-byte voice frame or a 2-byte end marker. A
    client that can't take a message at once is dropped; with no client, values are dropped."""
    d = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(d):
        os.makedirs(d, 0o700)
    try:
        if stat.S_ISSOCK(os.lstat(path).st_mode):
            os.unlink(path)                               # a killed run's; anything else there stays, and bind fails
    except FileNotFoundError:
        pass
    lst, clients, mask = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET), [], os.umask(0o177)
    try:
        lst.bind(path)                                    # 0600 from the start (the umask)
    except OSError:
        lst.close()
        raise
    finally:
        os.umask(mask)
    try:
        lst.listen(CLIENTS)
        while True:
            ready = select.select([lst, mon] + clients, [], [])[0]
            if lst in ready:                              # before the packet below, which a client that's in gets
                c = lst.accept()[0]
                pid, peer, _ = struct.unpack("iII", c.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if peer != uid or len(clients) >= CLIENTS:
                    tap.say("client pid %d refused: %s" % (pid, "uid %d" % peer if peer != uid else "%d already" %
                                                           CLIENTS))
                    c.close()
                else:
                    clients.append(c)
                    tap.say("client pid %d connected (%d of %d)" % (pid, len(clients), CLIENTS))
            for c in [c for c in clients if c in ready]:  # clients never send: this is their end
                clients.remove(c)
                c.close()
            if mon in ready:
                p = mon.recv(4096)
                if not p:
                    return
                v = tap.packet(p)
                if v is not None and clients:
                    m = struct.pack("<Q", time.monotonic_ns()) + v
                    for c in clients[:]:
                        try:
                            c.send(m, socket.MSG_DONTWAIT)
                        except OSError as e:              # its queue is full (it fell behind), or it is gone
                            tap.say("client dropped: %s" % e)
                            clients.remove(c)
                            c.close()
    finally:
        for c in clients + [lst]:
            c.close()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def serve_forever(remote, path, info=None):
    """start(remote), then serve() the remote's voice on `path` until SIGTERM, or an OSError."""
    mon, tap = start(remote, info)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # (so serve() removes its socket)
    with mon:
        tap.say("serving the remote's voice on %s" % path)
        serve(mon, tap, path, os.getuid())
    raise OSError(errno.EPIPE, "the HCI monitor socket closed")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hcitap.py", description="Serve the gen-1 Siri Remote's voice frames from the "
                                 "HCI monitor on a unix socket (Linux; needs CAP_NET_RAW, which it gives up once the "
                                 "monitor is open). python3 -m appleremote voice --help says more.")
    ap.add_argument("--remote", metavar="ADDR", help="the remote's Bluetooth address (default: the connected gen-1 "
                    "Siri Remote's, from its hidraw node)")
    ap.add_argument("--serve", metavar="PATH", required=True, help="the unix socket to serve on")
    a = ap.parse_args(argv)
    try:
        serve_forever(a.remote, a.serve)
    except (OSError, ValueError) as e:
        return "hcitap: %s" % e


if __name__ == "__main__":
    sys.exit(main())
