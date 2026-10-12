# open-appleremote

**Use the first-generation Apple Siri Remote on Linux**: the touch remote of the 2015 and 2017 Apple TVs (A1513,
A1962), not the older infrared Apple Remote. Touch surface, buttons and tvOS-style gestures are decoded in plain
Python 3 with no dependencies; an optional virtual keyboard makes an old remote work in Kodi, a browser, an emulator
or any other app, and the remote's microphone records what you say while you hold Siri (decoding it needs libopus).

Paired with Linux, the remote does nothing: BlueZ exposes it only as a raw `/dev/hidrawN` node with a vendor-defined
report map, and it stays silent until it is unlocked. This package unlocks it, decodes its reports, and turns them
into events, gestures or key presses. [docs/PROTOCOL.md](docs/PROTOCOL.md) documents everything it sends.

Status: 0.1.0, an early release, tested on an A1962 with Linux: finding, unlocking and reading the remote as a normal
user, and reconnecting after it sleeps. The virtual keyboard (`keys`) is not hardware-tested yet, and the voice
(`voice`, not released yet) is tested only on synthetic frames and replayed captures. Reports are welcome (see
[CHANGELOG.md](CHANGELOG.md)).

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
sudo python3 -m appleremote voice --out voice/   # each Siri press as a WAV file: see Voice
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
- `voicechunk`: an audio report while Siri is held. Through hidraw it holds only the start of an Opus frame: its
  level and sequence number, not enough to decode
  ([docs/PROTOCOL.md](docs/PROTOCOL.md#voice-opus-frames-cut-short-by-bluez-before-hidraw)). `voice` gets the whole
  frames (see Voice).
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

## Voice

Hold Siri and speak: `voice` saves each press as a WAV file (16 kHz, mono) for whisper or any other speech
recognizer, and prints a line per press. A quick try, on Linux:

```sh
sudo apt install libopus0                          # the decoder (Debian, Ubuntu); nothing else needs it
sudo python3 -m appleremote voice --out voice/     # Ctrl-C to stop
```

```json
{"kind": "press", "wav": "voice/siri-20261011-201500-01.wav", "frames": 213, "lost": 0, "held": 163, "seconds": 4.26, "level": 21034, "ended": true}
```

`frames` counts the press's 20 ms Opus frames: while Siri is held, then about a second more after you let go. `lost`
counts frames that never arrived (libopus fills them in), `held` the frames sent with Siri held, `level` is the
highest loudness the remote reported, and `ended` says whether the stream's end marker came. Without `--out` it only
prints the lines.

**Why sudo.** The remote sends each frame whole, but BlueZ keeps only its first 20 bytes before hidraw
([docs/PROTOCOL.md](docs/PROTOCOL.md#voice-opus-frames-cut-short-by-bluez-before-hidraw)), so `voice` reads the frames
below BlueZ, from the kernel's HCI monitor (what btmon reads). Opening it needs `CAP_NET_RAW`. Under sudo, `voice`
opens it, then goes back to your user, with no capability left, before it reads a packet; the WAV files are yours.

**What it keeps.** The HCI monitor shows every Bluetooth device's traffic on the machine: keyboard input, pairing
keys, headsets. [appleremote/hcitap.py](appleremote/hcitap.py), the only code that opens it (standard library only,
short enough to read before you run it), keeps the remote's voice and nothing else:

- data only from the remote's own link, and only once that link is encrypted with its bond's key: the frame layout is
  public and an address can be borrowed, but the key can't;
- of that, only voice frames and end markers (notifications on ATT handle `0x0023`);
- everything else is dropped as it is parsed, before any of it is buffered. It never logs a payload or an address.

**Finding the remote.** `voice` reads the address BlueZ gives the remote's hidraw node, so it waits until the remote
is connected (press a button). If it keeps waiting, give the address: `--remote AA:BB:CC:DD:EE:FF`
(`bluetoothctl devices Paired` lists it).

**To keep it running** without sudo, [systemd/appleremote-voice.service](systemd/appleremote-voice.service) runs a
root-owned copy of `hcitap.py` as your user, with `CAP_NET_RAW` alone (`AmbientCapabilities=`, `NoNewPrivileges=` and
a tight sandbox), serving the frames on `/run/appleremote/voice.sock`. Its first lines say how to install it. Then, as
your user:

```sh
python3 -m appleremote voice --socket /run/appleremote/voice.sock --out voice/
```

Don't give the capability to the Python interpreter (`setcap cap_net_raw+ep /usr/bin/python3`): every Python script on
the machine would get it.

**Other apps.** `--serve PATH` serves the frames on a unix socket instead of decoding them, as the unit does (mode
0600, your user's processes only, 4 at most). Each message is the `CLOCK_MONOTONIC` time in nanoseconds (8 bytes,
little-endian), then the frame (101 bytes) or an end marker (2 bytes). From Python:

```python
from appleremote import voice

for press in voice.presses(voice.client("/run/appleremote/voice.sock")):
    voice.write_wav("press.wav", voice.decode(press.packets()))
```

**No remote at hand, or another OS:** `--replay FILE` decodes a capture made with `sudo btmon -w FILE`, adding
`--remote` if the remote connected during the capture. A btmon capture holds all the machine's Bluetooth traffic,
your voice included: keep it to yourself.

Through hidraw alone, with no capability, the lab shows the voice reports' level and sequence number while Siri is
held.

## Ideas for an old remote

- A couch remote for Kodi, Jellyfin, mpv, VLC, a browser in TV mode, or a game launcher.
- A presentation clicker: swipe for the next slide, click to start a video.
- A home-automation remote: pipe `gestures` into a script that calls your lights, scenes or music.
- A voice remote: hold Siri, say a command, and hand the WAV to a speech recognizer (whisper.cpp, Vosk).
- A small touchpad for a kiosk or a Raspberry Pi media box.
- An instrument: the touch surface streams x/y at 100 Hz, enough for a theremin or a filter sweep.
- Two-finger gestures: the decoder reports both fingers; nothing uses them yet.

## Supported and tested

- **Tested**: Siri Remote gen 1, model A1962, on BlueZ 5.72, Linux 6.17 (arm64, MediaTek MT7925 Bluetooth). The
  0.1.0 code passed there: `list`, `monitor` and `lab` as a normal user through the udev rule, and the reconnect
  after sleep once LE privacy is off. The virtual keyboard (`keys`) isn't hardware-tested yet.
- **Should work, untested**: the A1513 (the original gen-1 remote, which the A1962 is a variant of) and other Linux
  machines with a recent BlueZ. Reports welcome.
- **Not supported yet**: Siri Remote gen 2 and gen 3. Per the prior art they use other report IDs and another
  unlock ([sources](docs/PROTOCOL.md#other-remotes)). `list` shows them as another Apple device.
- **Voice** (`voice`, not released yet): tested on synthetic frames, and its decoding on the recorded gen-1 frames of
  [Jack-R1/SiriRemoteVoiceDecoder](https://github.com/Jack-R1/SiriRemoteVoiceDecoder), replayed. It hasn't run live in
  this package yet. It ports code from the private project this package came from, which picked exactly the remote's
  whole voice frames out of a btmon capture of an A1962.
- Live use is Linux-only (hidraw, uinput, the HCI monitor); `--replay` and the tests run anywhere.

## Limitations

- Voice needs more than the rest: BlueZ keeps only the first 20 bytes of each voice frame before hidraw sees it
  ([docs/PROTOCOL.md](docs/PROTOCOL.md#voice-opus-frames-cut-short-by-bluez-before-hidraw)), so `voice` reads the
  frames from the HCI monitor, which takes `CAP_NET_RAW` (sudo, or the system unit), and decodes them with libopus.
  Without those, hidraw gives each frame's level and sequence number only.
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

They run on any OS; the voice tests that need libopus skip without it, and those of the HCI tap's privileges and
socket skip off Linux. [tests/sample.jsonl](tests/sample.jsonl) is a real capture of a guided session (swipes, flicks,
taps, clicks, every button, two fingers), cut down to report bytes, relative times and step labels; it has no audio.
The voice tests build their frames; a recorded voice never goes in this repository.

## Contributing

Issues and pull requests are welcome. Keep it small: standard library only, no frameworks, a test for any new
logic. Comments that start with `ponytail:` mark a deliberate shortcut and say where it stops being enough.

- **Another remote model**: please send the output of `python3 -m appleremote list` with the quoted name blanked out
  (a remote's name is its serial number, and other devices' names often include yours), the report descriptor
  (`xxd /sys/class/hidraw/hidrawN/device/report_descriptor`), and a short `monitor --raw` capture: each button once,
  one swipe per direction, a click. Leave Siri out unless you're happy to share your voice: audio reports carry it.
  Never attach a btmon capture or a WAV of `voice`: they hold your voice, and a btmon capture all your Bluetooth
  traffic.
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
