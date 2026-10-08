"""Opt-in camera-selected full-frame vehicle ROIs; no GI/PyDS imports."""
import configparser
from dataclasses import dataclass
import os
from pathlib import Path

from pgie_cache import prepare_engine_cache

VEHICLE_GIE_ID = 2
VEHICLE_ROI_ID = 90
VEHICLE_TRACKER_CLASS = 100
# Labels tracked as one vehicle class. car is required; truck/bus are optional
# (TrafficCamNet has only car, COCO YOLO models have all three).
VEHICLE_LABELS = ("car", "truck", "bus")


@dataclass(frozen=True)
class VehicleDetector:
    cameras: frozenset[str]
    config_path: str
    car_class_id: int
    vehicle_class_ids: frozenset[int] | None = None

    def __post_init__(self):
        if self.vehicle_class_ids is None:
            object.__setattr__(self, "vehicle_class_ids", frozenset({self.car_class_id}))


def prepare_vehicle_detector(cameras, cache_dir, environ=None):
    env = os.environ if environ is None else environ
    selected = frozenset(v.strip() for v in env.get("VEHICLE_CAMERA_IDS", "").split(",") if v.strip())
    if not selected:
        return None
    if not selected <= {c.camera_id for c in cameras}:
        raise ValueError("Vehicle cameras must be enabled runtime cameras")
    path = Path(env.get("VEHICLE_INFER_CONFIG", ""))
    if not path.is_file():
        raise ValueError("Vehicle detector requires a local native nvinfer model config")
    config = configparser.ConfigParser(interpolation=None)
    config.read(path)
    props = config["property"]
    for key in ("onnx-file", "tlt-encoded-model", "model-file", "proto-file", "uff-file",
                "labelfile-path", "custom-lib-path", "mean-file", "int8-calib-file"):
        if props.get(key, "").strip():
            file = (path.parent / props[key].strip()).resolve()
            if not file.is_file():
                raise ValueError("Vehicle model/config dependency missing; no download attempted")
            props[key] = str(file)
    labels = Path(props["labelfile-path"]).read_text().splitlines()
    names = [label.split(";")[0].strip().lower() for label in labels]
    ids = {name: [i for i, value in enumerate(names) if value == name] for name in VEHICLE_LABELS}
    if (len(ids["car"]) != 1 or any(len(v) > 1 for v in ids.values())
            or props.getint("num-detected-classes") != len(labels)):
        raise ValueError("Vehicle labels must identify exactly one car class and match class count")
    car = ids["car"][0]
    vehicle_ids = sorted(v[0] for v in ids.values() if v)
    # Detector threshold: explicit environment override, else the model config's
    # class/all value. Backend min_confidence stays separate, so weaker
    # candidates reach occupancy as uncertain evidence instead of vanishing.
    default = config.get("class-attrs-all", "pre-cluster-threshold", fallback="0.4")
    # Only synthetic full-frame ROIs are inferred; persons are never car-detector crops.
    props.update({"gpu-id": "0", "batch-size": str(len(selected)), "process-mode": "2",
                  "network-type": "0", "gie-unique-id": str(VEHICLE_GIE_ID),
                  "operate-on-gie-id": str(VEHICLE_ROI_ID), "operate-on-class-ids": "0",
                  "interval": "0", "secondary-reinfer-interval": "0", "classifier-async-mode": "0",
                  "input-object-min-width": "1", "input-object-min-height": "1",
                  "input-object-max-width": "0", "input-object-max-height": "0",
                  "input-tensor-meta": "0", "network-mode": props.get("network-mode", "2")})
    props["filter-out-class-ids"] = ";".join(str(i) for i in range(len(labels)) if i not in vehicle_ids)
    for class_id in vehicle_ids:
        section = f"class-attrs-{class_id}"
        if not config.has_section(section):
            config.add_section(section)
        threshold = float(env.get("VEHICLE_MIN_CONFIDENCE")
                          or config[section].get("pre-cluster-threshold", default))
        if not 0 <= threshold <= 1:
            raise ValueError("Invalid vehicle confidence threshold")
        config[section]["pre-cluster-threshold"] = str(threshold)
    directory, digest = prepare_engine_cache(props, Path(cache_dir) / "vehicle")
    generated = directory / f"vehicle_{digest}.txt"
    with generated.open("w") as target:
        config.write(target, space_around_delimiters=False)
    return VehicleDetector(selected, str(generated), car, frozenset(vehicle_ids))


def metadata_nodes(node, cast):
    while node is not None:
        value = cast(node.data)
        try:
            node = node.next
        except StopIteration:
            node = None
        yield value


def add_vehicle_rois(batch, detector, camera_for_frame, pyds):
    original_inference = {}
    for frame in metadata_nodes(batch.frame_meta_list, pyds.NvDsFrameMeta.cast):
        if camera_for_frame(frame).camera_id not in detector.cameras:
            continue
        roi = pyds.nvds_acquire_obj_meta_from_pool(batch)
        roi.unique_component_id, roi.class_id = VEHICLE_ROI_ID, 0
        roi.object_id, roi.confidence = (1 << 64) - 1, 1.0
        rect = roi.rect_params
        rect.left, rect.top, rect.width, rect.height = 0.0, 0.0, 1920.0, 1080.0
        rect.border_width = 0
        pyds.nvds_add_obj_meta_to_frame(frame, roi, None)
        original_inference[(int(frame.pad_index), int(frame.frame_num), int(frame.buf_pts))] = bool(frame.bInferDone)
        # Secondary detector sets this even for an empty result. Diagnostics for
        # PeopleNet run before this reset. Missing vehicle inference is unknown.
        frame.bInferDone = False
    return original_inference


def prepare_vehicle_tracks(batch, detector, camera_for_frame, pyds):
    for frame in metadata_nodes(batch.frame_meta_list, pyds.NvDsFrameMeta.cast):
        selected = camera_for_frame(frame).camera_id in detector.cameras
        remove = []
        for obj in metadata_nodes(frame.obj_meta_list, pyds.NvDsObjectMeta.cast):
            if obj.unique_component_id == VEHICLE_GIE_ID:
                if selected and obj.class_id in detector.vehicle_class_ids:
                    obj.parent = None
                    obj.class_id = VEHICLE_TRACKER_CLASS
                else:
                    remove.append(obj)
            elif obj.unique_component_id == VEHICLE_ROI_ID:
                remove.append(obj)
        # Children are detached before returning the ROI parent to the meta pool.
        for obj in remove:
            pyds.nvds_remove_obj_meta_from_frame(frame, obj)
