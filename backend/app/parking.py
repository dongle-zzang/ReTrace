"""Occupancy write boundary for a future Backend ingest adapter (no GPU imports)."""
from datetime import datetime, timezone

from sqlalchemy import select, text

from .db import ParkingEvent, ParkingSpace


def record_transition(session, space, previous, reason, observed_at, evidence=None):
    if previous != space.occupancy:
        if session.get_bind().dialect.name == "postgresql":
            # Serialize transition inserts until commit: sequence IDs alone can
            # commit out of order, causing an after=cursor reader to skip one.
            session.execute(text("SELECT pg_advisory_xact_lock(724905321)"))
        session.add(ParkingEvent(camera_id=space.camera_id, space_id=space.space_id,
                                 revision=space.revision, previous_occupancy=previous,
                                 occupancy=space.occupancy, reason=reason,
                                 observed_at=observed_at, evidence=evidence or {}))


def set_decision(session, space, occupancy, reason, observed_at, evidence=None):
    previous = space.occupancy
    space.occupancy, space.occupancy_observed_at = occupancy, observed_at
    space.updated_at = datetime.now(timezone.utc)
    record_transition(session, space, previous, reason, observed_at, evidence)


def event_out(event):
    at = event.observed_at
    return {"event_id": str(event.event_id), "camera_id": event.camera_id, "space_id": event.space_id,
            "revision": event.revision, "previous_occupancy": event.previous_occupancy,
            "occupancy": event.occupancy, "reason": event.reason,
            "observed_at": (at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at).isoformat(),
            "evidence": event.evidence}


def update_occupancy(session, camera_id, space_id, revision, occupancy, observed_at):
    """Caller owns the transaction; return False for missing/stale observations.

    Geometry revision guards delayed detections after a polygon edit. Timestamp
    ordering rejects duplicate/out-of-order results. No public CRUD request may
    assign occupancy; a future ingest adapter must validate camera/runtime
    identity and convert missing/unusable observations to unknown before calling.
    """
    if occupancy not in {"occupied", "empty", "unknown"}:
        raise ValueError("Invalid occupancy")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("Observation timezone required")
    space = session.scalar(select(ParkingSpace).where(ParkingSpace.camera_id == camera_id,
                           ParkingSpace.space_id == space_id).with_for_update())
    if space is None or space.revision != revision:
        return False
    previous_at = space.occupancy_observed_at
    if previous_at is not None:
        previous_at = previous_at.replace(tzinfo=timezone.utc) if previous_at.tzinfo is None else previous_at
        if previous_at >= observed_at:
            return False
    set_decision(session, space, occupancy, "external_observation", observed_at)
    return True
