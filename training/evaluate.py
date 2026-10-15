"""Compare vehicle detectors on one held-out split of a ReTrace dataset.

Default comparison: stock COCO YOLO11m (classes mapped by name car/truck/bus) vs a fine-tuned
best.pt, same images, same preprocessing (Ultralytics letterbox, --imgsz), same NMS IoU.

Primary metrics are class-agnostic "vehicle" metrics because production merges car/truck/bus
into one tracker class (vehicle_detection.py). Operating-point numbers use the production
detector threshold (models/yolo11m/vehicle_infer.txt pre-cluster-threshold=0.25, NMS 0.45).
This is NOT a DeepStream measurement: nvinfer scaling, TensorRT FP16 and the RTSP path differ.

Outputs (under --out): report.md, metrics.json, per_image.csv, images/*.jpg (GT green,
TP blue, FP orange, missed GT red; one panel per model).
"""
import argparse
import csv
import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("YOLO_CONFIG_DIR", str(ROOT / ".ultralytics"))
(Path(os.environ["YOLO_CONFIG_DIR"]) / "Ultralytics").mkdir(parents=True, exist_ok=True)

import check_dataset  # noqa: E402
import dataset_views  # noqa: E402

IOUS = np.linspace(0.5, 0.95, 10)
CLASSES = check_dataset.CLASSES
NAME_TO_ID = {v: k for k, v in CLASSES.items()}


def load_gt(label_path, width, height):
    rows = [list(map(float, ln.split())) for ln in label_path.read_text().splitlines() if ln.strip()]
    if not rows:
        return np.zeros((0, 4)), np.zeros(0, dtype=int)
    a = np.array(rows)
    xc, yc, w, h = a[:, 1] * width, a[:, 2] * height, a[:, 3] * width, a[:, 4] * height
    return np.stack([xc - w / 2, yc - h / 2, xc + w / 2, yc + h / 2], 1), a[:, 0].astype(int)


def iou_matrix(a, b):
    if not len(a) or not len(b):
        return np.zeros((len(a), len(b)))
    lt = np.maximum(a[:, None, :2], b[None, :, :2])
    rb = np.minimum(a[:, None, 2:], b[None, :, 2:])
    inter = np.clip(rb - lt, 0, None).prod(2)
    area = lambda x: (x[:, 2] - x[:, 0]) * (x[:, 3] - x[:, 1])  # noqa: E731
    return inter / (area(a)[:, None] + area(b)[None, :] - inter + 1e-9)


def greedy_match(iou, threshold, same_class=None):
    """Predictions are sorted by confidence; each takes the best unmatched GT above threshold."""
    matched_gt, pred_to_gt = set(), {}
    for p in range(iou.shape[0]):
        best, best_iou = None, threshold
        for g in np.argsort(-iou[p]):
            if iou[p, g] < best_iou:
                break
            if g in matched_gt or (same_class is not None and not same_class[p, g]):
                continue
            best = g
            break
        if best is not None:
            matched_gt.add(best)
            pred_to_gt[p] = best
    return pred_to_gt


def class_map_for(model):
    names = {int(k): str(v).lower() for k, v in model.names.items()}
    if len(names) == 1:
        return {0: 0}, False  # single-class fine-tune: every prediction is a vehicle
    mapping = {k: NAME_TO_ID[v] for k, v in names.items() if v in NAME_TO_ID}
    if "car" not in {names[k] for k in mapping}:
        raise SystemExit(f"model has no 'car' class: {list(names.values())[:10]}")
    return mapping, True


def size_bucket(box):
    area = (box[2] - box[0]) * (box[3] - box[1])
    return next(name for limit, name in check_dataset.SIZE_BUCKETS if area < limit)


