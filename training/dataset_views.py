"""Derived, read-only views of an extracted ReTrace dataset; the export itself is never modified."""
import json
import os
import shutil
from pathlib import Path

import yaml

from check_dataset import IMAGE_SUFFIXES, SPLITS


def coco_remap(root, model_names):
    """Dataset class id -> the base model's COCO id, matched by class NAME (dataset 0 car != COCO 0 person)."""
    names = {int(k): str(v) for k, v in model_names.items()}
    lookup = {v.lower(): k for k, v in names.items()}
    data_names = yaml.safe_load((Path(root) / "data.yaml").read_text())["names"]
    data_names = dict(enumerate(data_names)) if isinstance(data_names, list) else data_names
    # KeyError -> the model lacks the class.
    return names, {int(cid): lookup[str(name).lower()] for cid, name in data_names.items()}


def remap_label(src, dst, remap):
    lines = []
    for line in src.read_text().splitlines():
        if line.strip():
            cls, *rest = line.split()
            lines.append(" ".join([str(remap[int(cls)])] + rest))
    dst.write_text("\n".join(lines) + ("\n" if lines else ""))


def split_images(root, split):
    folder = root / "images" / split
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES) if folder.is_dir() else []


def link_or_copy(src, dst):
    """Hardlink on the same filesystem (no extra space, the copy cannot change the source), else copy."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def publish(tmp, view):
    shutil.rmtree(view, ignore_errors=True)  # always rebuilt from the current labels
    tmp.rename(view)
    return view


def coco80_view(root, model_names):
    """Same images (symlinked), labels remapped car/truck/bus -> the base model's COCO IDs.

    Fine-tuning keeps the pretrained 80-class head instead of re-initializing it, and the
    resulting ONNX keeps the production 80-label layout (labels.txt / filter-out-class-ids).
    """
    root = Path(root).resolve()
    names, remap = coco_remap(root, model_names)
    view = root.parent / f"{root.name}__coco80"
    tmp = view.with_name(view.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    for split in SPLITS:
        (tmp / "labels" / split).mkdir(parents=True)
        (tmp / "images" / split).mkdir(parents=True)
        # Per-file links: Ultralytics resolve()s split directories, so a linked directory would
        # lead it back to the original 3-class labels.
        for image in split_images(root, split):
            (tmp / "images" / split / image.name).symlink_to(image)
        for src in sorted((root / "labels" / split).glob("*.txt")):
            remap_label(src, tmp / "labels" / split / src.name, remap)
    (tmp / "data.yaml").write_text(yaml.safe_dump(
        {"train": "images/train", "val": "images/val", "test": "images/test", "names": names}, sort_keys=False))
    return publish(tmp, view)


def train_all_view(root, model_names):
    """Training copy for Train All: every train/val/test image becomes a training image.

    The export and its split assignment stay untouched; this is a separate directory with its
    own data.yaml. Labels are remapped to the base model's COCO IDs (pretrained 80-class head kept,
    car stays COCO 2). Ultralytics requires a `val` entry and validates once on the final epoch,
    so `val` points at the same training images: that number is training fit, never an
    independent score. Image ids are unique across splits (check_dataset refuses duplicates).
    """
    root = Path(root).resolve()
    names, remap = coco_remap(root, model_names)
    view = root.parent / f"{root.name}__trainall"
    tmp = view.with_name(view.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "images" / "train").mkdir(parents=True)
    (tmp / "labels" / "train").mkdir(parents=True)
    sources = {}
    for split in SPLITS:
        for image in split_images(root, split):
            link_or_copy(image, tmp / "images" / "train" / image.name)
            remap_label(root / "labels" / split / f"{image.stem}.txt", tmp / "labels" / "train" / f"{image.stem}.txt", remap)
            sources[image.stem] = split
    counts = {split: sum(1 for value in sources.values() if value == split) for split in SPLITS}
    (tmp / "data.yaml").write_text(yaml.safe_dump(
        {"train": "images/train", "val": "images/train", "names": names}, sort_keys=False))
    (tmp / "train_all.json").write_text(json.dumps(
        {"source": str(root), "counts": {**counts, "total": len(sources)}, "original_split": sources}, indent=2))
    return publish(tmp, view)


def subset_view(root, image_ids, name="neweval"):
    """Evaluation-only copy holding just `image_ids`, all in the test split (original class IDs).

    Used to score a model on newly captured CCTV images: the Backend already removed every image
    (and capture session) the model was trained on. manifest.json is rewritten for the subset so
    check_dataset still verifies hashes, box counts and sessions.
    """
    root = Path(root).resolve()
    wanted = set(image_ids)
    manifest = json.loads((root / "manifest.json").read_text())
    view = root.parent / f"{root.name}__{name}"
    tmp = view.with_name(view.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    for split in SPLITS:
        (tmp / "images" / split).mkdir(parents=True)
        (tmp / "labels" / split).mkdir(parents=True)
    found = set()
    for split in SPLITS:
        for image in split_images(root, split):
            if image.stem in wanted:
                link_or_copy(image, tmp / "images" / "test" / image.name)
                shutil.copyfile(root / "labels" / split / f"{image.stem}.txt", tmp / "labels" / "test" / f"{image.stem}.txt")
                found.add(image.stem)
    entries = [{**entry, "split": "test"} for entry in manifest.get("images", []) if str(entry.get("id")) in found]
    shutil.copyfile(root / "data.yaml", tmp / "data.yaml")
    (tmp / "manifest.json").write_text(json.dumps({
        **{k: v for k, v in manifest.items() if k not in ("images", "counts", "session_splits", "warnings")},
        "images": entries, "counts": {"train": 0, "val": 0, "test": len(found)}, "warnings": [],
        "subset_of": root.name}, indent=2))
    publish(tmp, view)
    return view, sorted(wanted - found)
