import math
from collections import deque
import time
from typing import Tuple, Optional


class LowPassFilter:
    def __init__(self, alpha: float = 0.5):
        self.alpha = alpha
        self.y: Optional[float] = None
        self.s: Optional[float] = None

    def filter(self, value: float, alpha: Optional[float] = None) -> float:
        if alpha is not None:
            self.alpha = alpha
        if self.y is None:
            self.s = value
        else:
            self.s = self.alpha * value + (1.0 - self.alpha) * self.s
        self.y = value
        return self.s

    def reset(self):
        self.y = None
        self.s = None


class OneEuroFilter:
    """
    Standard One Euro Filter (Casiez et al., CHI 2012).
    Minimizes jitter at low speeds (via min_cutoff) while maintaining low latency
    during high-speed sweeps (via beta).
    """

    def __init__(
        self,
        freq: float = 60.0,
        min_cutoff: float = 1.0,
        beta: float = 0.007,
        d_cutoff: float = 1.0,
    ):
        self.freq = freq
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff

        self.x_filt = LowPassFilter()
        self.dx_filt = LowPassFilter()
        self.last_time: Optional[float] = None

    def _alpha(self, cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt) if dt > 0 else 1.0

    def filter(self, x: float, timestamp: Optional[float] = None) -> float:
        if timestamp is None:
            timestamp = time.time()

        if self.last_time is None:
            dt = 1.0 / self.freq
        else:
            dt = max(timestamp - self.last_time, 1e-6)

        self.last_time = timestamp

        # Estimate derivative (velocity)
        prev_x = self.x_filt.s
        dx = 0.0 if prev_x is None else (x - prev_x) / dt
        edx = self.dx_filt.filter(dx, self._alpha(self.d_cutoff, dt))

        # Dynamic cutoff frequency based on velocity magnitude
        cutoff = self.min_cutoff + self.beta * abs(edx)
        return self.x_filt.filter(x, self._alpha(cutoff, dt))

    def reset(self):
        self.x_filt.reset()
        self.dx_filt.reset()
        self.last_time = None


class DeadbandOneEuroFilter2D:
    """
    2D coordinate filter combining:
    1. Tunable spatial deadband radius to lock micro-tremors when resting.
    2. Independent OneEuroFilters for X and Y for continuous, low-latency trajectory.
    """

    def __init__(
        self,
        deadband_radius: float = 12.0,
        min_cutoff: float = 0.8,
        beta: float = 0.008,
        d_cutoff: float = 1.0,
        move_confirm_frames: int = 3,
        settle_frames: int = 8,
    ):
        self.deadband_radius = deadband_radius
        self.anchor_x: Optional[float] = None
        self.anchor_y: Optional[float] = None
        self.move_confirm_frames = max(2, int(move_confirm_frames))
        self.settle_frames = max(4, int(settle_frames))
        self._candidate = deque(maxlen=self.move_confirm_frames)
        self._settle = deque(maxlen=self.settle_frames)
        self._moving = False

        self.filter_x = OneEuroFilter(min_cutoff=min_cutoff, beta=beta, d_cutoff=d_cutoff)
        self.filter_y = OneEuroFilter(min_cutoff=min_cutoff, beta=beta, d_cutoff=d_cutoff)

    def filter(
        self, raw_x: float, raw_y: float, timestamp: Optional[float] = None
    ) -> Tuple[float, float]:
        if timestamp is None:
            timestamp = time.time()

        if self.anchor_x is None or self.anchor_y is None:
            self.anchor_x = raw_x
            self.anchor_y = raw_y

        point = (float(raw_x), float(raw_y))
        dist = math.hypot(point[0] - self.anchor_x, point[1] - self.anchor_y)
        if not self._moving:
            if dist < self.deadband_radius:
                self._candidate.clear()
                input_x, input_y = self.anchor_x, self.anchor_y
            else:
                self._candidate.append(point)
                if len(self._candidate) < self.move_confirm_frames:
                    input_x, input_y = self.anchor_x, self.anchor_y
                else:
                    points = list(self._candidate)
                    dx = points[-1][0] - points[0][0]
                    dy = points[-1][1] - points[0][1]
                    span = math.hypot(dx, dy)
                    spread = max(math.hypot(p[0] - points[0][0], p[1] - points[0][1]) for p in points)
                    anchor_dx = point[0] - self.anchor_x
                    anchor_dy = point[1] - self.anchor_y
                    progressing = dx * anchor_dx + dy * anchor_dy > 3.0
                    outside_deadband = all(math.hypot(p[0] - self.anchor_x, p[1] - self.anchor_y) >= self.deadband_radius for p in points)
                    if (span >= self.deadband_radius * 0.55 or (outside_deadband and progressing)) and spread <= self.deadband_radius * 2.5:
                        self._moving = True
                        self._settle.clear()
                        input_x, input_y = point
                    else:
                        self._candidate.clear()
                        input_x, input_y = self.anchor_x, self.anchor_y
        else:
            input_x, input_y = point
            self._settle.append(point)
            if len(self._settle) == self.settle_frames:
                xs = [p[0] for p in self._settle]
                ys = [p[1] for p in self._settle]
                spread = max(math.hypot(p[0] - self._settle[0][0], p[1] - self._settle[0][1]) for p in self._settle)
                if spread < self.deadband_radius * 0.65:
                    self.anchor_x = sorted(xs)[len(xs) // 2]
                    self.anchor_y = sorted(ys)[len(ys) // 2]
                    self._moving = False
                    self._candidate.clear()
                    self._settle.clear()
                    input_x, input_y = self.anchor_x, self.anchor_y

        smooth_x = self.filter_x.filter(input_x, timestamp)
        smooth_y = self.filter_y.filter(input_y, timestamp)
        return smooth_x, smooth_y

    def reset(self):
        self.anchor_x = None
        self.anchor_y = None
        self._candidate.clear()
        self._settle.clear()
        self._moving = False
        self.filter_x.reset()
        self.filter_y.reset()
