"""CPU tests for the dataset checker and derived views (synthetic images only, no CCTV data)."""
import hashlib
import json
import random
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_dataset  # noqa: E402
import dataset_views  # noqa: E402

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def write_dataset(root, items):
    """items: (id, split, camera, minutes, session, label_lines)."""
    (root / "data.yaml").write_text("train: images/train\nval: images/val\ntest: images/test\n"
                                    "names:\n  0: car\n  1: truck\n  2: bus\n")
    manifest = {"seed": 42, "images": [], "warnings": [], "counts": {"train": 0, "val": 0, "test": 0}}
    for n, (image_id, split, camera, minutes, session, lines) in enumerate(items):
        for kind in ("images", "labels"):
            (root / kind / split).mkdir(parents=True, exist_ok=True)
        rng = random.Random(n)
        path = root / "images" / split / f"{image_id}.jpg"
        Image.frombytes("L", (64, 48), bytes(rng.randrange(256) for _ in range(64 * 48))).convert("RGB").save(path)
        (root / "labels" / split / f"{image_id}.txt").write_text("".join(f"{line}\n" for line in lines))
        manifest["images"].append({"id": image_id, "camera_id": camera, "session": session, "split": split,
                                   "captured_at": (T0 + timedelta(minutes=minutes)).isoformat(),
                                   "box_count": len(lines), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        manifest["counts"][split] += 1
    (root / "manifest.json").write_text(json.dumps(manifest))


GOOD = [("a", "train", "c1", 0, "s1", ["0 0.5 0.5 0.2 0.2", "1 0.2 0.2 0.1 0.1"]),
        ("b", "train", "c1", 10, "s1", []),
        ("c", "val", "c1", 120, "s2", ["2 0.5 0.5 0.4 0.4"]),
        ("d", "test", "c1", 300, "s3", ["0 0.9 0.9 0.2 0.2"])]


class CheckDatasetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "ds"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_export_has_no_errors_and_counts(self):
        write_dataset(self.root, GOOD)
        report = check_dataset.check(self.root)
        self.assertEqual(report["errors"], [])
        info = report["info"]
        self.assertEqual(info["images"], {"train": 2, "val": 1, "test": 1, "total": 4})
        self.assertEqual(info["labels_per_class_total"], {"car": 2, "truck": 1, "bus": 1})
        self.assertEqual(info["empty_reviewed_images"]["train"], 1)
        self.assertTrue(any("val: only 1" in w for w in report["warnings"]))

    def test_bad_labels_are_errors(self):
        items = list(GOOD)
        items[0] = ("a", "train", "c1", 0, "s1", ["3 0.5 0.5 0.2 0.2", "0 0.95 0.5 0.2 0.2", "0 0.5 0.5"])
        write_dataset(self.root, items)
        (self.root / "labels" / "val" / "c.txt").unlink()
        errors = "\n".join(check_dataset.check(self.root)["errors"])
        self.assertIn("class 3 outside", errors)
        self.assertIn("box outside the image", errors)
        self.assertIn("expected 5 fields", errors)
        self.assertIn("val/c: image without label TXT", errors)

    def test_session_leak_and_changed_image(self):
        items = list(GOOD)
        items[2] = ("c", "val", "c1", 25, "s2", ["2 0.5 0.5 0.4 0.4"])  # 15 min after b, other split
        write_dataset(self.root, items)
        Image.new("RGB", (64, 48)).save(self.root / "images" / "test" / "d.jpg")
        errors = "\n".join(check_dataset.check(self.root)["errors"])
        self.assertIn("900s apart in different splits", errors)
        self.assertIn("d: sha256 differs", errors)

    def test_zip_extraction_reuses_and_accepts_wrapping_folder(self):
        write_dataset(self.root, GOOD)
        archive = Path(self.tmp.name) / "export.zip"
        with zipfile.ZipFile(archive, "w") as z:
            for f in self.root.rglob("*"):
                z.write(f, "dataset/" + str(f.relative_to(self.root)))
        out = Path(self.tmp.name) / "out"
        first = check_dataset.extract_zip(archive, out)
        self.assertEqual(first, check_dataset.extract_zip(archive, out))
        self.assertEqual(first.name, "dataset")
        self.assertEqual(check_dataset.check(first)["errors"], [])

    def test_coco80_view_remaps_without_touching_original(self):
        write_dataset(self.root, GOOD)
        names = {i: f"class{i}" for i in range(80)} | {2: "car", 5: "bus", 7: "truck"}
        view = dataset_views.coco80_view(self.root, names)
        self.assertEqual((view / "labels" / "train" / "a.txt").read_text(),
                         "2 0.5 0.5 0.2 0.2\n7 0.2 0.2 0.1 0.1\n")
        self.assertEqual((view / "labels" / "val" / "c.txt").read_text(), "5 0.5 0.5 0.4 0.4\n")
        self.assertEqual((view / "labels" / "train" / "b.txt").read_text(), "")
        self.assertEqual((self.root / "labels" / "train" / "a.txt").read_text(),
                         "0 0.5 0.5 0.2 0.2\n1 0.2 0.2 0.1 0.1\n")
        self.assertFalse((view / "images" / "train").is_symlink())
        self.assertEqual((view / "images" / "train" / "a.jpg").resolve(), (self.root / "images/train/a.jpg").resolve())


if __name__ == "__main__":
    unittest.main()
