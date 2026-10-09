# open-appleremote

**Use the first-generation Apple Siri Remote on Linux**: the touch remote of the 2015 and 2017 Apple TVs (A1513,
A1962), not the older infrared Apple Remote. Touch surface, buttons and tvOS-style gestures are decoded in plain
Python 3 with no dependencies; an optional virtual keyboard makes an old remote work in Kodi, a browser, an emulator
or any other app.

Paired with Linux, the remote does nothing: BlueZ exposes it only as a raw `/dev/hidrawN` node with a vendor-defined
report map, and it stays silent until it is unlocked. This package unlocks it, decodes its reports, and turns them
into events, gestures or key presses. [docs/PROTOCOL.md](docs/PROTOCOL.md) documents everything it sends.

Status: 0.1.0, an early release. Decoding, gestures and the lab are verified against real captures from an A1962 on
Linux; the live device loop, the uinput keyboard and the udev rule have not been re-run on hardware since they were
extracted. Reports are welcome (see [CHANGELOG.md](CHANGELOG.md)).

## Quick start

You need Linux with BlueZ (its `hog` plugin is on by default) and Python 3.8 or newer. There is nothing to install:
clone the repository and run the commands below from its top directory.

**1. Pair the remote** (once). Hold **Menu + Volume Up** for about 5 seconds next to the computer, then pair it in
`bluetoothctl`. After `scan on`, the remote shows up named after its serial number; use its address:

```text
$ bluetoothctl
[bluetooth]# agent NoInputNoOutput
[bluetooth]# default-agent
[bluetooth]# scan on
[bluetooth]# pair AA:BB:CC:DD:EE:FF
[bluetooth]# trust AA:BB:CC:DD:EE:FF
[bluetooth]# connect AA:BB:CC:DD:EE:FF
[bluetooth]# scan off
```

**2. Let your user read it** (once), instead of running everything as root:

```sh
sudo cp udev/71-appleremote.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

Then press a button on the remote. The rules file explains the options for `/dev/uinput` and for headless machines.

**3. Watch it:**

```sh
python3 -m appleremote list        # finds the remote's hidraw node
python3 -m appleremote monitor     # decoded events, one JSON object per line
python3 -m appleremote gestures    # tvOS-style moves, tilts and buttons
python3 -m appleremote lab         # live web lab: open http://127.0.0.1:8791
```

The lab listens on 127.0.0.1 only. On a headless machine, forward it with `ssh -L 8791:127.0.0.1:8791 <host>` and
open the same address on your computer.

**4. Use it as a keyboard:**

```sh
python3 -m appleremote keys
```

This needs write access to `/dev/uinput` (root, or the optional line in the udev rules). The default key map:

| On the remote | Key |
|---|---|
| slide or flick up, down, left, right | arrow keys |
| click (press the touch surface) | Enter |
| Menu | Esc (back) |
| TV / Home | Home page (`KEY_HOMEPAGE`) |
| Play/Pause | Play/Pause |
| Volume + / − | Volume up / down |
| Siri | nothing |

Change it with a JSON file, `python3 -m appleremote keys --map keys.json`. Values are key names (`KEY_A` to `KEY_Z`,
digits, `KEY_F1` to `KEY_F12`, arrows, media keys: see `NAMES` in `appleremote/keys.py`), numbers from
`linux/input-event-codes.h`, or `null` to turn an input off:

```json
{"click": "KEY_SPACE", "siri": "KEY_C", "tv": null}
```

To start it with your session, see [systemd/appleremote-keys.service](systemd/appleremote-keys.service).

**No remote at hand?** `monitor`, `gestures`, `keys` and `lab` take `--replay FILE` to play a capture instead; all
but `keys` work on any OS. Try the lab with a real capture from the test data:

```sh
python3 -m appleremote lab --replay tests/sample.jsonl
```

Record your own with `python3 -m appleremote monitor --raw > capture.jsonl`: `--raw` also logs each report with its
time, which is what a replay plays.

## Events

`monitor` and `gestures` print one JSON object per line, ready for `jq` or any language:

```json
{"kind": "touch", "t": 0.0, "x": -849, "y": -828, "down": true, "fingers": [[-849, -828, 3]], "raw": "fc010032b8ff01af4ccc3e210a23"}
{"kind": "buttons", "mask": 128, "pressed": ["click"], "released": [], "raw": "fc0080"}
{"kind": "gesture", "type": "move", "dir": "right", "src": "flick"}
{"kind": "gesture", "type": "tilt", "x": -0.083, "y": 0.134}
```

- `touch`: `t` is seconds on the remote's own clock, `x` and `y` the first finger (+X right, +Y up, about 1650
  units across), `fingers` every finger as `[x, y, state]`.
- `buttons`: `mask` is the buttons held; names are `tv`, `volup`, `voldown`, `playpause`, `siri`, `menu`, `click`.
- `voicechunk`: an audio report while Siri is held (research: it can't be decoded through hidraw).
- `gesture`: `touch` (down or up), `move` (a focus step: `src` is `slide` or `flick`), `tilt` (a resting finger
  leaning the focused tile, -1 to 1).

From Python:

```python
from appleremote.decode import Decoder, Touch
from appleremote.device import reports
from appleremote.gestures import Gestures

