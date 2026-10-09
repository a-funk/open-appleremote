# The gen-1 Siri Remote, as seen from Linux

What the gen-1 Apple Siri Remote (A1513, and the A1962 variant) sends over Bluetooth LE, as it arrives through
BlueZ and the kernel's hidraw interface, and how to unlock it, pair it, and keep it connected.

Every statement carries one of these labels:

- **Measured**: seen on an A1962 with BlueZ 5.72 and Linux 6.17. *(sample)* means you can check it yourself in
  [`tests/sample.jsonl`](../tests/sample.jsonl); counts without it come from the full capture the sample was cut
  from (about 19,800 reports over five connections, not published).
- **Reported**: measured while this code lived in an earlier private project, not reproducible from the sample.
- **Guess**: an interpretation that fits every report we have, not confirmed.

## Connection

| | |
|---|---|
| Transport | Bluetooth LE, HID over GATT. Apple vendor ID `0x004C`, product ID `0x026D`. **Reported** |
| On Linux | BlueZ's `hog` plugin turns it into a uhid device: `/dev/hidrawN` with `HID_ID=0005:0000004C:0000026D`, and **no input device**, because the report map is vendor-defined only: input report `0xFC` (20 bytes) and feature report `0xF0` (208 bytes). That is why the remote does nothing on Linux out of the box. **Reported** |
| Name | The remote advertises its serial number as its name. **Reported** |

### Pairing **Reported**

1. Hold **Menu + Volume Up** for about 5 seconds, close to the host. The remote starts advertising.
2. Pair from the host as a bonded, trusted device. LE pairing needs an agent. In `bluetoothctl`, after `scan on`,
   wait for a new device named like a serial number and use its address:

```text
agent NoInputNoOutput
default-agent
scan on
pair AA:BB:CC:DD:EE:FF
trust AA:BB:CC:DD:EE:FF
connect AA:BB:CC:DD:EE:FF
scan off
```

`bluetoothctl info AA:BB:CC:DD:EE:FF` shows Apple's manufacturer data (`Key: 0x004c`) and the Human Interface
Device service. Then `/dev/hidrawN` appears; `python3 -m appleremote list` finds it.

### Unlock

The remote sends nothing until feature report `0xF0` is set to the one byte `0xAF` **(Reported)**:
`ioctl(fd, HIDIOCSFEATURE(2), [0xF0, 0xAF])`. `hog` forwards it to GATT. Do it again on every connect.

Sometimes the remote answers with a 5-byte report `fc e0 BB 60 02` **(Measured: 2 of 5 connects)**. `BB` is the
buttons byte: `0x22` (Menu + Volume Up, held while waking it) *(sample)*; `0x20` was also seen **(Reported)**.
`60 02` is unknown.

## Report 0xFC

Every report has ID `0xFC`. **Measured** *(sample)*: byte 2 holds the buttons that are down right now, in every kind
of report, and byte 1 differs by kind. Reading byte 1 as "what follows" is a **Guess** that fits all of them.

| byte 1 | length | content |
|---|---|---|
| `0x00` | 3 | buttons only |
| `0x01`, `0x02` | 14, 21 | touch, with 1 or 2 fingers |
| `0x10` | 21, or 3 | audio while Siri is held (see Voice) |
| `0xE0` | 5 | unlock reply |

## Buttons: byte 2

**Measured** *(sample)*. A press sets its bit, a release clears it; several can be down at once (Menu + Volume Up
is `0x22`).

| bit | mask | button |
|---|---|---|
| 0 | `0x01` | TV / Home |
| 1 | `0x02` | Volume + |
| 2 | `0x04` | Volume − |
| 3 | `0x08` | Play/Pause |
| 4 | `0x10` | Siri |
| 5 | `0x20` | Menu |
| 6 | `0x40` | never seen |
| 7 | `0x80` | click (the touch surface pressed down) |

Read byte 2 from every report, not only the 3-byte ones. A click mostly arrives in touch reports only: of 20 clicks
in the full capture, 4 came with a 3-byte report, and those stayed set until the next 3-byte report, up to 9 clicks
later (sample: 1 of 3 clicks has a 3-byte report). The Siri bit is also in the audio reports, and the unlock reply
carries whatever is held.

