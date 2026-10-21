"""autolabel.py pure helpers (class mapping, box conversion); Ultralytics/GPU are not needed."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import autolabel  # noqa: E402

COCO = {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck", 8: "boat"}


class ClassMappingTests(unittest.TestCase):
    def test_coco_heads_merge_car_bus_truck(self):
        self.assertEqual(autolabel.target_classes(COCO), [2, 5, 7])
        self.assertEqual(autolabel.target_classes({str(k): v for k, v in COCO.items()}), [2, 5, 7])
        self.assertEqual(autolabel.target_classes({2: "Car", 7: "TRUCK"}), [2, 7])

    def test_single_class_model_keeps_class_zero(self):
        self.assertEqual(autolabel.target_classes({0: "item"}), [0])
        self.assertEqual(autolabel.target_classes({0: "car"}), [0])

    def test_model_without_car_is_refused(self):
        with self.assertRaises(SystemExit):
            autolabel.target_classes({0: "person", 1: "dog"})


class BoxConversionTests(unittest.TestCase):
    def test_xyxy_to_top_left_size_sorted_and_capped(self):
        xyxy = [[10, 20, 50, 60], [5, 5, 5, 30], [100.123, 50.456, 180.789, 90.111]]
        boxes = autolabel.boxes_from(xyxy, [0.4, 0.9, 0.85])
        self.assertEqual(boxes, [{"x": 100.12, "y": 50.46, "width": 80.67, "height": 39.66, "score": 0.85},
                                 {"x": 10.0, "y": 20.0, "width": 40.0, "height": 40.0, "score": 0.4}])
        many = autolabel.boxes_from([[i, 0, i + 5, 5] for i in range(400)], [i / 400 for i in range(400)], limit=300)
        self.assertEqual(len(many), 300)
        self.assertEqual(many[0]["score"], 0.9975)

    def test_image_list_skips_blank_lines(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            listing = Path(tmp) / "images.txt"
            listing.write_text("/a/1.jpg\n\n /a/2.jpg \n")
            self.assertEqual([p.name for p in autolabel.read_image_list(listing)], ["1.jpg", "2.jpg"])


if __name__ == "__main__":
    unittest.main()
