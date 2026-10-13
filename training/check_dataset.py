"""Validate a ReTrace YOLO dataset ZIP/directory before training.

Mirrors the export contract in backend/app/dataset.py and docs/datasets.md:
data.yaml + manifest.json + images|labels/{train,val,test}, classes 0 car / 1 truck / 2 bus,
capture sessions chained per camera when consecutive captures are < 30 minutes apart.

Never modifies the ZIP or labels. A ZIP is extracted once into training/datasets/<name>/.

Exit codes: 0 usable, 1 errors (do not train), 2 usable for a pipeline check only
(warnings such as an empty/small val or test split; use --strict to fail on them).
"""
import argparse
import hashlib
import json
import math
import shutil
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import yaml
from PIL import Image

ROOT = Path(__file__).resolve().parent
SPLITS = ("train", "val", "test")
CLASSES = {0: "car", 1: "truck", 2: "bus"}
SESSION_GAP_SECONDS = 1800  # backend/app/dataset.py capture(): same session if gap < 30 min
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
MIN_EVAL_IMAGES = 10  # same threshold the exporter warns on
NEAR_DUP_BITS = 4  # dHash Hamming distance treated as near-identical frame
SIZE_BUCKETS = ((32 ** 2, "small"), (96 ** 2, "medium"), (math.inf, "large"))  # COCO, source pixels


