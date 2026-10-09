"""Touch frames -> tvOS-style focus gestures, smoothed.

The feel modelled here:
- a resting finger that moves a little tilts the focused tile toward it (parallax); focus doesn't move;
- sliding steps the focus along one locked axis, one step per STEP of travel, paced (never a burst), with extra travel
  needed to step back the other way (no jitter ping-pong);
- a quick flick coasts a few more steps with friction; the next touch stops it;
- clicking selects (a button event, handled by the caller).
Input is smoothed with a One Euro filter (steady when slow, responsive when fast). The first SETTLE frames of a touch
(the finger landing) and the last GUARD frames before it lifts (the finger rolling off) are dropped: positions jump
there. Units are the remote's touch units (about 1650 across); t is seconds on the remote's clock. Every constant is a
calibration knob, tuned by feel on a gen-1 remote.

Use: g = Gestures(emit); g.frame(t, x, y, down) for every touch report; g.tick() often (about every 10 ms) for
coasting. emit(kind, **fields) gets ("touch", down), ("move", dir, src) with src "slide" or "flick", ("tilt", x, y).
"""
import math
import time

STEP = 560          # travel per focus step (~3 across the surface)
LOCK = 140          # travel before the axis locks...
LOCK_RATIO = 1.5    # ...and by how much the major axis must win
TURN = 1.25         # travel (in STEPs) on the other axis that switches the locked axis
BACK = 1.4          # travel (in STEPs) needed to step back against the last step
PACE = .12          # s between slide steps at the fastest
SETTLE = 3          # frames ignored after touch-down (the finger landing)
GUARD = 2           # frames held back and dropped at lift (the finger rolling off)
FLICK = 4200        # release speed (units/s) along the locked axis that coasts
FLICK_MIN = 200     # least travel along the axis for a flick
COAST_MAX = 4
COAST_FIRST, FRICTION = .11, 1.3
TILT_HZ, TILT_EPS, TILT_DEAD = 20, .05, .06


class OneEuro:
    """Casiez, Roussel & Vogel (CHI 2012): a low-pass whose cutoff rises with speed."""
    def __init__(self, min_cutoff=1.5, beta=.004, d_cutoff=1.0):
        self.mc, self.beta, self.dc, self.x, self.dx, self.t = min_cutoff, beta, d_cutoff, None, 0.0, None

    @staticmethod
    def _a(cutoff, dt):
        r = 2 * math.pi * cutoff * dt
        return r / (r + 1)

    def __call__(self, t, x):
        if self.x is None or t <= self.t:
            self.x, self.t = x, t
            return x
        dt = t - self.t
        dx = (x - self.x) / dt
        self.dx += self._a(self.dc, dt) * (dx - self.dx)
        self.x += self._a(self.mc + self.beta * abs(self.dx), dt) * (x - self.x)
        self.t = t
        return self.x


