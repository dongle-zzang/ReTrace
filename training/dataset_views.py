"""Derived, read-only views of an extracted ReTrace dataset; the export itself is never modified."""
import shutil
from pathlib import Path

import yaml

from check_dataset import SPLITS


def coco80_view(root, model_names):
    """Same images (symlinked), labels remapped car/truck/bus -> the base model's COCO IDs.

    Fine-tuning keeps the pretrained 80-class head instead of re-initializing it, and the
    resulting ONNX keeps the production 80-label layout (labels.txt / filter-out-class-ids).
    """
    root = Path(root).resolve()
    names = {int(k): str(v) for k, v in model_names.items()}
    lookup = {v.lower(): k for k, v in names.items()}
    data_names = yaml.safe_load((root / "data.yaml").read_text())["names"]
    data_names = dict(enumerate(data_names)) if isinstance(data_names, list) else data_names
    # Mapped by class NAME (dataset 0 car != COCO 0 person); KeyError -> model lacks the class.
    remap = {int(cid): lookup[str(name).lower()] for cid, name in data_names.items()}
    view = root.parent / f"{root.name}__coco80"
    tmp = view.with_name(view.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    for split in SPLITS:
        (tmp / "labels" / split).mkdir(parents=True)
        (tmp / "images" / split).mkdir(parents=True)
        # Per-file links: Ultralytics resolve()s split directories, so a linked directory would
        # lead it back to the original 3-class labels.
        for image in sorted((root / "images" / split).iterdir()):
            (tmp / "images" / split / image.name).symlink_to(image)
        for src in sorted((root / "labels" / split).glob("*.txt")):
            lines = []
            for line in src.read_text().splitlines():
                if line.strip():
                    cls, *rest = line.split()
                    lines.append(" ".join([str(remap[int(cls)])] + rest))
            (tmp / "labels" / split / src.name).write_text("\n".join(lines) + ("\n" if lines else ""))
    (tmp / "data.yaml").write_text(yaml.safe_dump(
        {"train": "images/train", "val": "images/val", "test": "images/test", "names": names}, sort_keys=False))
    shutil.rmtree(view, ignore_errors=True)  # always rebuilt from the current labels
    tmp.rename(view)
    return view