def extract_zip(zip_path, out_root):
    """Extract into out_root/<zip stem>; reuse an identical earlier extraction."""
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    target = out_root / zip_path.stem
    stamp = target / ".source.json"
    if target.exists():
        if stamp.is_file() and json.loads(stamp.read_text()).get("sha256") == digest:
            return find_dataset_root(target)
        raise SystemExit(f"{target} exists and is not an extraction of this ZIP; choose another name")
    tmp = target.with_name(target.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            dest = (tmp / member.filename).resolve()
            if not dest.is_relative_to(tmp.resolve()):
                raise SystemExit(f"Unsafe path in ZIP: {member.filename!r}")
        archive.extractall(tmp)
    (tmp / ".source.json").write_text(json.dumps({"zip": zip_path.name, "sha256": digest}))
    tmp.rename(target)
    return find_dataset_root(target)


def find_dataset_root(path):
    """Exporter writes data.yaml at the archive root; tolerate one wrapping folder (dataset/)."""
    if (path / "data.yaml").is_file():
        return path
    children = [p for p in path.iterdir() if p.is_dir() and (p / "data.yaml").is_file()]
    if len(children) == 1:
        return children[0]
    raise SystemExit(f"No data.yaml found in {path}")


def dhash(path):
    with Image.open(path) as im:
        small = im.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
    px = small.tobytes()
    return sum(1 << i for i in range(64) if px[(i // 8) * 9 + i % 8] > px[(i // 8) * 9 + i % 8 + 1])


def parse_time(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def check(root):
    errors, warnings, info = [], [], {}
    data_yaml = yaml.safe_load((root / "data.yaml").read_text())
    names = data_yaml.get("names")
    names = dict(enumerate(names)) if isinstance(names, list) else {int(k): v for k, v in (names or {}).items()}
    if names != CLASSES:
        errors.append(f"data.yaml names {names} != expected {CLASSES}")
    if "path" in data_yaml:
        warnings.append("data.yaml has 'path'; ReTrace exports resolve splits relative to data.yaml")
    for split in SPLITS:
        if data_yaml.get(split) != f"images/{split}":
            warnings.append(f"data.yaml {split}={data_yaml.get(split)!r} (expected images/{split})")
    nc = len(names) if names else len(CLASSES)

    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else None
    if manifest is None:
        errors.append("manifest.json missing: cannot verify review status, sessions or hashes")
    by_id = {str(m["id"]): m for m in (manifest or {}).get("images", [])}

    images, label_counts, size_counts = {}, {s: Counter() for s in SPLITS}, {s: Counter() for s in SPLITS}
    empty, boxes_per_image = Counter(), {}
    for split in SPLITS:
        img_dir, lbl_dir = root / "images" / split, root / "labels" / split
        imgs = {p.stem: p for p in img_dir.glob("*") if p.suffix.lower() in IMAGE_SUFFIXES} if img_dir.is_dir() else {}
        lbls = {p.stem: p for p in lbl_dir.glob("*.txt")} if lbl_dir.is_dir() else {}
        for stem in sorted(imgs.keys() - lbls.keys()):
            errors.append(f"{split}/{stem}: image without label TXT (exports always write one, even empty)")
        for stem in sorted(lbls.keys() - imgs.keys()):
            errors.append(f"{split}/{stem}: label TXT without image")
        for stem in sorted(imgs.keys() & lbls.keys()):
            if stem in images:
                errors.append(f"{stem}: present in both {images[stem]['split']} and {split}")
                continue
            path = imgs[stem]
            try:
                with Image.open(path) as im:
                    im.verify()
                with Image.open(path) as im:
                    width, height = im.size
            except Exception as exc:  # noqa: BLE001 - report any decode failure
                errors.append(f"{split}/{path.name}: unreadable image ({exc.__class__.__name__})")
                continue
            lines = [ln for ln in lbls[stem].read_text().splitlines() if ln.strip()]
            seen = set()
            for n, line in enumerate(lines, 1):
                where = f"{split}/{stem}.txt:{n}"
                parts = line.split()
                if len(parts) != 5:
                    errors.append(f"{where}: expected 5 fields, got {len(parts)}")
                    continue
                try:
                    cls = int(parts[0])
                    xc, yc, w, h = map(float, parts[1:])
                except ValueError:
                    errors.append(f"{where}: non-numeric value")
                    continue
                if not 0 <= cls < nc:
                    errors.append(f"{where}: class {cls} outside 0..{nc - 1}")
                    continue
                if not all(map(math.isfinite, (xc, yc, w, h))) or w <= 0 or h <= 0:
                    errors.append(f"{where}: non-finite or non-positive box")
                    continue
                eps = 1e-6
                if xc - w / 2 < -eps or yc - h / 2 < -eps or xc + w / 2 > 1 + eps or yc + h / 2 > 1 + eps:
                    errors.append(f"{where}: box outside the image")
                    continue
                if line in seen:
                    warnings.append(f"{where}: duplicate box line")
                seen.add(line)
                if w * width < 2 or h * height < 2:
                    warnings.append(f"{where}: box smaller than 2px")
                label_counts[split][CLASSES.get(cls, str(cls))] += 1
                area = w * width * h * height
                size_counts[split][next(name for limit, name in SIZE_BUCKETS if area < limit)] += 1
            boxes_per_image[stem] = len(lines)
            if not lines:
                empty[split] += 1
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            images[stem] = {"split": split, "path": path, "sha256": sha, "boxes": len(lines), "size": (width, height)}
            entry = by_id.get(stem)
            if manifest is not None:
                if entry is None:
                    errors.append(f"{split}/{stem}: not listed in manifest.json")
                else:
                    if entry.get("split") != split:
                        errors.append(f"{stem}: manifest split {entry.get('split')} != folder {split}")
                    if entry.get("sha256") and entry["sha256"] != sha:
                        errors.append(f"{stem}: sha256 differs from manifest (image changed after export)")
                    if entry.get("box_count") is not None and entry["box_count"] != len(lines):
                        errors.append(f"{stem}: manifest box_count {entry['box_count']} != {len(lines)} label lines")
    for stem in sorted(by_id.keys() - images.keys()):
        errors.append(f"manifest image {stem} missing from images/labels")

    counts = Counter(v["split"] for v in images.values())
    info["images"] = {s: counts[s] for s in SPLITS} | {"total": len(images)}
    info["empty_reviewed_images"] = {s: empty[s] for s in SPLITS}
    info["labels_per_class"] = {s: dict(label_counts[s]) for s in SPLITS}
    info["labels_per_class_total"] = dict(sum(label_counts.values(), Counter()))
    info["box_size_buckets_px"] = {s: dict(size_counts[s]) for s in SPLITS}
    if manifest and manifest.get("counts") and any(manifest["counts"].get(s) != counts[s] for s in SPLITS):
        errors.append(f"manifest counts {manifest['counts']} != files {dict(counts)}")

    # Empty images: exporter only includes reviewed images and refuses empty ones unless reviewed_empty.
    if images and sum(empty.values()) == len(images):
        errors.append("every image is empty (no vehicle boxes); nothing to learn")
    elif images and sum(empty.values()) / len(images) > 0.5:
        warnings.append(f"{sum(empty.values())}/{len(images)} images have no boxes; confirm they are truly vehicle-free")

    # Exact duplicates.
    by_sha = defaultdict(list)
    for stem, v in images.items():
        by_sha[v["sha256"]].append(stem)
    for sha, stems in by_sha.items():
        if len(stems) > 1:
            splits = {images[s]["split"] for s in stems}
            (errors if len(splits) > 1 else warnings).append(
                f"identical image bytes {stems} in splits {sorted(splits)}")

    # Sessions: each session must sit in exactly one split, and re-derived chains must agree.
    sessions = defaultdict(set)
    per_camera = defaultdict(list)
    for stem, entry in by_id.items():
        if stem not in images:
            continue
        sessions[entry.get("session")].add(images[stem]["split"])
        t = parse_time(entry.get("captured_at"))
        if t is None or t.tzinfo is None:
            warnings.append(f"{stem}: missing/naive captured_at; session gap cannot be checked")
        else:
            per_camera[entry.get("camera_id")].append((t, stem))
    for session, splits in sessions.items():
        if len(splits) > 1:
            errors.append(f"session {session} spans splits {sorted(splits)} (leakage)")
    leaks = 0
    for camera, items in per_camera.items():
        items.sort()
        for (t0, a), (t1, b) in zip(items, items[1:]):
            if (t1 - t0).total_seconds() < SESSION_GAP_SECONDS and images[a]["split"] != images[b]["split"]:
                leaks += 1
                if leaks <= 20:
                    errors.append(f"camera {camera}: {a} ({images[a]['split']}) and {b} ({images[b]['split']}) "
                                  f"captured {(t1 - t0).total_seconds():.0f}s apart in different splits")
    if leaks > 20:
        errors.append(f"... {leaks - 20} more < 30 min cross-split pairs")
    split_sessions = {s: sorted({by_id[k].get('session') for k in by_id if k in images and images[k]['split'] == s})
                      for s in SPLITS}
    info["sessions"] = {s: len(v) for s, v in split_sessions.items()}
    info["cameras"] = {s: dict(Counter(by_id[k].get("camera_id") for k in by_id
                                       if k in images and images[k]["split"] == s)) for s in SPLITS}

    # Near-duplicate frames across train and evaluation splits (fixed CCTV makes some unavoidable;
    # many hits mean val/test scores will be optimistic).
    hashes = {stem: dhash(v["path"]) for stem, v in images.items()}
    near = []
    train = [s for s in images if images[s]["split"] == "train"]
    for stem in (s for s in images if images[s]["split"] != "train"):
        best = min(((bin(hashes[stem] ^ hashes[t]).count("1"), t) for t in train), default=None)
        if best and best[0] <= NEAR_DUP_BITS:
            near.append((stem, images[stem]["split"], best[1], best[0]))
    info["near_duplicates_eval_vs_train"] = len(near)
    if near:
        warnings.append(f"{len(near)} val/test images are near-identical (dHash <= {NEAR_DUP_BITS} bits) to a "
                        f"train image, e.g. {near[0][0]} ~ {near[0][2]}; evaluation may be optimistic")

    # Sufficiency: report, never synthesize evaluation data.
    for split in ("val", "test"):
        if counts[split] == 0:
            warnings.append(f"{split}: no images (no independent session); metrics on it are impossible")
        elif counts[split] < MIN_EVAL_IMAGES:
            warnings.append(f"{split}: only {counts[split]} images; evaluation is not reliable")
    if counts["train"] == 0:
        errors.append("train: no images")
    for name in CLASSES.values():
        if info["labels_per_class_total"].get(name, 0) == 0:
            info.setdefault("absent_classes", []).append(name)
    if manifest:
        info["manifest_warnings"] = manifest.get("warnings", [])
        info["seed"] = manifest.get("seed")
    return {"root": str(root), "errors": errors, "warnings": warnings, "info": info}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dataset", type=Path, help="ReTrace export ZIP or extracted directory")
    parser.add_argument("--extract-to", type=Path, default=ROOT / "datasets")
    parser.add_argument("--report", type=Path, help="write the JSON report here (default: <dataset>/check_report.json)")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failure")
    args = parser.parse_args(argv)
    src = args.dataset.resolve()
    if src.is_file() and zipfile.is_zipfile(src):
        root = extract_zip(src, args.extract_to.resolve())
    elif src.is_dir():
        root = find_dataset_root(src)
    else:
        raise SystemExit(f"{src} is not a ZIP or directory")
    report = check(root)
    out = args.report or root / "check_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    print(f"Dataset: {root}")
    for key, value in report["info"].items():
        print(f"  {key}: {value}")
    for label, items in (("ERROR", report["errors"]), ("WARN", report["warnings"])):
        for item in items[:50]:
            print(f"{label}: {item}")
        if len(items) > 50:
            print(f"{label}: ... {len(items) - 50} more (see {out})")
    print(f"Report: {out}")
    if report["errors"]:
        return 1
    return 1 if args.strict and report["warnings"] else (2 if report["warnings"] else 0)


if __name__ == "__main__":
    sys.exit(main())