Not seen yet: another button held while a finger is on the surface. If touch reports turned out not to carry it,
the decoder would report that button released early.

Earlier notes read bytes 1 and 2 as one little-endian mask (click `0x8000`, Menu `0x2000`, TV `0x0100`,
Play/Pause `0x0800`, Volume + `0x0200`, Volume − `0x0400`, Siri `0x1000`): the same bits, shifted left by 8.

## Touch

About 100 reports a second while a finger is on the surface, none otherwise. **Measured** *(sample)*

| byte | content |
|---|---|
| 0 | `0xFC` |
| 1 | number of finger records N: 1 (14-byte report) or 2 (21 bytes) |
| 2 | buttons (`0x80` while clicking) |
| 3 | `0x32` in every touch report; meaning unknown |
| 4-5 | the remote's clock, 16-bit little-endian, 2000 ticks per second |
| 6 | flags, meaning unknown (see the correction below) |
| 7 + 7k | finger record k, 7 bytes |

Finger record:

| offset | content |
|---|---|
| 0 | X bits 0-7 |
| 1 | X bits 8-11 (low nibble), Y bits 0-3 (high nibble) |
| 2 | Y bits 4-11 |
| 3-5 | change with the contact, zero once lifted, grow while pressing to click. **Guess**: contact size or pressure |
| 6 | state in bits 0-2 (below). Bit 3 stays the same through a touch (223 of 224) and differs between two fingers down together (106 of 106 reports), **Guess**: a contact id. Bits 4-7 change within a touch: unknown |

**Coordinates**: X = `b0 | (b1 & 0x0F) << 8`, Y = `b1 >> 4 | b2 << 4`, both signed 12-bit; +X is right, +Y is up.
Seen: X from −1818 to −182, Y from −1074 to 612, so about 1650 units per axis, not centred on 0. **Measured**.
Only 33 of 17,987 consecutive frames jump by more than 300 units on either axis; the earlier check found 7 jumps in
1,432 pairs for this encoding against hundreds for the alternatives **(Reported)**.

**Clock**: 20 ticks (10 ms) between touch reports in 19,208 of 19,250 pairs; 19 or 21 in 40, and 40 (a lost report)
twice. **Measured**. It wraps every 32.768 s: unwrap with `(now - last) mod 65536` *(sample: it wraps during the
clicks)*. Only touch reports carry it; a silence longer than one wrap loses whole wraps.

**Finger state** (bits 0-2 of record byte 6). **Measured**: 214 of the 227 touches in the full capture end with
state 7 then 0; the rest were cut off by the capture or never got past 2.

| state | where it shows up | name (Guess) |
|---|---|---|
| 0 | the last frame of a touch, after 7 | not tracking |
| 1 | rarely (22 finger records) | start in range |
| 2 | light contacts; 12 touches never got past it | hover |
| 3 | the first frame of a touch | make touch |
| 4 | the body of every touch (18,393 finger records) | touching |
| 5 | as the finger leaves | break touch |
| 6 | as the finger leaves | linger |
| 7 | the lift: position frozen, record bytes 3-5 zero | out of range |

The order matches the touch states of Apple's multitouch devices: macOS's private MultitouchSupport framework, as
reverse-engineered by several open-source projects, uses these names, and Linux's `hid-magicmouse` driver uses 3
for touch start and 4 for drag. This package counts a finger as down in states 3 to 6.

**Correction: byte 6 is not "finger down".** Earlier notes read byte 6 bit 0 as finger down. It stays the same
through the lift frames: in 180 of 227 touches it never changes (156 always set, 24 always clear). Decoding with it
merges touches: it finds 65 touch starts in the full capture instead of 210, and 5 instead of 32 in the sample.
Values seen: 0, 1, 4, 5, 8, 9 with one finger; with two, each nibble takes 0, 1, 8 or 9 (`0x00` to `0x91`), so
perhaps one nibble per finger (**Guess**). Meaning unknown.

