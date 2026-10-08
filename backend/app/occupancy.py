"""Replayable normalized geometry and temporal occupancy policy, without GPU/IO."""
from dataclasses import dataclass, fields
import math


@dataclass(frozen=True)
class ParkingPolicy:
    footprint_height: float = .25
    horizontal_inset: float = .1
    enter_overlap: float = .55
    exit_overlap: float = .35
    min_space_coverage: float = .1
    min_confidence: float = .4
    min_tracker_confidence: float = .2
    stationary_seconds: float = 10
    empty_seconds: float = 5
    vacancy_seconds: float = 12
    uncertain_seconds: float = 10
    max_observation_gap: float = 5
    max_speed: float = .015
    max_displacement: float = .025
    ambiguity_margin: float = .05
    require_anchor: bool = True
    # Absence of detections proves vacancy only on cameras whose detector was
    # validated against real footage; elsewhere absence stays unknown.
    validated_cameras: tuple = ()
    min_empty_observations: int = 3
    # An occupant that vanished without being seen leaving is first unknown,
    # then empty only after this much continuous valid absence.
    lost_empty_seconds: float = 60

    def __post_init__(self):
        cameras = self.validated_cameras
        if (not isinstance(cameras, (list, tuple))
                or not all(isinstance(c, str) and c.strip() for c in cameras)):
            raise ValueError("Invalid validated parking cameras")
        object.__setattr__(self, "validated_cameras", tuple(c.strip() for c in cameras))
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == "validated_cameras":
                continue
            if field.name == "require_anchor":
                if type(value) is not bool:
                    raise ValueError("Invalid parking anchor policy")
            elif type(value) not in (float, int) or not math.isfinite(value) or value < 0:
                raise ValueError("Invalid parking policy")
        if not (0 < self.footprint_height <= 1 and 0 <= self.horizontal_inset < .5
                and 0 <= self.exit_overlap <= self.enter_overlap <= 1
                and 0 <= self.min_space_coverage <= 1
                and 0 <= self.min_confidence <= 1 and 0 <= self.min_tracker_confidence <= 1
                and 0 <= self.ambiguity_margin <= 1 and self.max_observation_gap > 0
                and type(self.min_empty_observations) is int and self.min_empty_observations >= 1
                and self.lost_empty_seconds >= self.empty_seconds):
            raise ValueError("Invalid parking policy bounds")


def area(points):
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1]))) / 2


def intersection_area(polygon, rectangle):
    """Clip a simple (possibly concave) polygon to an axis-aligned rectangle."""
    points = list(polygon)
    for axis, boundary, keep_greater in ((0, rectangle[0], True), (0, rectangle[2], False),
                                         (1, rectangle[1], True), (1, rectangle[3], False)):
        output = []
        for a, b in zip(points, points[1:] + points[:1]):
            inside_a = a[axis] >= boundary if keep_greater else a[axis] <= boundary
            inside_b = b[axis] >= boundary if keep_greater else b[axis] <= boundary
            if inside_a != inside_b:
                t = (boundary - a[axis]) / (b[axis] - a[axis])
                output.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
            if inside_b:
                output.append(b)
        points = output
        if not points:
            return 0.0
    return area(points)


def contains(polygon, point):
    x, y = point
    inside = False
    for a, b in zip(polygon, polygon[1:] + polygon[:1]):
        cross = (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])
        if abs(cross) < 1e-12 and min(a[0], b[0]) <= x <= max(a[0], b[0]) and min(a[1], b[1]) <= y <= max(a[1], b[1]):
            return True
        if (a[1] > y) != (b[1] > y) and x < a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1]):
            inside = not inside
    return inside


def footprint(vehicle, frame, policy):
    b = vehicle.bbox
    left, top = max(0, b.x / frame.bbox_width), max(0, b.y / frame.bbox_height)
    right = min(1, (b.x + b.width) / frame.bbox_width)
    bottom = min(1, (b.y + b.height) / frame.bbox_height)
    if right <= left or bottom <= top:
        return None
    width = right - left
    return (left + width * policy.horizontal_inset, bottom - (bottom - top) * policy.footprint_height,
            right - width * policy.horizontal_inset, bottom)


@dataclass
class Memory:
    revision: int
    identity: tuple
    state: str = "unknown"
    last_timestamp: object = None
    frame_number: int = -1
    track_id: int | None = None
    origin: tuple | None = None
    previous: tuple | None = None
    stationary_since: object = None
    absent_since: object = None
    uncertain_since: object = None
    absent_observations: int = 0
    lost: bool = False  # Occupied space whose vehicle vanished without an observed departure.
    departed: bool = False  # The occupant track was observed moving or outside the space.


