"""python3 -m appleremote {list,monitor,gestures,keys,lab}: each command has --help; README.md walks through them."""
import argparse
import json
import os
import sys

from . import device
from .decode import Decoder, Touch
from .gestures import Gestures


def as_dict(ev):
    """An event namedtuple -> a JSON-ready dict with its kind; bytes become hex."""
    d = {"kind": type(ev).__name__.lower()}
    d.update((k, v.hex() if isinstance(v, bytes) else v) for k, v in ev._asdict().items())
    return d


def pump(source, out, gestures=False, raw=False):
    """(time, report) pairs -> decoder (-> gesture engine) -> out(event dict), in order."""
    now = 0.0
    dec = Decoder()
    eng = Gestures(lambda kind, **f: out(dict(kind="gesture", type=kind, **f)), clock=lambda: now) if gestures else None
    for now, r in source:
        if raw and r:
            out({"kind": "report", "t": round(now, 3), "hex": r.hex()})
        for ev in dec(r):
            out(as_dict(ev))
            if eng and type(ev) is Touch:
                eng.frame(ev.t, ev.x, ev.y, ev.down)
        if eng:
            eng.tick()


def printer(kinds):
    def out(ev):
        if ev["kind"] in kinds:
            print(json.dumps(ev), flush=True)
    return out


def run_keys(args, source):
    from . import keys
    try:
        overrides = None
        if args.map:
            with open(args.map) as f:
                overrides = json.load(f)
        keymap = keys.keymap(overrides)
    except (OSError, ValueError) as e:
        sys.exit("keys: --map: %s" % e)
    try:
        kb = keys.Keyboard(keymap)
    except OSError as e:
        sys.exit("keys: cannot create a virtual keyboard on /dev/uinput: %s (see udev/71-appleremote.rules)" % e.strerror)
    print("virtual keyboard ready", file=sys.stderr, flush=True)
    try:
        pump(source(), kb, gestures=True)
    finally:
        os.close(kb.fd)


def run_lab(args, source):
    import queue
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lab.html"), "rb") as f:
        page = f.read()
    clients, lock, status = [], threading.Lock(), [{"kind": "info", "text": "starting"}]

    def out(ev):
        if ev["kind"] == "info":
            status[0] = ev
        with lock:
            for q in clients:
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    pass  # ponytail: a viewer that can't keep up misses events

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.headers.get("Host", "").rsplit(":", 1)[0] not in ("127.0.0.1", "localhost"):
                return self.send_error(403)  # DNS-rebinding guard: only pages served from this machine
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif self.path == "/events":
                q = queue.Queue(1000)
                q.put(status[0])
                with lock:
                    clients.append(q)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    while True:
                        try:
                            self.wfile.write(b"data: " + json.dumps(q.get(timeout=15)).encode() + b"\n\n")
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except OSError:
                    pass
                finally:
                    with lock:
                        clients.remove(q)
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)

    def run():
        info = lambda text: out({"kind": "info", "text": text})
        while True:  # live: runs forever; a replay starts over when it ends
            if args.replay:
                info("replaying " + os.path.basename(args.replay))
            pump(source(info), out, gestures=True, raw=True)

    threading.Thread(target=run, daemon=True).start()
    print("lab: http://127.0.0.1:%d (Ctrl-C to stop)" % args.port, file=sys.stderr, flush=True)
    server.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m appleremote",
                                 description="Use a gen-1 Apple Siri Remote (A1513/A1962) on Linux, over Bluetooth LE.")
    sub = ap.add_subparsers(dest="cmd", metavar="command")
    sub.required = True
    sub.add_parser("list", help="list paired Apple Bluetooth HID devices", description="List paired Apple Bluetooth "
                   "HID devices: hidraw node, HID id, name (a remote's name is its serial number).")
    for name, text in (("monitor", "print decoded events (touch, buttons, voicechunk) as JSON lines"),
                       ("gestures", "print tvOS-style gestures (move, tilt, touch) and buttons as JSON lines"),
                       ("keys", "type keys from the remote through a virtual keyboard (uinput)"),
                       ("lab", "serve a live web lab on 127.0.0.1: touch plot, buttons, bytes, feel test")):
        p = sub.add_parser(name, help=text, description=text[0].upper() + text[1:] + ".")
        p.add_argument("--dev", metavar="PATH", help="hidraw node to read (default: find the gen-1 Siri Remote, "
                       "again on every reconnect, as its node number can change)")
        p.add_argument("--replay", metavar="FILE", help="play a capture (monitor --raw output) instead of a remote")
        if name == "monitor":
            p.add_argument("--raw", action="store_true",
                           help="also print every report as hex with its time; this output can be replayed")
        if name == "keys":
            p.add_argument("--map", metavar="FILE", help='JSON overrides of the key map, e.g. {"click": "KEY_SPACE"}')
        if name == "lab":
            p.add_argument("--port", type=int, default=8791, help="port on 127.0.0.1 (default 8791)")
    args = ap.parse_args(argv)

    def source(info=None):
        if args.replay:
            return device.replay(args.replay, realtime=args.cmd in ("keys", "lab"))
        return device.reports(args.dev, info=info)

    try:
        if args.cmd == "list":
            found = device.find()
            for path, hid, name in found:
                print("%s  %s  %r  %s" % (path, hid, name, "gen-1 Siri Remote" if hid == device.REMOTE
                                          else "another Apple device (untested)"))
            if not found:
                print("no paired Apple Bluetooth HID device found: see README.md, Quick start", file=sys.stderr)
            return 0 if found else 1
        if not args.replay and not sys.platform.startswith("linux"):
            ap.exit(2, "live mode needs Linux (hidraw); elsewhere try --replay tests/sample.jsonl\n")
        if args.cmd == "monitor":
            pump(source(), printer({"report", "touch", "buttons", "voicechunk"}), raw=args.raw)
        elif args.cmd == "gestures":
            pump(source(), printer({"gesture", "buttons"}), gestures=True)
        elif args.cmd == "keys":
            run_keys(args, source)
        else:
            run_lab(args, source)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:  # e.g. piped into head: leave quietly
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