decode = Decoder()
engine = Gestures(lambda kind, **f: print(kind, f))
for t, report in reports():          # survives sleep and reconnects
    for ev in decode(report):        # Touch, Buttons or VoiceChunk
        if isinstance(ev, Touch):
            engine.frame(ev.t, ev.x, ev.y, ev.down)
        else:
            print(ev)
    engine.tick()                    # lets flicks coast
```

The gesture engine's feel (step size, pacing, flick speed, coasting) is set by the constants at the top of
`appleremote/gestures.py`.

## Ideas for an old remote

- A couch remote for Kodi, Jellyfin, mpv, VLC, a browser in TV mode, or a game launcher.
- A presentation clicker: swipe for the next slide, click to start a video.
- A home-automation remote: pipe `gestures` into a script that calls your lights, scenes or music.
- A small touchpad for a kiosk or a Raspberry Pi media box.
- An instrument: the touch surface streams x/y at 100 Hz, enough for a theremin or a filter sweep.
- Two-finger gestures: the decoder reports both fingers; nothing uses them yet.

## Supported and tested

- **Tested**: Siri Remote gen 1, model A1962, on BlueZ 5.72, Linux 6.17 (arm64, MediaTek MT7925 Bluetooth). The
  live code paths of this extraction get one more run there before 0.1.0 ships (see the changelog).
- **Should work, untested**: the A1513 (the original gen-1 remote, which the A1962 is a variant of) and other Linux
  machines with a recent BlueZ. Reports welcome.
- **Not supported yet**: Siri Remote gen 2 and gen 3. Per the prior art they use other report IDs and another
  unlock ([sources](docs/PROTOCOL.md#other-remotes)). `list` shows them as another Apple device.
- Live use is Linux-only (hidraw, uinput); `--replay` and the tests run anywhere.

## Limitations

- Voice is not decoded. hidraw cuts the audio reports short; decoding needs a raw GATT client.
- After a long sleep, the button press that wakes the remote may be lost.
- If the remote won't reconnect after sleeping, BlueZ is probably using LE privacy: set `Privacy = off` in
  `/etc/bluetooth/main.conf` (details in [docs/PROTOCOL.md](docs/PROTOCOL.md#sleep-wake-and-reconnect)).
- Short, fast swipes (about a tenth of a second on the surface) often don't move focus: the gesture constants were
  tuned before touches were decoded exactly, and want a re-tune on hardware.
- One remote at a time: the first one found, or the one given with `--dev`.
- The ioctl numbers assume the common Linux layout (x86, ARM, RISC-V), not powerpc, mips or sparc.

## Tests

```sh
python3 -m unittest -v
```

They run on any OS. [tests/sample.jsonl](tests/sample.jsonl) is a real capture of a guided session (swipes, flicks,
taps, clicks, every button, two fingers), cut down to report bytes, relative times and step labels; it has no audio.

## Contributing

Issues and pull requests are welcome. Keep it small: standard library only, no frameworks, a test for any new
logic. Comments that start with `ponytail:` mark a deliberate shortcut and say where it stops being enough.

- **Another remote model**: please send the output of `python3 -m appleremote list` with the quoted name blanked out
  (a remote's name is its serial number, and other devices' names often include yours), the report descriptor
  (`xxd /sys/class/hidraw/hidrawN/device/report_descriptor`), and a short `monitor --raw` capture: each button once,
  one swipe per direction, a click. Leave Siri out unless you're happy to share your voice: audio reports carry it.
- **Protocol findings**: add them to [docs/PROTOCOL.md](docs/PROTOCOL.md) with a label (Measured, Reported or Guess)
  and, where you can, a capture that shows them.

## Credits

The protocol knowledge builds on these projects (no code was copied):
[Jakkumn/SiriRemoteESP](https://github.com/Jakkumn/SiriRemoteESP),
[Yanndroid/SiriRemote-Linux](https://github.com/Yanndroid/SiriRemote-Linux),
[azais-corentin/siri-remote](https://github.com/azais-corentin/siri-remote) and
[Jack-R1/SiriRemoteVoiceDecoder](https://github.com/Jack-R1/SiriRemoteVoiceDecoder).
The gesture smoothing is the One Euro filter (Casiez, Roussel and Vogel, CHI 2012). This code started inside a
private TV-launcher project and was extracted to share.

Apple, Siri and Apple TV are trademarks of Apple Inc. This project is not affiliated with or endorsed by Apple.

## License

MIT, see [LICENSE](LICENSE).
