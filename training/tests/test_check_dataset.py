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

    def test_train_all_view_trains_on_every_split_and_keeps_the_export(self):
        write_dataset(self.root, GOOD)
        before = {f: f.read_bytes() for f in self.root.rglob("*") if f.is_file()}
        names = {i: f"class{i}" for i in range(80)} | {2: "car", 5: "bus", 7: "truck"}
        view = dataset_views.train_all_view(self.root, names)
        self.assertEqual(sorted(p.name for p in (view / "images" / "train").iterdir()),
                         ["a.jpg", "b.jpg", "c.jpg", "d.jpg"])
        self.assertFalse((view / "images" / "val").exists() or (view / "images" / "test").exists())
        self.assertEqual((view / "labels" / "train" / "d.txt").read_text(), "2 0.9 0.9 0.2 0.2\n")  # car -> COCO 2
        self.assertEqual((view / "labels" / "train" / "c.txt").read_text(), "5 0.5 0.5 0.4 0.4\n")
        data = check_dataset.yaml.safe_load((view / "data.yaml").read_text())
        self.assertEqual((data["train"], data["val"], len(data["names"])), ("images/train", "images/train", 80))
        info = json.loads((view / "train_all.json").read_text())
        self.assertEqual(info["counts"], {"train": 2, "val": 1, "test": 1, "total": 4})
        self.assertEqual(info["original_split"]["d"], "test")
        self.assertFalse((view / "images" / "train" / "a.jpg").is_symlink())  # a real copy (hardlink), not a link
        self.assertEqual({f: f.read_bytes() for f in self.root.rglob("*") if f.is_file()}, before)
        self.assertEqual(check_dataset.check(self.root)["info"]["images"]["test"], 1)  # splits unchanged

    def test_subset_view_holds_only_new_images_in_test(self):
        write_dataset(self.root, GOOD)
        view, missing = dataset_views.subset_view(self.root, ["c", "d", "zz"])
        self.assertEqual(missing, ["zz"])
        self.assertEqual(sorted(p.name for p in (view / "images" / "test").iterdir()), ["c.jpg", "d.jpg"])
        self.assertEqual(list((view / "images" / "train").iterdir()), [])
        self.assertEqual((view / "labels" / "test" / "c.txt").read_text(), "2 0.5 0.5 0.4 0.4\n")  # dataset ids
        report = check_dataset.check(view)
        self.assertEqual(report["errors"], ["train: no images"])  # evaluate.py --eval-only accepts exactly this
        self.assertEqual(report["info"]["images"], {"train": 0, "val": 0, "test": 2, "total": 2})
        # coco80_view (used by evaluate.py's Ultralytics val) works on the subset too.
        names = {i: f"class{i}" for i in range(80)} | {2: "car", 5: "bus", 7: "truck"}
        self.assertTrue((dataset_views.coco80_view(view, names) / "labels" / "test" / "d.txt").is_file())


if __name__ == "__main__":
    unittest.main()