def evaluate_model(label, weights, items, args):
    from ultralytics import YOLO
    from ultralytics.utils.metrics import ap_per_class

    model = YOLO(weights)
    mapping, class_aware = class_map_for(model)
    tp_agn, tp_cls, confs, pcls, tcls_all = [], [], [], [], []
    op = {"tp": 0, "fp": 0, "fn": 0, "gt": 0, "fn_by_size": {}, "gt_by_size": {}, "fn_by_class": {}, "gt_by_class": {}}
    per_image = {}
    results = model.predict([str(i["image"]) for i in items], imgsz=args.imgsz, conf=0.001, iou=args.nms_iou,
                            max_det=300, classes=sorted(mapping), device=args.device, half=args.half,
                            stream=True, verbose=False)
    for item, res in zip(items, results):
        gt_boxes, gt_cls = item["gt"]
        b = res.boxes
        boxes, conf = b.xyxy.cpu().numpy(), b.conf.cpu().numpy()
        cls = np.array([mapping[int(c)] for c in b.cls.cpu().numpy()], dtype=int)
        order = np.argsort(-conf)
        boxes, conf, cls = boxes[order], conf[order], cls[order]
        iou = iou_matrix(boxes, gt_boxes)
        same = cls[:, None] == gt_cls[None, :] if len(boxes) and len(gt_boxes) else None
        tpa = np.zeros((len(boxes), len(IOUS)), bool)
        tpc = np.zeros((len(boxes), len(IOUS)), bool)
        for j, t in enumerate(IOUS):
            for p in greedy_match(iou, t):
                tpa[p, j] = True
            if class_aware:
                for p in greedy_match(iou, t, same):
                    tpc[p, j] = True
        tp_agn.append(tpa)
        tp_cls.append(tpc)
        confs.append(conf)
        pcls.append(cls)
        tcls_all.append(gt_cls)

        keep = conf >= args.conf
        matches = greedy_match(iou[keep], 0.5)
        matched = set(matches.values())
        fp_idx = [p for p in range(int(keep.sum())) if p not in matches]
        fn_idx = [g for g in range(len(gt_boxes)) if g not in matched]
        op["tp"] += len(matches)
        op["fp"] += len(fp_idx)
        op["fn"] += len(fn_idx)
        op["gt"] += len(gt_boxes)
        for g, box in enumerate(gt_boxes):
            s, c = size_bucket(box), CLASSES[int(gt_cls[g])]
            op["gt_by_size"][s] = op["gt_by_size"].get(s, 0) + 1
            op["gt_by_class"][c] = op["gt_by_class"].get(c, 0) + 1
            if g in fn_idx:
                op["fn_by_size"][s] = op["fn_by_size"].get(s, 0) + 1
                op["fn_by_class"][c] = op["fn_by_class"].get(c, 0) + 1
        kept = boxes[keep]
        per_image[item["id"]] = {"tp": len(matches), "fp": len(fp_idx), "fn": len(fn_idx),
                                 "pred_tp": kept[list(matches)].tolist() if matches else [],
                                 "pred_fp": kept[fp_idx].tolist() if fp_idx else [],
                                 "gt_fn": gt_boxes[fn_idx].tolist() if fn_idx else []}

    conf_all, tcls = np.concatenate(confs), np.concatenate(tcls_all)
    out = {"weights": str(weights), "class_aware": class_aware, "names": {int(k): v for k, v in model.names.items()},
           "operating_point": op}
    if len(tcls):
        r = ap_per_class(np.concatenate(tp_agn), conf_all, np.zeros(len(conf_all)), np.zeros(len(tcls)))
        p, rc, ap = r[2], r[3], r[5]
        out["vehicle"] = {"precision_best_f1": float(p.mean()), "recall_best_f1": float(rc.mean()),
                          "mAP50": float(ap[:, 0].mean()), "mAP50-95": float(ap.mean())}
        if class_aware:
            r = ap_per_class(np.concatenate(tp_cls), conf_all, np.concatenate(pcls), tcls)
            out["per_class"] = {CLASSES[int(c)]: {"precision": float(r[2][i]), "recall": float(r[3][i]),
                                                  "mAP50": float(r[5][i, 0]), "mAP50-95": float(r[5][i].mean())}
                                for i, c in enumerate(r[6])}
    op["precision"] = op["tp"] / max(op["tp"] + op["fp"], 1)
    op["recall"] = op["tp"] / max(op["gt"], 1)
    return out, per_image


def ultralytics_val(weights, root, split, args):
    from ultralytics import YOLO
    model = YOLO(weights)
    single = len(model.names) == 1
    data_yaml = root / "data.yaml"
    if not single and len(model.names) != len(CLASSES):  # coco80 fine-tune: score with remapped labels
        data_yaml = dataset_views.coco80_view(root, model.names) / "data.yaml"
    m = model.val(data=str(data_yaml), split=split, imgsz=args.imgsz, batch=args.batch, device=args.device,
                  single_cls=single, plots=True, project=str(args.out), name=f"ultralytics_val_{split}",
                  exist_ok=True, verbose=True)
    out = {"precision": float(m.box.mp), "recall": float(m.box.mr), "mAP50": float(m.box.map50),
           "mAP50-95": float(m.box.map), "single_cls": single}
    if not single:
        out["per_class"] = {m.names[c]: {"precision": float(m.box.p[i]), "recall": float(m.box.r[i]),
                                         "mAP50": float(m.box.ap50[i]), "mAP50-95": float(m.box.ap[i])}
                            for i, c in enumerate(m.box.ap_class_index)}
    return out


