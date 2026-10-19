"""Export registered YOLO11m weights with the installed production DeepStream-Yolo exporter."""
import argparse
import json
import os
from pathlib import Path
import runpy
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vehicle_deployment import DIRECTORY, ROOT, digest, local_path, read_json, write_json


def shape(value):
    return [d.dim_value or d.dim_param for d in value.type.tensor_type.shape.dim]


def static_io(model):
    """Input/output names, types and shapes with batch fixed to 1 (exporter leaves output dims symbolic)."""
    import onnx
    model = onnx.ModelProto.FromString(model.SerializeToString())
    for value in model.graph.input:
        value.type.tensor_type.shape.dim[0].dim_value = 1
    model = onnx.shape_inference.infer_shapes(model, data_prop=True)
    return ([(v.name, v.type.tensor_type.elem_type, shape(v)) for v in model.graph.input],
            [(v.name, v.type.tensor_type.elem_type, shape(v)) for v in model.graph.output])


def validate_onnx(baseline, candidate):
    """Input size, output layout ([boxes, score, class] per anchor) and dynamic batch must match production."""
    import onnx
    old, new = onnx.load(str(baseline)), onnx.load(str(candidate))
    onnx.checker.check_model(new)
    if len(new.graph.input) != 1 or len(new.graph.output) != 1:
        raise ValueError('Expected one input and one output')
    if not isinstance(shape(new.graph.input[0])[0], str):
        raise ValueError('Dynamic batch is required')
    io = static_io(new)
    if io != static_io(old):
        raise ValueError('ONNX input/output contract differs from production')
    if io[0][0][2] != [1, 3, 640, 640] or io[1][0][2] != [1, 8400, 6]:
        raise ValueError('Only the production 640x640 YOLO11m decoded output is supported')


SINGLE_NAMES = ('car', 'item')  # Ultralytics single_cls renames the class to "item"


def class_map(names, runtime_labels, class_mode):
    """Model class id -> running detector class id; the runtime config (80 labels, filters,
    per-class thresholds, tracker mapping) never changes, so a 1-class car head is remapped."""
    if class_mode == 'vehicle3':
        if names != runtime_labels or len(names) != 80:
            raise ValueError('Class ordering must match the production COCO80 detector')
        return {}
    if class_mode == 'single':
        if len(names) != 1 or names[0].lower() not in SINGLE_NAMES:
            raise ValueError('A single-class model must have exactly one car class')
        cars = [i for i, label in enumerate(runtime_labels) if label.split(';')[0].strip().lower() == 'car']
        if len(cars) != 1:
            raise ValueError('The running detector must have exactly one car label')
        return {0: cars[0]}
    raise ValueError('Unsupported class mode')


def check_architecture(candidate, baseline, classes):
    """YOLO11m layers must be identical; only the Detect class-output convs may shrink 80 -> classes."""
    old = {k: tuple(v.shape) for k, v in baseline.model.state_dict().items()}
    new = {k: tuple(v.shape) for k, v in candidate.model.state_dict().items()}
    if candidate.task != 'detect' or old.keys() != new.keys():
        raise ValueError('Weights must preserve the YOLO11m architecture')
    for key, shape in new.items():
        if shape != old[key] and not ('.cv3.' in key and old[key][0] == 80 and shape[0] == classes
                                      and shape[1:] == old[key][1:]):
            raise ValueError('Weights must preserve the YOLO11m architecture')


