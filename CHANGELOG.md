# Changelog

## Unreleased

- Tested on hardware (A1962, Linux 6.17, BlueZ 5.72, MediaTek MT7925): `list`, `monitor` and `lab` find, unlock and
  read the remote as a normal user through the udev rule, and the remote reconnects after it sleeps once LE privacy
  is off (docs/PROTOCOL.md). Not yet tested on hardware: `keys` (uinput).
- Docs: the voice section was wrong about where the audio is cut. The remote sends whole 101-byte frames, and
  bluetoothd's 23-byte ATT reads cut them before hidraw. It now has the frame layout, measured with btmon.
- Voice: `python3 -m appleremote voice` saves each Siri press as a 16 kHz WAV and prints a JSON line per press.
  - `appleremote/hcitap.py` reads the whole frames from the kernel's HCI monitor (it needs `CAP_NET_RAW`). It keeps
    only the remote's own encrypted link and its voice frames, drops everything else before buffering any of it, and
    gives up its capabilities (and root, under sudo; as plain root it won't run) before it waits for the remote or
    reads a packet.
  - `appleremote/voice.py` assembles each press by sequence number, fills lost frames with libopus's concealment, and
    decodes through libopus (ctypes; optional, loaded only to decode: `libopus0`).
  - `voice` keeps the remote unlocked through hidraw, as `monitor` does (`device.keep_unlocked()`): the remote sends
    no voice until it is. The WAVs are 0600, in a 0700 directory; a press with no frame held (the tail of a Siri press
    that woke the remote) gets none.
  - `--serve PATH` serves the frames to other apps on a unix socket; `--socket PATH` reads them from one; `--replay`
    decodes a btmon capture on any OS.
  - `systemd/appleremote-voice.service`: a sandboxed system unit that runs a root-owned copy of the tap as a user of
    its own (`DynamicUser=`) with `CAP_NET_RAW` alone, serving your user's processes (`--allow-uid`). Its socket is
    there from the start; clients wait with it for the remote.
  - The lab shows the voice reports' level and sequence number while Siri is held; hidraw has no more of the audio.
  - Tested on synthetic frames, on Jack-R1/SiriRemoteVoiceDecoder's recorded gen-1 frames wrapped in a synthetic btmon
    capture, on real pairing, reconnect and gamepad captures (no voice: it keeps nothing of them), and in Linux
    containers, the unit under systemd 257 with a socket standing in for the monitor; not yet live on hardware.

## 0.1.0 (2026-10-09)

First release, extracted from a private TV-launcher project.

- `appleremote.device`: finds the remote's hidraw node, unlocks it, reads its reports, and survives sleep and
  reconnects; replays captures on any OS.
- `appleremote.decode`: touch (one or two fingers, the remote's clock unwrapped), buttons from every report kind,
  and raw audio chunks.
- `appleremote.gestures`: the tvOS-style engine (One Euro smoothing, landing and lift guards, tilt, paced slide
  steps with axis lock, flick coasting with friction) and its self-test.
- `appleremote.keys`: optional uinput keyboard with a JSON key map.
- CLI: `list`, `monitor`, `gestures`, `keys`, `lab` (a live web lab on 127.0.0.1), with `--replay` for captures.
- `docs/PROTOCOL.md`, a udev rule, an example systemd user unit, tests with a real capture.

Protocol corrections over the earlier notes: byte 6 bit 0 of a touch report is not "finger down" (the finger state is
the low 3 bits of each finger record's last byte); byte 2 is the button byte in every report, which is where most
clicks arrive; in touch reports byte 1 is the finger count, and two-finger reports are 21 bytes.

Known limits:

- The live paths have not been re-run on hardware since extraction: they were rewritten and checked only with recorded
  data and on macOS. That covers `reports()` (hidraw read, unlock, sleep and reconnect), `keys` (uinput) and the
  udev rule.
- The gestures want a re-tune on hardware: with exact touch boundaries, short fast swipes often don't step (6 of the 12 up
  and down swipes in the sample). A candidate, measured on the sample only: count a finger as lifted from state 5
  (break) on and set `GUARD = 0`; then 11 of 12 swipes step, taps and clicks still don't, and the self-test passes.
  But fast swipes then coast too far (one up swipe gives 5 moves: a slide and 4 coasting), which fails the "at most
  2 moves per swipe" check in `tests/test_appleremote.py`. Re-tune `FLICK` and `COAST_MAX` with it, on hardware.