def draw(items, per_model, out_dir, limit):
    import cv2
    out_dir.mkdir(parents=True, exist_ok=True)
    labels = list(per_model)
    ranked = sorted(items, key=lambda i: -sum(per_model[m][i["id"]]["fn"] + per_model[m][i["id"]]["fp"]
                                             for m in labels))
    saved = 0
    for item in ranked[:limit]:
        if not any(per_model[m][item["id"]]["fn"] or per_model[m][item["id"]]["fp"] for m in labels):
            break
        base = cv2.imread(str(item["image"]))
        panels = []
        for m in labels:
            im = base.copy()
            r = per_model[m][item["id"]]
            for box in item["gt"][0]:
                cv2.rectangle(im, tuple(map(int, box[:2])), tuple(map(int, box[2:])), (0, 200, 0), 1)
            for key, color, width in (("pred_tp", (255, 120, 0), 2), ("pred_fp", (0, 140, 255), 2),
                                      ("gt_fn", (0, 0, 255), 3)):
                for box in r[key]:
                    cv2.rectangle(im, tuple(map(int, box[:2])), tuple(map(int, box[2:])), color, width)
            text, scale = f"{m}: TP {r['tp']} FP {r['fp']} FN {r['fn']}", max(im.shape[1] / 1600, 0.5)
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
            cv2.rectangle(im, (0, 0), (tw + 20, th + 20), (0, 0, 0), -1)
            cv2.putText(im, text, (10, th + 10), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 2)
            panels.append(im)
        cv2.imwrite(str(out_dir / f"{item['split']}_{item['id']}.jpg"), np.vstack(panels), [cv2.IMWRITE_JPEG_QUALITY, 85])
        saved += 1
    return saved


