#!/usr/bin/env python3
"""Evaluate labelled metadata sidecars from real videos without GPU access.

Each JSONL line: {frame: FrameIn, runtime_session: str, spaces: [{space_id,
camera_id,revision,polygon}], expected: {space_id: occupancy} (optional),
vehicle_labels: [{x,y,width,height}] (optional normalized ground-truth boxes)}.
No raw video, RTSP or service addresses are needed. Output is aggregate metrics.
"""
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.occupancy import OccupancyEvaluator, ParkingPolicy
from backend.app.schemas import FrameIn, ParkingSpaceCreate


def iou(a, b):
    width = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    height = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    overlap = width * height
    union = a[2] * a[3] + b[2] * b[3] - overlap
    return overlap / union if union > 0 else 0


def replay(path, policy=None):
    evaluator = OccupancyEvaluator(policy)
    confusion, frames, correct, judged = {}, 0, 0, 0
    tp = fp = fn = labelled_frames = 0
    with Path(path).open() as source:
        for line in source:
            if not line.strip():
                continue
            item = json.loads(line)
            frame = FrameIn.model_validate(item["frame"])
            spaces = []
            for space in item["spaces"]:
                validated = ParkingSpaceCreate(name="validation", polygon=space["polygon"])
                if space["camera_id"] != frame.camera_id:
                    raise ValueError("Replay camera mismatch")
                spaces.append(SimpleNamespace(**{**space, "polygon": [p.model_dump() for p in validated.polygon]}))
            decisions = evaluator.observe(frame, item["runtime_session"], spaces)
            for decision in decisions:
                expected = item.get("expected", {}).get(decision.space_id)
                if expected is not None:
                    if expected not in ("occupied", "empty", "unknown"):
                        raise ValueError("Invalid replay label")
                    judged += 1
                    correct += decision.occupancy == expected
                    key = f"{expected}->{decision.occupancy}"
                    confusion[key] = confusion.get(key, 0) + 1
            if "vehicle_labels" in item:
                labels = [tuple(box[k] for k in ("x", "y", "width", "height")) for box in item["vehicle_labels"]]
                detections = [(v.bbox.x / frame.bbox_width, v.bbox.y / frame.bbox_height,
                               v.bbox.width / frame.bbox_width, v.bbox.height / frame.bbox_height)
                              for v in frame.vehicles if v.confidence is not None and v.confidence >= evaluator.policy.min_confidence]
                candidates = sorted(((iou(a, b), i, j) for i, a in enumerate(detections)
                                     for j, b in enumerate(labels)), reverse=True)
                matched_detections, matched_labels = set(), set()
                for score, i, j in candidates:
                    if score >= .5 and i not in matched_detections and j not in matched_labels:
                        matched_detections.add(i)
                        matched_labels.add(j)
                tp += len(matched_labels)
                fp += len(detections) - len(matched_detections)
                fn += len(labels) - len(matched_labels)
                labelled_frames += 1
            frames += 1
    return {"frames": frames, "occupancy_labels": judged,
            "occupancy_accuracy": correct / judged if judged else None,
            "occupancy_confusion": confusion, "vehicle_labelled_frames": labelled_frames,
            "vehicle_iou_threshold": .5, "vehicle_tp": tp, "vehicle_fp": fp, "vehicle_fn": fn,
            "vehicle_precision": tp / (tp + fp) if tp + fp else None,
            "vehicle_recall": tp / (tp + fn) if tp + fn else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("--policy", type=Path, help="JSON object with ParkingPolicy fields")
    args = parser.parse_args()
    try:
        policy = ParkingPolicy(**json.loads(args.policy.read_text())) if args.policy else None
        print(json.dumps(replay(args.metadata, policy), allow_nan=False))
    except Exception:
        print("Replay input/config invalid; details hidden", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
