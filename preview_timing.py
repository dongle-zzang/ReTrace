"""Small, thread-safe arrival/PTS counters; no GStreamer dependency."""

import threading
import time


class FrameTiming:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.started = clock()
        self.last_arrival = None
        self.last_pts = None
        self.total = 0
        self.previous_total = 0
        self.max_gap_ms = 0.0
        self.gaps_over_100ms = 0
        self.pts_min_ms = None
        self.pts_max_ms = None
        self.pts_backwards = 0

    def observe(self, pts_ns=None):
        now = self.clock()
        with self.lock:
            if self.last_arrival is not None:
                gap = (now - self.last_arrival) * 1000
                self.max_gap_ms = max(self.max_gap_ms, gap)
                self.gaps_over_100ms += gap > 100
            if pts_ns is not None and self.last_pts is not None:
                delta = (pts_ns - self.last_pts) / 1_000_000
                self.pts_backwards += delta < 0
                self.pts_min_ms = delta if self.pts_min_ms is None else min(self.pts_min_ms, delta)
                self.pts_max_ms = delta if self.pts_max_ms is None else max(self.pts_max_ms, delta)
            self.last_arrival = now
            self.last_pts = pts_ns
            self.total += 1

    def snapshot(self):
        now = self.clock()
        with self.lock:
            result = {
                "frames": self.total,
                "fps": round((self.total - self.previous_total) / max(now - self.started, 1e-9), 2),
                "max_gap_ms": round(self.max_gap_ms, 2),
                "gaps_over_100ms": self.gaps_over_100ms,
                "pts_min_ms": None if self.pts_min_ms is None else round(self.pts_min_ms, 2),
                "pts_max_ms": None if self.pts_max_ms is None else round(self.pts_max_ms, 2),
                "pts_backwards": self.pts_backwards,
            }
            self.previous_total = self.total
            self.started = now
            self.max_gap_ms = 0.0
            self.gaps_over_100ms = self.pts_backwards = 0
            self.pts_min_ms = self.pts_max_ms = None
            return result
