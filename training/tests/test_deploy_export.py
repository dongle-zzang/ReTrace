"""Deployment export checks without Ultralytics/GPU: class maps, architecture, ONNX class remap.

Needs onnx + numpy (training venv):  cd training && .venv/bin/python -m unittest discover -s tests
"""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest

TRAINING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TRAINING))
sys.path.insert(0, str(TRAINING.parent))
import deploy_export  # noqa: E402

try:
    import numpy as np
    import onnx
    from onnx import TensorProto, helper
    HAVE_ONNX = True
except ImportError:
    HAVE_ONNX = False

COCO = ['person', 'bicycle', 'car'] + [f'class{i}' for i in range(3, 80)]


def fake_model(head_classes, task='detect', extra=None):
    shapes = {'model.0.conv.weight': (64, 3, 3, 3), 'model.23.cv2.0.2.weight': (64, 64, 1, 1),
              'model.23.cv3.0.2.weight': (head_classes, 256, 1, 1), 'model.23.cv3.0.2.bias': (head_classes,),
              **(extra or {})}
    tensors = {k: NS(shape=v) for k, v in shapes.items()}
    return NS(task=task, model=NS(state_dict=lambda: tensors))


class ClassMapTests(unittest.TestCase):
    def test_coco80_is_identity_and_single_maps_to_runtime_car(self):
        self.assertEqual(deploy_export.class_map(COCO, COCO, 'vehicle3'), {})
        self.assertEqual(deploy_export.class_map(['item'], COCO, 'single'), {0: 2})
        self.assertEqual(deploy_export.class_map(['car'], COCO, 'single'), {0: 2})
        for names, mode in ((['item'], 'vehicle3'), (COCO, 'single'), (['truck'], 'single'),
                            (['car', 'truck'], 'single'), (COCO[::-1], 'vehicle3'), (['car'], 'multi')):
            with self.assertRaises(ValueError):
                deploy_export.class_map(names, COCO, mode)
        with self.assertRaises(ValueError):  # running detector without a unique car label
            deploy_export.class_map(['car'], ['person', 'bus'], 'single')

    def test_architecture_allows_only_the_class_head_to_shrink(self):
        baseline = fake_model(80)
        deploy_export.check_architecture(fake_model(80), baseline, 80)
        deploy_export.check_architecture(fake_model(1), baseline, 1)
        for candidate, classes in ((fake_model(3), 1), (fake_model(1, task='segment'), 1),
                                   (fake_model(1, extra={'model.24.weight': (1,)}), 1),
                                   (fake_model(1, extra={'model.0.conv.weight': (32, 3, 3, 3)}), 1)):
            with self.assertRaises(ValueError):
                deploy_export.check_architecture(candidate, baseline, classes)


@unittest.skipUnless(HAVE_ONNX, 'needs onnx/numpy (training venv)')
class RemapTests(unittest.TestCase):
    def model(self, path, columns=6):
        """Stand-in for the DeepStream-Yolo export: dynamic batch, decoded [batch, 8400, 6] output."""
        graph = helper.make_graph(
            [helper.make_node('Identity', ['input'], ['output'])], 'yolo',
            [helper.make_tensor_value_info('input', TensorProto.FLOAT, ['batch', 8400, columns])],
            [helper.make_tensor_value_info('output', TensorProto.FLOAT, ['batch', 8400, columns])])
        onnx.save(helper.make_model(graph, opset_imports=[helper.make_opsetid('', 17)]), str(path))

    def test_single_class_column_is_offset_and_io_contract_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            original, candidate = Path(directory, 'production.onnx'), Path(directory, 'best.onnx')
            self.model(original)
            self.model(candidate)
            deploy_export.remap_output(candidate, {0: 2})
            deploy_export.check_remap(candidate, {0: 2})
            self.assertEqual(deploy_export.static_io(onnx.load(str(candidate))),
                             deploy_export.static_io(onnx.load(str(original))))
            from onnx.reference import ReferenceEvaluator
            rows = np.random.default_rng(1).random((3, 8400, 6), dtype=np.float32)
            rows[..., 5] = 0
            (out,) = ReferenceEvaluator(onnx.load(str(candidate))).run(None, {'input': rows})
            self.assertTrue(np.array_equal(out[..., :5], rows[..., :5]) and (out[..., 5] == 2).all())

    def test_remap_check_detects_a_wrong_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory, 'best.onnx')
            self.model(candidate)
            deploy_export.remap_output(candidate, {0: 7})
            with self.assertRaises(ValueError):
                deploy_export.check_remap(candidate, {0: 2})


if __name__ == '__main__':
    unittest.main()
