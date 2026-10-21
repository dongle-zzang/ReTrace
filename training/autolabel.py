"""Draft vehicle boxes for dataset images with a YOLO11m model (auto labelling, GPU worker child).

Started by worker.py with a validated argument list; never a user command line. Reads image paths from
a file, runs Ultralytics prediction once per image and appends one JSON line per image to --out:
    {"image_id": "<uuid>", "boxes": [{"x", "y", "width", "height", "score"}, ...]}
Boxes are original-pixel top-left/size; the Backend clips them and stores them as source='auto'.

Class mapping (the dataset has the single class 0 = car): COCO-80 heads (base yolo11m, coco80/vehicle3
fine-tunes) keep COCO car 2, bus 5 and truck 7 and all three become dataset class 0, the same merge the
production tracker applies. A 1-class (single) fine-tune keeps its class 0.
"""
import argparse
import json
import sys
from pathlib import Path

VEHICLE_NAMES = ("car", "bus", "truck")
MAX_BOXES = 300


def target_classes(names):
    """Model class ids to predict, given Ultralytics `model.names` ({id: name})."""
    names = {int(k): str(v).lower() for k, v in dict(names).items()}
    if len(names) == 1:
        return [next(iter(names))]  # single-class vehicle fine-tune
    wanted = sorted(k for k, v in names.items() if v in VEHICLE_NAMES)
    if "car" not in {names[k] for k in wanted}:
        raise SystemExit(f"model has no 'car' class: {list(names.values())[:10]}")
    return wanted


def boxes_from(xyxy, scores, limit=MAX_BOXES):
    """[[x1,y1,x2,y2], ...] + scores -> top-left/size boxes, best scores first, at most `limit`."""
    boxes = []
    for (x1, y1, x2, y2), score in zip(xyxy, scores):
        if x2 - x1 <= 0 or y2 - y1 <= 0:
            continue
        boxes.append({"x": round(float(x1), 2), "y": round(float(y1), 2), "width": round(float(x2 - x1), 2),
                      "height": round(float(y2 - y1), 2), "score": round(float(score), 4)})
    boxes.sort(key=lambda box: -box["score"])
    return boxes[:limit]


def read_image_list(path):
    items = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(Path(line))
    return items


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--weights", required=True, type=Path)
    p.add_argument("--images", required=True, type=Path, help="text file: one image path per line; stem = image id")
    p.add_argument("--out", required=True, type=Path, help="JSON lines output (appended per image)")
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--conf", type=float, default=0.35)
    p.add_argument("--device", default="0")
    p.add_argument("--class-mode", default="coco80", choices=("coco80", "vehicle3", "single"))
    p.add_argument("--progress-file", type=Path)
    args = p.parse_args(argv)

    images = read_image_list(args.images)
    if not images:
        raise SystemExit("no images to label")
    from ultralytics import YOLO
    import torch
    if not torch.cuda.is_available():
        raise SystemExit("CUDA is not available; auto labelling never runs on CPU")
    model = YOLO(str(args.weights))
    classes = target_classes(model.names)
    if args.class_mode == "single" and len(classes) != 1:
        print(f"warning: class mode single but the model has {len(model.names)} classes; using vehicle names", flush=True)
    print(f"labelling {len(images)} images with classes {classes} at conf {args.conf}", flush=True)
    results = model.predict(source=[str(path) for path in images], imgsz=args.imgsz, conf=args.conf,
                            device=args.device, classes=classes, max_det=MAX_BOXES, stream=True, verbose=False)
    done = 0
    with open(args.out, "a", encoding="utf-8") as out:
        for path, result in zip(images, results):
            boxes = result.boxes
            drafts = boxes_from(boxes.xyxy.tolist(), boxes.conf.tolist()) if boxes is not None and len(boxes) else []
            out.write(json.dumps({"image_id": path.stem, "boxes": drafts}) + "\n")
            out.flush()
            done += 1
            if args.progress_file:
                args.progress_file.write_text(json.dumps({"done": done, "total": len(images), "boxes": len(drafts)}))
            if done % 25 == 0 or done == len(images):
                print(f"{done}/{len(images)} images", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