class Gestures:
    def __init__(self, emit, clock=time.monotonic):
        self.emit, self.clock = emit, clock  # clock: host time, for pacing and coasting
        self.down, self.coast, self.tilt, self.tilt_at = False, [], (0.0, 0.0), 0.0

    def frame(self, t, x, y, down):
        if down and not self.down:           # touch: stop coasting, start a gesture
            self.coast.clear()
            self.down, self.n, self.buf, self.hist = True, 0, [], []
            self.fx, self.fy = OneEuro(), OneEuro()
            self.anchor = self.axis = self.last = None
            self.steps, self.next_at, self.travel = 0, 0.0, 0.0
            self.emit("touch", down=True)
        if not down:
            if self.down:
                self._release()
            return
        self.n += 1
        if self.n <= SETTLE:
            return
        self.buf.append((t, x, y))           # hold GUARD frames back: they may be the roll-off before a lift
        if len(self.buf) <= GUARD:
            return
        t, x, y = self.buf.pop(0)
        self._move(t, self.fx(t, x), self.fy(t, y))

    def _move(self, t, x, y):
        self.hist = [h for h in self.hist if t - h[0] <= .08] + [(t, x, y)]
        if self.anchor is None:
            self.anchor = [x, y]
            return
        ax, ay = self.anchor
        dx, dy = x - ax, y - ay
        if self.axis is None and max(abs(dx), abs(dy)) >= LOCK:
            if abs(dx) >= LOCK_RATIO * abs(dy):
                self.axis = "x"
            elif abs(dy) >= LOCK_RATIO * abs(dx):
                self.axis = "y"
        elif self.axis == "x" and abs(dy) >= TURN * STEP:   # a deliberate turn
            self.axis, self.anchor[0], self.last = "y", x, None
        elif self.axis == "y" and abs(dx) >= TURN * STEP:
            self.axis, self.anchor[1], self.last = "x", y, None
        ax, ay = self.anchor
        d = (x - ax) if self.axis == "x" else (y - ay) if self.axis == "y" else 0.0
        if self.axis:
            name = ("right" if d > 0 else "left") if self.axis == "x" else ("up" if d > 0 else "down")
            need = STEP * (BACK if self.last and name != self.last else 1)
            now = self.clock()
            if abs(d) >= need and now >= self.next_at:
                self.emit("move", dir=name, src="slide")
                self.steps += 1
                self.last, self.next_at = name, now + PACE
                i = 0 if self.axis == "x" else 1
                # the anchor follows by one step, at most one step behind the finger (no stored-up burst)
                self.anchor[i] = (x if self.axis == "x" else y) - math.copysign(min(abs(d) - STEP, STEP * .5), d)
                if self.axis == "x":
                    self.anchor[1] = y
                else:
                    self.anchor[0] = x
        self._tilt(x - self.anchor[0], y - self.anchor[1])

    def _tilt(self, dx, dy, force=False):
        tx = max(-1.0, min(1.0, dx / (STEP * .8)))
        ty = max(-1.0, min(1.0, dy / (STEP * .8)))
        tx, ty = (0.0 if abs(v) < TILT_DEAD else round(v, 3) for v in (tx, ty))
        now = self.clock()
        if force or (abs(tx - self.tilt[0]) + abs(ty - self.tilt[1]) >= TILT_EPS and now - self.tilt_at >= 1 / TILT_HZ):
            self.tilt, self.tilt_at = (tx, ty), now
            self.emit("tilt", x=tx, y=ty)

    def _release(self):
        self.down = False
        self.emit("touch", down=False)
        if self.tilt != (0.0, 0.0):
            self._tilt(0, 0, force=True)
        h = self.hist                        # the held-back roll-off frames are dropped; the speed is from before them
        if self.axis and len(h) >= 3 and h[-1][0] > h[0][0]:
            i = 1 if self.axis == "x" else 2
            v = (h[-1][i] - h[0][i]) / (h[-1][0] - h[0][0])
            if abs(v) >= FLICK and abs(h[-1][i] - self.anchor[i - 1]) + self.steps * STEP >= FLICK_MIN:
                name = ("right" if v > 0 else "left") if self.axis == "x" else ("up" if v > 0 else "down")
                if not (self.last and name != self.last):
                    n = min(COAST_MAX if self.steps < 3 else 2, 1 + int((abs(v) - FLICK) / 3000))
                    due, gap = self.clock() + COAST_FIRST, COAST_FIRST
                    for _ in range(n):
                        self.coast.append((due, name))
                        gap *= FRICTION
                        due += gap

    def tick(self):
        """Send the coasting steps that are due now."""
        t = self.clock()
        while self.coast and self.coast[0][0] <= t:
            self.emit("move", dir=self.coast.pop(0)[1], src="flick")


def _selftest():
    out, now = [], [100.0]
    g = Gestures(lambda kind, **f: out.append((kind, f)), clock=lambda: now[0])
    t = [0.0]
    def touch(path, dt=.01):                 # path: list of (x, y); the clock advances with the frames
        for x, y in path:
            g.frame(t[0], x, y, True); t[0] += dt; now[0] += dt
        g.frame(t[0], 0, 0, False)
    def moves():
        return [f["dir"] for k, f in out if k == "move"]
    still = [(5 * math.sin(i), 4 * math.cos(i * 1.3)) for i in range(120)]     # a resting finger: jitter only
    touch(still)
    assert moves() == [] and not any(k == "tilt" and (f["x"] or f["y"]) for k, f in out), out[-5:]
    out.clear()
    landing = [(0, 0)] + [(400, 0)] * 2 + [(400 + 8 * i, 0) for i in range(40)]  # a landing jump, then a short slide
    touch(landing)
    assert moves() == [], moves()                                                # the jump is ignored
    out.clear()
    slide = [(9 * i, 0) for i in range(200)]                                       # 1800 units right at 900 u/s
    touch(slide)
    assert moves() == ["right", "right", "right"], moves()                        # ~3 steps per surface, paced
    out.clear()
    jiggle = [(9 * i, 0) for i in range(70)] + [(630 - 9 * i, 0) for i in range(40)]   # one step, then back 360
    touch(jiggle)
    assert moves() == ["right"], moves()                                          # no ping-pong back
    out.clear()
    flick = [(60 * i, 0) for i in range(14)]                                       # 6000 u/s, short
    touch(flick)                                                                   # too short to step while sliding:
    assert moves() == [] and 1 <= len(g.coast) <= COAST_MAX, (moves(), g.coast)   # it coasts instead
    pending = list(g.coast)
    now[0] = g.coast[0][0]; g.tick()
    assert [(k, f["src"]) for k, f in out if k == "move"] == [("move", "flick")]
    g.coast[:] = pending[1:] + [(pending[-1][0] + .2, "right"), (pending[-1][0] + .5, "right")]
    gaps = [b[0] - a[0] for a, b in zip(g.coast, g.coast[1:])]
    assert all(b > a for a, b in zip(gaps, gaps[1:])), gaps                      # friction
    g.frame(t[0], 0, 0, True)
    assert not g.coast                                                            # a touch stops coasting
    g.frame(t[0] + .01, 0, 0, False)
    print("gesture selftest ok")


if __name__ == "__main__":
    _selftest()
