# Changelog

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
