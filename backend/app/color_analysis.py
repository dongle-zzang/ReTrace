"""CPU HSV features and temporal stabilization; no model or stream connections."""
from dataclasses import dataclass
import math

import cv2
import numpy as np


def histogram(image, polygon, min_pixels=64):
    height, width = image.shape[:2]
    vertices = np.rint([[p['x'] * (width - 1), p['y'] * (height - 1)] for p in polygon]).astype(np.int32)
    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.fillPoly(mask, [vertices], 255)
    if cv2.countNonZero(mask) < min_pixels:
        return None
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    # Hue is unstable for gray/dark pixels; collapse those hue bins.
    hsv[:, :, 0][(hsv[:, :, 1] < 32) | (hsv[:, :, 2] < 32)] = 0
    hist = cv2.calcHist([hsv], [0, 1, 2], mask, [12, 4, 4], [0, 180, 0, 256, 0, 256]).ravel()
    return hist / hist.sum()


def color_score(hist, baseline=None):
    if baseline is not None:
        # Hellinger distance: zero for the empty reference, one for disjoint colors.
        return float(np.sqrt(max(0.0, 1.0 - float(np.sqrt(hist * np.asarray(baseline)).sum()))))
    nonzero = hist[hist > 0]
    return float(-(nonzero * np.log(nonzero)).sum() / math.log(len(hist)))


@dataclass
class StableState:
    status: str = 'UNKNOWN'
    candidate: str = 'UNKNOWN'
    count: int = 0

    def update(self, score, threshold, hysteresis, confirm_frames):
        if score is None:
            self.status, self.candidate, self.count = 'UNKNOWN', 'UNKNOWN', 0
            return self.status
        candidate = ('OCCUPIED' if score >= threshold + hysteresis else
                     'EMPTY' if score <= threshold - hysteresis else self.status)
        if candidate == 'UNKNOWN' or candidate == self.status:
            self.candidate, self.count = 'UNKNOWN', 0
        else:
            self.count = self.count + 1 if self.candidate == candidate else 1
            self.candidate = candidate
            if self.count >= confirm_frames:
                self.status, self.count = candidate, 0
        return self.status


def aggregate(zones):
    states = [zone['status'] for zone in zones]
    conflict = 'OCCUPIED' in states and 'EMPTY' in states
    status = ('OCCUPIED' if 'OCCUPIED' in states else
              'EMPTY' if states and all(s == 'EMPTY' for s in states) else 'UNKNOWN')
    return status, conflict