**Two fingers** *(sample)*: 21-byte reports with N = 2. When a second finger landed, the first kept record 0; when
the first lifted, the next one-finger report carried the other finger (seen once each). A third finger
would need a 28-byte report, longer than the 20 bytes `hog` passes on: unknown.

**Landing and lift**: positions are noisier where the finger lands and leaves. Median frame-to-frame movement
of the first finger (|dx| + |dy|): 18 in state 4, 48 in state 5, 37 in state 6, 48 into state 3 from state 1 or 2.
**Measured**. The gesture engine drops `SETTLE` frames after landing and `GUARD` frames before the lift for this.

## Voice: research, not decodable through hidraw

While Siri is held, and briefly after, reports `fc 10 BB ...` arrive every 20 ms. **Measured**. Through hidraw each
is cut to 21 bytes (the report ID plus the 20 bytes of the report map), so most of every audio frame is lost. A
decoder needs the full GATT notifications: a raw GATT client instead of `hog`. See the prior art below.

| byte | seen | Guess |
|---|---|---|
| 2 | `0x10` while Siri is held, `0x00` after release | buttons |
| 3-4 | varies | unknown, perhaps a level |
| 5 | +1 per report (259 of 261 pairs); restarts at each press | sequence number |
| 6 | always 0 | high byte of the sequence? |
| 7 | 67 to 94 | frame length |
| 8 | always `0xB8` | Opus TOC byte: config 23 (CELT-only, wideband, 20 ms), mono, one frame, which fits the 20 ms spacing |
| 9-20 | | the first 12 bytes of that frame |

Short `fc 10 BB` reports without payload also appear around Siri presses (3 in the sample). The sample contains no
audio reports: they carry the speaker's voice.

## Sleep, wake and reconnect

- After a few idle minutes the remote disconnects; the hidraw node goes away and reads fail with EIO. Any button
  press reconnects it, but after a long sleep the press that wakes it may be lost. **Reported**
- **Reconnect fails when BlueZ uses LE privacy.** With `Privacy = device` (or any LE privacy) in
  `/etc/bluetooth/main.conf`, the host connects from a rotating resolvable private address, and every wake ends in
  HCI Disconnect `0x3E`, "Connection Failed to be Established", in a tight retry loop. **Reported**. Why: the remote
  (Bluetooth 4.0) probably can't resolve that address. **Guess**
  - Fix: `Privacy = off` in the `[General]` section of `/etc/bluetooth/main.conf`, then restart bluetooth.
  - To try it without a config change: `bluetoothctl power off`, `sudo btmgmt privacy off`, `bluetoothctl power on`
    (lasts until the next restart).
  - Re-pair once afterwards if the remote stored the old address.
  - With a public host address, the remote reconnects on any button press.
- On a MediaTek MT7925 controller the kernel logs "ACL packet for unknown connection handle 3837". It is a firmware
  debug event and harmless. **Reported**

## Other remotes

Untested. Per SiriRemoteESP (`report_decoder.h`), azais-corentin/siri-remote and Yanndroid/SiriRemote-Linux (links
below), the gen-2 and gen-3 remotes use other report IDs (buttons in `0xFB`, audio in `0xFA`) and another unlock
(`0xF0 0x00` to the first Report characteristic without notify, or `0xAF` to every writable non-input report), so
this package doesn't decode them. `python3 -m appleremote list` shows any Apple Bluetooth HID device; the README says
what to send us.

## Prior art

Protocol knowledge only; no code was copied from:

- [Jakkumn/SiriRemoteESP](https://github.com/Jakkumn/SiriRemoteESP)
- [Yanndroid/SiriRemote-Linux](https://github.com/Yanndroid/SiriRemote-Linux)
- [azais-corentin/siri-remote](https://github.com/azais-corentin/siri-remote)
- [Jack-R1/SiriRemoteVoiceDecoder](https://github.com/Jack-R1/SiriRemoteVoiceDecoder)

The gesture engine's smoothing is the One Euro filter: Casiez, Roussel and Vogel, CHI 2012.