def remap_output(path, mapping):
    """Append class-column offset nodes to the exported [batch, 8400, 6] output (TensorRT-friendly
    Slice/Add/Concat). Only constant 1-class maps are supported."""
    import onnx
    from onnx import TensorProto, helper
    ((source, target),) = mapping.items()
    model = onnx.load(str(path))
    name = model.graph.output[0].name
    for node in model.graph.node:
        node.output[:] = [name + '_raw' if out == name else out for out in node.output]
    const = lambda n, values, kind=TensorProto.INT64: helper.make_node(
        'Constant', [], [n], value=helper.make_tensor(n + '_v', kind, [len(values)], values))
    model.graph.node.extend([
        const('remap_axis', [2]), const('remap_0', [0]), const('remap_5', [5]), const('remap_6', [6]),
        const('remap_offset', [float(target - source)], TensorProto.FLOAT),
        helper.make_node('Slice', [name + '_raw', 'remap_0', 'remap_5', 'remap_axis'], ['remap_box_score']),
        helper.make_node('Slice', [name + '_raw', 'remap_5', 'remap_6', 'remap_axis'], ['remap_class']),
        helper.make_node('Add', ['remap_class', 'remap_offset'], ['remap_class_mapped']),
        helper.make_node('Concat', ['remap_box_score', 'remap_class_mapped'], [name], axis=2)])
    onnx.checker.check_model(model)
    onnx.save(model, str(path))


def check_remap(path, mapping):
    """Run only the appended nodes on synthetic rows: boxes/scores unchanged, class source -> target."""
    import numpy as np
    import onnx
    import onnx.utils
    from onnx.reference import ReferenceEvaluator
    model = onnx.load(str(path))
    name = model.graph.output[0].name
    tail = onnx.utils.Extractor(model).extract_model([name + '_raw'], [name])
    rows = np.random.default_rng(0).random((2, 8400, 6), dtype=np.float32)
    ((source, target),) = mapping.items()
    rows[..., 5] = source
    (out,) = ReferenceEvaluator(tail).run(None, {name + '_raw': rows})
    if out.shape != rows.shape or not np.array_equal(out[..., :5], rows[..., :5]) or not (out[..., 5] == target).all():
        raise ValueError('Class remapping produced unexpected output')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--weights', type=Path, required=True)
    p.add_argument('--job', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--sha256', required=True)
    p.add_argument('--classes', choices=('vehicle3', 'single'), default='vehicle3')
    p.add_argument('--imgsz', type=int, default=640)
    args = p.parse_args()
    from uuid import UUID
    if str(UUID(args.job)) != args.job:
        raise ValueError('Invalid job id')
    if args.imgsz != 640:
        # nvinfer's on-the-fly update rejects a different network resolution; other sizes need a
        # restart with a new config and new FPS/VRAM measurements, so they are never auto-applied.
        raise ValueError('Only 640px models can be hot-deployed')
    runtime = read_json(DIRECTORY / 'status.json', {})
    if digest(args.weights) != args.sha256:
        raise ValueError('Registered weights changed')
    from ultralytics import YOLO
    candidate = YOLO(str(args.weights))
    baseline = YOLO(str(ROOT / 'training/weights/yolo11m.pt'))
    names = [candidate.names[i] for i in range(len(candidate.names))]
    mapping = class_map(names, runtime['labels'], args.classes)
    check_architecture(candidate, baseline, len(names))
    del candidate, baseline
    directory = DIRECTORY / args.job
    directory.mkdir(parents=True, exist_ok=True)
    weights = directory / 'best.pt'
    shutil.copyfile(args.weights, weights)
    exporter = ROOT / 'models/yolo11m/DeepStream-Yolo/utils/export_yolo11.py'
    os.chdir(directory)  # exporter writes labels.txt to cwd
    sys.argv = [str(exporter), '-w', str(weights), '--dynamic', '--simplify', '-s', '640']
    runpy.run_path(str(exporter), run_name='__main__')
    onnx = directory / 'best.onnx'
    if (directory / 'labels.txt').read_text().splitlines() != names:
        raise ValueError('Exporter changed class ordering')
    if mapping:
        remap_output(onnx, mapping)
        check_remap(onnx, mapping)
    validate_onnx(local_path(runtime['onnx']), onnx)
    print(f'Class map (model -> runtime): {mapping or "identity (COCO80)"}', flush=True)
    write_json(directory / 'manifest.json', {
        'model_id': args.model, 'sha256': digest(onnx), 'weights_sha256': args.sha256, 'contract': runtime['contract'],
        'class_mode': args.classes, 'source_names': names,
        'class_map': {str(k): v for k, v in mapping.items()} or None})


if __name__ == '__main__':
    main()