@dataclass(frozen=True)
class Decision:
    space_id: str
    revision: int
    occupancy: str
    reason: str
    timestamp: object
    evidence: dict


class OccupancyEvaluator:
    def __init__(self, policy=None):
        self.policy = policy or ParkingPolicy()
        self.memory = {}
        self.diagnostics = {}

    def forget_camera(self, camera_id, spaces):
        for space in spaces:
            self.memory.pop(space.space_id, None)
        self.diagnostics.pop(camera_id, None)

    def reset(self, space, identity, old):
        """Restart timers; a same-revision occupant lost across a gap stays lost."""
        memory = Memory(space.revision, identity)
        memory.lost = old is not None and old.revision == space.revision and (old.lost or old.state == "occupied")
        self.memory[space.space_id] = memory
        return memory

    def observe(self, frame, runtime_session, spaces):
        policy = self.policy
        identity = (runtime_session, frame.generation)
        polygons = {s.space_id: [(p["x"], p["y"]) for p in s.polygon] for s in spaces}
        candidates, uncertain, present = {}, set(), set()
        scores = {s.space_id: [] for s in spaces}
        validated = frame.camera_id in policy.validated_cameras
        if frame.vehicle_detection_enabled and frame.vehicle_inference_done:
            for vehicle in frame.vehicles:
                rect = footprint(vehicle, frame, policy)
                if rect is None:
                    uncertain.update(polygons)  # Malformed geometry cannot prove vacancy.
                    continue
                size = (rect[2] - rect[0]) * (rect[3] - rect[1])
                anchor = ((rect[0] + rect[2]) / 2, rect[3])
                matches = []
                for space in spaces:
                    polygon = polygons[space.space_id]
                    intersect = intersection_area(polygon, rect)
                    overlap, coverage = intersect / size, intersect / area(polygon)
                    evidence = {"track_id": str(vehicle.track_id) if vehicle.track_id is not None else None,
                                "overlap": overlap, "space_coverage": coverage,
                                "footprint": list(rect), "anchor_inside": contains(polygon, anchor)}
                    scores[space.space_id].append(evidence)
                    scores[space.space_id].sort(key=lambda e: e["overlap"], reverse=True)
                    del scores[space.space_id][20:]
                    old = self.memory.get(space.space_id)
                    threshold = policy.exit_overlap if old and old.state == "occupied" else policy.enter_overlap
                    if overlap >= threshold and coverage >= policy.min_space_coverage:
                        if not policy.require_anchor or evidence["anchor_inside"]:
                            matches.append((overlap, space.space_id, evidence))
                        else:
                            uncertain.add(space.space_id)
                    elif intersect > 0 and evidence["anchor_inside"]:
                        uncertain.add(space.space_id)
                matches.sort(reverse=True, key=lambda m: (m[0], m[1]))
                reliable = (vehicle.track_id is not None and vehicle.confidence is not None
                            and vehicle.confidence >= policy.min_confidence
                            and vehicle.tracker_confidence is not None
                            and vehicle.tracker_confidence >= policy.min_tracker_confidence)
                if reliable:
                    present.add(vehicle.track_id)
                if not reliable or (len(matches) > 1 and matches[0][0] - matches[1][0] <= policy.ambiguity_margin):
                    uncertain.update(m[1] for m in matches)
                elif matches:
                    overlap, key, evidence = matches[0]
                    if key not in candidates or overlap > candidates[key][0]:
                        candidates[key] = (overlap, vehicle, anchor, evidence)
                    # A vehicle can occupy at most one space; overlap with others
                    # remains uncertain rather than declaring them vacant.
                    uncertain.update(m[1] for m in matches[1:])
        decisions = []
        for space in spaces:
            memory = self.memory.get(space.space_id)
            if memory is None or memory.revision != space.revision or memory.identity != identity:
                memory = self.reset(space, identity, memory)
            if memory.last_timestamp is not None:
                dt = (frame.timestamp - memory.last_timestamp).total_seconds()
                if dt <= 0 or frame.frame_number <= memory.frame_number:
                    continue  # Re-polling the same snapshot never advances timers.
                if dt > policy.max_observation_gap:
                    memory = self.reset(space, identity, memory)
            else:
                dt = 0
            now = frame.timestamp
            reason = "confirming_empty"
            evidence = {"candidates": scores[space.space_id], "stationary_seconds": 0.0,
                        "runtime_session": runtime_session, "generation": frame.generation,
                        "frame_number": frame.frame_number}
            match = candidates.get(space.space_id)
            if not frame.vehicle_detection_enabled or not frame.vehicle_inference_done:
                # A vehicle may still be parked during the outage; later absence must
                # meet the lost-occupant rule instead of the short empty rule.
                memory.lost = memory.lost or memory.state == "occupied"
                memory.absent_observations, memory.departed = 0, False
                memory.state = "unknown"
                memory.track_id = memory.origin = memory.previous = memory.stationary_since = None
                memory.absent_since = memory.uncertain_since = None
                reason = "detector_unavailable"
            elif match:
                _, vehicle, center, selected = match
                memory.absent_since = memory.uncertain_since = None
                memory.absent_observations = 0
                moved = (memory.previous is not None and dt > 0
                         and math.dist(center, memory.previous) / dt > policy.max_speed)
                displaced = memory.origin is not None and math.dist(center, memory.origin) > policy.max_displacement
                if memory.track_id != vehicle.track_id or moved or displaced:
                    if memory.state == "occupied":
                        # The occupant itself moving starts a departure; a different
                        # track (ID switch or passer-by) does not prove one.
                        if memory.track_id == vehicle.track_id:
                            memory.departed = True
                        else:
                            memory.lost = True
                    memory.track_id, memory.origin, memory.stationary_since = vehicle.track_id, center, now
                memory.previous = center
                duration = (now - memory.stationary_since).total_seconds()
                evidence.update(selected=selected, stationary_seconds=duration)
                if duration >= policy.stationary_seconds:
                    memory.state, reason = "occupied", "stationary_vehicle"
                    memory.lost = memory.departed = False
                else:
                    reason = "vehicle_moving_or_confirming"
                    # Passing/changing tracks cannot indefinitely renew occupancy.
                    if memory.state == "occupied":
                        memory.state = "unknown"
            elif space.space_id in uncertain:
                memory.track_id = memory.origin = memory.previous = memory.stationary_since = None
                memory.absent_since = None
                memory.absent_observations = 0
                memory.uncertain_since = memory.uncertain_since or now
                reason = "ambiguous_or_low_confidence"
                if memory.state != "occupied" or (now - memory.uncertain_since).total_seconds() >= policy.uncertain_seconds:
                    memory.state = "unknown"
            else:
                # No detection in the space is not proof of vacancy: the detector may
                # miss a parked car. Require a validated camera plus sustained absence,
                # and a longer absence if the occupant vanished without leaving.
                if memory.state == "occupied" and memory.track_id in present:
                    memory.departed = True  # Occupant tracked outside this space.
                if memory.state != "occupied":
                    memory.track_id = memory.origin = memory.previous = memory.stationary_since = None
                memory.uncertain_since = None
                memory.absent_since = memory.absent_since or now
                memory.absent_observations += 1
                elapsed = (now - memory.absent_since).total_seconds()
                evidence["absent_seconds"] = elapsed
                enough = memory.absent_observations >= policy.min_empty_observations
                if memory.state == "occupied" and not memory.departed:
                    reason = "vacancy_grace"
                    if elapsed >= policy.vacancy_seconds:
                        memory.state, memory.lost, reason = "unknown", True, "occupant_lost"
                elif not validated:
                    memory.state, reason = "unknown", "absence_unverified_camera"
                elif memory.lost:
                    reason = "occupant_lost"
                    if elapsed >= policy.lost_empty_seconds and enough:
                        memory.state, reason = "empty", "prolonged_absence"
                elif memory.state == "empty":
                    reason = "confirmed_absence"
                elif elapsed >= policy.empty_seconds and enough:
                    memory.state = "empty"
                    reason = "departure_observed" if memory.departed else "confirmed_absence"
                elif memory.state == "occupied":
                    reason = "departure_confirming"
                if memory.state == "empty":
                    memory.lost = memory.departed = False
            evidence.update(camera_validated=validated, absent_observations=memory.absent_observations,
                            occupant_lost=memory.lost, departure_observed=memory.departed)
            memory.last_timestamp, memory.frame_number = now, frame.frame_number
            decisions.append(Decision(space.space_id, space.revision, memory.state, reason, now, evidence))
        if decisions:
            self.diagnostics[frame.camera_id] = decisions
        return decisions