def fmt(v):
    return "-" if v is None else f"{v:.3f}" if isinstance(v, float) else str(v)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, required=True, help="extracted ReTrace dataset dir")
    p.add_argument("--split", default="test", choices=("train", "val", "test"))
    p.add_argument("--baseline", default=str(ROOT / "weights" / "yolo11m.pt"))
    p.add_argument("--model", action="append", default=[], metavar="LABEL=WEIGHTS",
                   help="model(s) to compare against the baseline, e.g. ft=runs/train/<run>/weights/best.pt")
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--conf", type=float, default=0.25, help="operating threshold (production pre-cluster-threshold)")
    p.add_argument("--nms-iou", type=float, default=0.45, help="NMS IoU (production nms-iou-threshold)")
    p.add_argument("--batch", type=int, default=4)
    p.add_argument("--device", default="0")
    p.add_argument("--half", action="store_true", default=True)
    p.add_argument("--max-images", type=int, default=40, help="comparison images to save")
    p.add_argument("--no-ultralytics-val", action="store_true")
    p.add_argument("--out", type=Path)
    p.add_argument("--predictions-json", type=Path,
                   help="also write per-image GT/TP/FP/missed boxes (pixel xyxy) per model for the web comparison")
    p.add_argument("--eval-only", action="store_true",
                   help="dataset holds only evaluation images (new CCTV captures); an empty train split is expected")
    args = p.parse_args(argv)

    root = check_dataset.find_dataset_root(args.data.resolve())
    report = check_dataset.check(root)
    if args.eval_only:
        report["errors"] = [e for e in report["errors"] if e != "train: no images"]
    if report["errors"]:
        print("\n".join(["Dataset errors; fix before evaluating:"] + report["errors"][:20]))
        return 1
    img_dir, lbl_dir = root / "images" / args.split, root / "labels" / args.split
    paths = sorted(q for q in img_dir.glob("*") if q.suffix.lower() in check_dataset.IMAGE_SUFFIXES)
    if not paths:
        print(f"{args.split} split has no images: no independent comparison is possible. Not evaluating.")
        return 1
    from PIL import Image
    items = []
    for q in paths:
        with Image.open(q) as im:
            w, h = im.size
        items.append({"id": q.stem, "split": args.split, "image": q, "size": (w, h),
                      "gt": load_gt(lbl_dir / f"{q.stem}.txt", w, h)})

    models = {"baseline": args.baseline}
    for spec in args.model:
        label, _, weights = spec.partition("=")
        models[label] = weights
    args.out = (args.out or ROOT / "runs" / "eval" / f"{root.name}_{args.split}_{args.imgsz}").resolve()
    args.out.mkdir(parents=True, exist_ok=True)

    metrics, per_model = {}, {}
    for label, weights in models.items():
        metrics[label], per_model[label] = evaluate_model(label, weights, items, args)
        if label != "baseline" and not args.no_ultralytics_val:
            metrics[label]["ultralytics_val"] = ultralytics_val(weights, root, args.split, args)

    with (args.out / "per_image.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["image", "gt"] + [f"{m}_{k}" for m in models for k in ("tp", "fp", "fn")])
        for item in items:
            writer.writerow([item["id"], len(item["gt"][0])] +
                            [per_model[m][item["id"]][k] for m in models for k in ("tp", "fp", "fn")])
    saved = draw(items, per_model, args.out / "images", args.max_images)
    if args.predictions_json:
        rounded = lambda boxes: [[round(float(v), 1) for v in box] for box in boxes]  # noqa: E731
        args.predictions_json.write_text(json.dumps({
            "split": args.split, "imgsz": args.imgsz, "conf": args.conf, "nms_iou": args.nms_iou,
            "images": [{"id": item["id"], "width": item["size"][0], "height": item["size"][1],
                        "gt": rounded(item["gt"][0].tolist()),
                        "models": {m: {"tp": r["tp"], "fp": r["fp"], "fn": r["fn"], "pred_tp": rounded(r["pred_tp"]),
                                       "pred_fp": rounded(r["pred_fp"]), "gt_fn": rounded(r["gt_fn"])}
                                   for m in models for r in [per_model[m][item["id"]]]}}
                       for item in items]}))
    split_warn = [w for w in report["warnings"] if args.split in w or "near-identical" in w]
    summary = {"dataset": str(root), "split": args.split, "images": len(items),
               "gt_boxes": int(sum(len(i["gt"][0]) for i in items)), "imgsz": args.imgsz, "conf": args.conf,
               "nms_iou": args.nms_iou, "dataset_warnings": split_warn, "models": metrics}
    (args.out / "metrics.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    lines = [f"# Vehicle detector comparison: `{root.name}` / {args.split}", "",
             f"- Images: {len(items)}, GT boxes: {summary['gt_boxes']}, imgsz {args.imgsz}, "
             f"operating conf {args.conf}, NMS IoU {args.nms_iou}",
             "- Offline Ultralytics inference; not a DeepStream/TensorRT measurement.", ""]
    if split_warn or len(items) < check_dataset.MIN_EVAL_IMAGES:
        lines += ["**Evaluation caveats**", ""] + [f"- {w}" for w in split_warn] + \
                 ([f"- only {len(items)} images: differences are not statistically meaningful"]
                  if len(items) < check_dataset.MIN_EVAL_IMAGES else []) + [""]
    lines += ["## Vehicle (class-agnostic)", "",
              "| model | mAP50 | mAP50-95 | P@conf | R@conf | TP | FP | FN (missed) | FN small/medium/large |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for label, m in metrics.items():
        v, op = m.get("vehicle", {}), m["operating_point"]
        sizes = " / ".join(f"{op['fn_by_size'].get(s, 0)} of {op['gt_by_size'].get(s, 0)}"
                           for s in ("small", "medium", "large"))
        lines.append(f"| {label} | {fmt(v.get('mAP50'))} | {fmt(v.get('mAP50-95'))} | {fmt(op['precision'])} | "
                     f"{fmt(op['recall'])} | {op['tp']} | {op['fp']} | {op['fn']} | {sizes} |")
    lines += ["", "## Missed GT by class at operating point (any-vehicle match)", "",
              "| model | " + " | ".join(CLASSES.values()) + " |", "| --- |" + " --- |" * len(CLASSES)]
    for label, m in metrics.items():
        op = m["operating_point"]
        lines.append(f"| {label} | " + " | ".join(f"{op['fn_by_class'].get(c, 0)} of {op['gt_by_class'].get(c, 0)}"
                                                    for c in CLASSES.values()) + " |")
    for label, m in metrics.items():
        if "per_class" in m:
            lines += ["", f"## {label}: class-aware", "", "| class | P | R | mAP50 | mAP50-95 |", "| --- | --- | --- | --- | --- |"]
            lines += [f"| {c} | {fmt(r['precision'])} | {fmt(r['recall'])} | {fmt(r['mAP50'])} | {fmt(r['mAP50-95'])} |"
                      for c, r in m["per_class"].items()]
        if "ultralytics_val" in m:
            u = m["ultralytics_val"]
            lines += ["", f"## {label}: Ultralytics val ({'single class' if u['single_cls'] else 'per class'})", "",
                      f"P {fmt(u['precision'])}, R {fmt(u['recall'])}, mAP50 {fmt(u['mAP50'])}, mAP50-95 {fmt(u['mAP50-95'])}"]
    lines += ["", f"Comparison images ({saved}): `images/` — green GT, blue TP, orange FP, red missed GT.", ""]
    (args.out / "report.md").write_text("\n".join(lines))
    print("\n".join(lines))
    print(f"Saved to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
