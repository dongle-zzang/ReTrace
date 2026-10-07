"""PeopleNet's native DeepStream engine cache (no GStreamer imports)."""

import configparser
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile


# NvDsInfer's model parsers and TrtModelBuilder consume these build options.
# Scaling/normalization, labels, interval, class filtering, clustering/NMS and
# all class-attrs-* sections operate outside the serialized TensorRT network.
BUILD_KEYS = (
    "batch-size", "network-mode", "gpu-id", "infer-dims", "input-dims",
    "uff-input-blob-name", "uff-input-order", "network-input-order",
    "output-blob-names", "workspace-size", "force-implicit-batch-dim",
    "enable-dla", "use-dla-core", "output-io-formats", "layer-device-precision",
    "tlt-model-key",
)
MODEL_KEYS = ("tlt-encoded-model", "model-file", "uff-file", "onnx-file")
BUILD_FILE_KEYS = (*MODEL_KEYS, "proto-file", "int8-calib-file", "custom-lib-path")
PRECISIONS = {"0": "fp32", "1": "int8", "2": "fp16"}


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def engine_runtime_identity(gpu_id):
    """Identify the CUDA-visible device and native libraries; never reuse unknowns."""
    try:
        cuda = ctypes.CDLL("libcuda.so.1")
        def call(name, argtypes, *args):
            function = getattr(cuda, name)
            function.argtypes, function.restype = argtypes, ctypes.c_int
            if function(*args) != 0:
                raise ValueError("Cannot identify PGIE CUDA device for engine caching")
        integer = ctypes.c_int
        pointer = ctypes.POINTER(integer)
        call("cuInit", [ctypes.c_uint], 0)
        device, major, minor = integer(), integer(), integer()
        call("cuDeviceGet", [pointer, integer], ctypes.byref(device), gpu_id)
        name = ctypes.create_string_buffer(256)
        call("cuDeviceGetName", [ctypes.c_char_p, integer, integer], name, len(name), device)
        call("cuDeviceComputeCapability", [pointer, pointer, integer],
             ctypes.byref(major), ctypes.byref(minor), device)
        trt = ctypes.CDLL("libnvinfer.so")
        trt.getInferLibVersion.argtypes = []
        trt.getInferLibVersion.restype = ctypes.c_int
        # SDK builder implementation changes can change the engine too.
        sdk = Path("/opt/nvidia/deepstream/deepstream/lib/libnvds_infer.so")
        return {"gpu": name.value.decode(), "compute": [major.value, minor.value],
                "tensorrt": trt.getInferLibVersion(), "nvdsinfer": file_digest(sdk)}
    except (OSError, AttributeError) as error:
        raise ValueError("Cannot identify PGIE GPU/TensorRT/DeepStream cache environment") from error


def engine_cache_key(props, runtime):
    """Hash explicit build inputs, independent of config order and runtime tuning."""
    files = {key: file_digest(props[key]) for key in BUILD_FILE_KEYS
             if props.get(key, "").strip()
             and (key != "int8-calib-file" or props.get("network-mode") == "1")}
    identity = {"policy": 2, "runtime": runtime, "files": files,
                "build": {key: props[key].strip() for key in BUILD_KEYS if key in props}}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]


def stage_model(source, target):
    """Publish a real file: nvinfer's realpath() would undo a symbolic link."""
    source, target = Path(source), Path(target)
    if target.is_file() and (os.path.samefile(source, target)
                             or file_digest(target) == file_digest(source)):
        return
    with tempfile.TemporaryDirectory(dir=target.parent) as directory:
        temporary = Path(directory) / "model"
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copyfile(source, temporary)
        os.replace(temporary, target)


def prepare_engine_cache(props, cache_dir, runtime=None):
    """Make native model-derived serialization and explicit loading paths agree."""
    # Custom builders may name output relative to cwd and consume arbitrary
    # config values. Do not silently apply the native-parser policy to them.
    if props.get("engine-create-func-name") or props.get("custom-network-config"):
        raise ValueError("PGIE engine cache requires the native PeopleNet model builder")
    model_keys = [key for key in MODEL_KEYS if props.get(key, "").strip()]
    if len(model_keys) != 1:
        raise ValueError("PGIE engine cache requires exactly one source model")
    precision = PRECISIONS.get(props["network-mode"])
    if precision is None:
        raise ValueError("PGIE network-mode must be 0 (FP32), 1 (INT8), or 2 (FP16)")
    if runtime is None:
        runtime = engine_runtime_identity(int(props["gpu-id"]))
    digest = engine_cache_key(props, runtime)
    directory = (Path(cache_dir) / "pgie").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    key = model_keys[0]
    source = Path(props[key])
    batch = props["batch-size"]
    staged = directory / f"peoplenet_b{batch}_{digest}{source.suffix}"
    stage_model(source, staged)
    props[key] = str(staged)
    device = (f"dla{props.get('use-dla-core', '0')}" if props.get("enable-dla") == "1"
              else f"gpu{props['gpu-id']}")
    # DS 7's TrtModelBuilder suggests <model>_bN_gpuN_precision.engine.
    # model-engine-file is a load path; its GObject override does not change
    # that builder naming rule. Stage the model under the build key so even
    # cold serialization lands in .cache/pgie without copying engine files.
    engine = Path(f"{staged}_b{batch}_{device}_{precision}.engine")
    # On unsupported hardware/calibration, the native builder can downgrade
    # INT8 -> FP16 -> FP32. Only search this exact build/runtime namespace;
    # a legacy engine beside the original model cannot prove compatibility.
    if not engine.is_file():
        fallbacks = {"int8": ("fp16", "fp32"), "fp16": ("fp32",), "fp32": ()}
        for fallback in fallbacks[precision]:
            candidate = Path(f"{staged}_b{batch}_{device}_{fallback}.engine")
            if candidate.is_file():
                engine = candidate
                break
    props["model-engine-file"] = str(engine)
    return directory, digest


def configure_pgie(element, config_path, batch_size):
    """Apply matching config/GObject paths before any pipeline state transition."""
    config = configparser.ConfigParser(interpolation=None)
    with Path(config_path).open(encoding="utf-8") as source:
        config.read_file(source)
    element.set_property("config-file-path", str(config_path))
    element.set_property("model-engine-file", config["property"]["model-engine-file"])
    element.set_property("batch-size", batch_size)
