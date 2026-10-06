#!/usr/bin/env python3
"""Detect people in multiple RTSP streams with DeepStream, without a display."""

import argparse
import configparser
import hashlib
import os
import signal
import sys
import threading
import time
from urllib.parse import urlsplit
from pathlib import Path

# GStreamer's default diagnostics can include RTSP URIs and credentials.
# Only the sanitized application messages below should be printed.
os.environ["GST_DEBUG"] = "0"
sys.dont_write_bytecode = True

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst
import pyds

PGIE_ID = 1
SAMPLES_ROOT = Path("/opt/nvidia/deepstream/deepstream/samples")
PEOPLENET_CONFIG = Path(__file__).resolve().parent / "configs/pgie_peoplenet.txt"


def load_peoplenet_config():
    """Prefer complete installed NVIDIA PeopleNet samples; never download files."""
    installed_configs = sorted(
        (SAMPLES_ROOT / "configs").rglob("config_infer_primary_peoplenet.txt")
    )
    for path in [*installed_configs, PEOPLENET_CONFIG]:
        config = configparser.ConfigParser(interpolation=None)
        with path.open(encoding="utf-8") as source:
            config.read_file(source)
        if not config.has_section("property"):
            raise ValueError(f"PeopleNet config has no [property] section: {path}")
        props = config["property"]
        if path == PEOPLENET_CONFIG:
            # Reuse a bundled model even when its accompanying config is absent.
            models = sorted((SAMPLES_ROOT / "models").rglob("*peoplenet*.onnx"))
            for model in models:
                labels = [model.parent / "labels.txt", *sorted(
                    (SAMPLES_ROOT / "configs").rglob("labels_peoplenet.txt")
                )]
                label = next((label for label in labels if label.is_file()), None)
                if label is not None:
                    props["onnx-file"] = str(model)
                    props["labelfile-path"] = str(label)
                    break
        model_keys = ("onnx-file", "tlt-encoded-model", "model-file", "uff-file")
        required = [props[key] for key in (*model_keys, "labelfile-path")
                    if props.get(key, "").strip()]
        has_model = any(props.get(key, "").strip() for key in model_keys)
        if has_model and props.get("labelfile-path") and all(
            (path.parent / filename.strip()).is_file() for filename in required
        ):
            return path, config
    raise ValueError(
        "PeopleNet model/labels not found. Place official NGC "
        "deployable_quantized_onnx_v2.6.2 resnet34_peoplenet.onnx and labels.txt "
        "in models/peoplenet/ (see README.md); no model was downloaded"
    )


def prepare_pgie_config(cache_dir, batch_size):
    """Reuse PeopleNet settings, keeping FP16 config/engine in the project."""
    sample_config, config = load_peoplenet_config()
    props = config["property"]
    # Relative model paths belong to the original config directory, not .cache.
    # INT8 calibration is unnecessary: this stage builds an FP16 engine.
    props.pop("int8-calib-file", None)
    path_keys = (
        "onnx-file", "tlt-encoded-model", "model-file", "proto-file", "uff-file",
        "labelfile-path", "custom-lib-path", "mean-file",
    )
    for key in path_keys:
        if props.get(key, "").strip():
            path = Path(props[key].strip())
            if not path.is_absolute():
                path = sample_config.parent / path
            path = path.resolve()
            if not path.is_file():
                raise ValueError(f"Missing sample {key}: {path}; no model was downloaded")
            props[key] = str(path)
    model_keys = ("onnx-file", "tlt-encoded-model", "model-file", "uff-file")
    model_paths = [props[key] for key in model_keys if props.get(key, "").strip()]
    if not model_paths:
        raise ValueError("Sample config has no source model; no model was downloaded")
    if not props.get("labelfile-path"):
        raise ValueError("Sample detector labels file is required")
    labels = Path(props["labelfile-path"]).read_text(encoding="utf-8").splitlines()
    person_ids = [
        index for index, label in enumerate(labels)
        if label.split(";")[0].strip().casefold() == "person"
    ]
    if len(person_ids) != 1:
        raise ValueError("Sample detector labels must contain exactly one person class")
    person_id = person_ids[0]
    class_count = props.getint("num-detected-classes")
    if class_count != len(labels):
        raise ValueError("Sample class count does not match labels")
    props.update({
        "gpu-id": "0", "batch-size": str(batch_size), "process-mode": "1",
        "network-type": "0", "network-mode": "2", "interval": "0",
        "gie-unique-id": str(PGIE_ID),
    })
    other_ids = [str(index) for index in range(class_count) if index != person_id]
    props.pop("filter-out-class-ids", None)
    if other_ids:
        props["filter-out-class-ids"] = ";".join(other_ids)
    # Separate engines for batch sizes/configs and changed sample model files.
    props.pop("model-engine-file", None)
    fingerprint = repr([(section, dict(config[section])) for section in config.sections()])
    for model_path in model_paths:
        stat = Path(model_path).stat()
        fingerprint += f"{stat.st_size}:{stat.st_mtime_ns}"
    digest = hashlib.sha256(fingerprint.encode()).hexdigest()[:12]
    pgie_dir = Path(cache_dir) / "pgie"
    pgie_dir.mkdir(parents=True, exist_ok=True)
    props["model-engine-file"] = str(pgie_dir / f"peoplenet_b{batch_size}_gpu0_fp16_{digest}.engine")
    generated = pgie_dir / f"config_peoplenet_b{batch_size}_{digest}.txt"
    with generated.open("w", encoding="utf-8") as target:
        config.write(target, space_around_delimiters=False)
    print(
        f"PGIE detector=PeopleNet sample={sample_config} model={Path(model_paths[0]).name} "
        f"person-class-id={person_id} batch-size={batch_size} precision=FP16", flush=True,
    )
    return str(generated), person_id


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's original message may contain a supplied URL.
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid arguments. Use --help for usage; input values are hidden.\n")


def resolve_inputs(parser, cli_inputs):
    """Prefer explicit CLI inputs; otherwise use the two environment inputs."""
    inputs = cli_inputs if cli_inputs is not None else [
        value for name in ("RTSP_URL", "RTSP_URL_2")
        if (value := os.environ.get(name, "").strip())
    ]
    if not inputs:
        parser.error("Supply --input or set RTSP_URL / RTSP_URL_2")
    return inputs


def parse_args():
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", action="append", metavar="RTSP_URI",
        help="RTSP input; repeat for each camera (overrides environment inputs)",
    )
    args = parser.parse_args()
    args.input = resolve_inputs(parser, args.input)
    for index, uri in enumerate(args.input):
        try:
            parsed = urlsplit(uri)
            valid = parsed.scheme.lower() == "rtsp" and bool(parsed.hostname)
            # Accessing port also checks malformed port values without logging them.
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            parser.exit(2, f"source={index}: invalid RTSP URI (value hidden).\n")
    return args


def make_element(factory, name):
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"Required GStreamer element is unavailable: {factory}")
    return element


def create_source_bin(index, uri, fail):
    """Use test3's dynamic video pad / NVMM ghost-pad source-bin pattern."""
    source_bin = Gst.Bin.new(f"source-bin-{index}")
    if source_bin is None:
        raise RuntimeError(f"source={index}: could not create source bin")
    decoder = make_element("uridecodebin", f"uri-decode-bin-{index}")
    decoder.set_property("uri", uri)
    source_bin.add(decoder)
    ghost_pad = Gst.GhostPad.new_no_target("src", Gst.PadDirection.SRC)
    if ghost_pad is None or not source_bin.add_pad(ghost_pad):
        raise RuntimeError(f"source={index}: could not create source pad")

    def on_pad_added(decodebin, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps is None or caps.get_size() == 0:
            fail(f"source={index}: decoder pad has no caps")
            return
        if not caps.get_structure(0).get_name().startswith("video/"):
            return  # Ignore RTSP audio pads.
        if not caps.get_features(0).contains("memory:NVMM"):
            fail(f"source={index}: video decoder output is not NVIDIA NVMM")
            return
        if ghost_pad.get_target() is not None:
            fail(f"source={index}: multiple video tracks are unsupported")
            return
        if not ghost_pad.set_target(pad):
            fail(f"source={index}: could not link decoder video pad")
            return
        print(f"source={index}: NVIDIA NVMM video pad linked", flush=True)

    def on_source_setup(decodebin, source):
        # uridecodebin creates rtspsrc internally; bound its jitter buffer.
        if source.find_property("latency") is not None:
            source.set_property("latency", 200)
        if source.find_property("drop-on-latency") is not None:
            source.set_property("drop-on-latency", True)

    decoder.connect("pad-added", on_pad_added)
    decoder.connect("source-setup", on_source_setup)
    return source_bin


def run(inputs):
    # Keep GStreamer's registry cache inside the project, including in Docker.
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        print("Could not create project .cache directory", file=sys.stderr)
        return 1
    try:
        pgie_config, person_class_id = prepare_pgie_config(cache_dir, len(inputs))
    except (OSError, ValueError, configparser.Error) as error:
        # Config/model errors contain file paths, never the supplied RTSP inputs.
        print(f"PGIE setup failed: {error}", file=sys.stderr)
        return 1
    os.environ["GST_REGISTRY"] = os.path.join(cache_dir, "gstreamer-registry.bin")
    Gst.init(None)
    Gst.debug_set_active(False)
    pipeline = Gst.Pipeline.new("retrace")
    if pipeline is None:
        print("Could not create GStreamer pipeline", file=sys.stderr)
        return 1
    loop = GLib.MainLoop()
    counts = [0] * len(inputs)
    persons = [None] * len(inputs)
    persons_max = [0] * len(inputs)
    previous_counts = [0] * len(inputs)
    counts_lock = threading.Lock()
    previous_time = time.monotonic()
    exit_code = 0
    stopping = False
    requested_pads = []
    streammux = None
    bus = None
    bus_handler = None
    timer_id = None
    previous_handlers = {}

    def stop(reason, failed=False):
        nonlocal exit_code, stopping
        if failed:
            exit_code = 1
        if not stopping:
            stopping = True
            print(reason, flush=True)
            loop.quit()
        return GLib.SOURCE_REMOVE

    def fail(reason):
        # Pad/probe callbacks run on streaming threads. Quit on the main loop.
        GLib.idle_add(stop, reason, True)

    def report(final=False):
        nonlocal previous_time
        now = time.monotonic()
        elapsed = max(now - previous_time, 1e-9)
        with counts_lock:
            snapshot = counts.copy()
            person_snapshot = persons.copy()
            peak_snapshot = persons_max.copy()
            persons_max[:] = [0] * len(inputs)
        prefix = "final" if final else "stats"
        for index, total in enumerate(snapshot):
            fps = (total - previous_counts[index]) / elapsed
            latest = person_snapshot[index] if person_snapshot[index] is not None else "NA"
            print(
                f"{prefix} source={index} frames={total} fps={fps:.2f} "
                f"persons={latest} persons_max={peak_snapshot[index]}", flush=True,
            )
        previous_counts[:] = snapshot
        previous_time = now
        return GLib.SOURCE_CONTINUE

    def on_batch(pad, info):
        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        try:
            batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buffer))
            if batch_meta is None:
                fail("PGIE output has no DeepStream batch metadata")
                return Gst.PadProbeReturn.OK
            frame_list = batch_meta.frame_meta_list
            while frame_list is not None:
                frame = pyds.NvDsFrameMeta.cast(frame_list.data)
                index = int(frame.pad_index)
                if not 0 <= index < len(counts):
                    fail("Unexpected source index in DeepStream frame metadata")
                    break
                person_count = 0
                object_list = frame.obj_meta_list
                while object_list is not None:
                    obj = pyds.NvDsObjectMeta.cast(object_list.data)
                    if obj.unique_component_id == PGIE_ID and obj.class_id == person_class_id:
                        person_count += 1
                    try:
                        object_list = object_list.next
                    except StopIteration:
                        break
                with counts_lock:
                    counts[index] += 1
                    persons[index] = person_count
                    persons_max[index] = max(persons_max[index], person_count)
                try:
                    frame_list = frame_list.next
                except StopIteration:
                    break
        except Exception:
            # Never print exception text that could originate from an input URI.
            fail("Failed to read DeepStream batch/frame/object metadata")
        return Gst.PadProbeReturn.OK

    def message_source_id(message):
        element = message.src
        while element is not None:
            name = element.get_name()
            for index in range(len(inputs)):
                if name == f"source-bin-{index}":
                    return str(index)
            element = element.get_parent()
        return "pipeline"

    def on_message(bus, message):
        source_id = message_source_id(message)
        if message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            # Error messages and debug strings can contain authentication details.
            stop(f"GStreamer ERROR source={source_id} code={error.code}; details hidden", True)
        elif message.type == Gst.MessageType.WARNING:
            print(f"GStreamer WARNING source={source_id}; details hidden", flush=True)
        elif message.type == Gst.MessageType.EOS:
            stop("GStreamer EOS: stopping")

    def on_signal(signum, frame):
        GLib.idle_add(stop, "Stop requested: shutting down")

    try:
        streammux = make_element("nvstreammux", "stream-muxer")
        pipeline.add(streammux)
        streammux.set_property("batch-size", len(inputs))
        streammux.set_property("live-source", True)
        streammux.set_property("width", 1920)
        streammux.set_property("height", 1080)
        streammux.set_property("batched-push-timeout", 40000)

        for index, uri in enumerate(inputs):
            source_bin = create_source_bin(index, uri, fail)
            queue = make_element("queue", f"source-queue-{index}")
            pipeline.add(source_bin)
            pipeline.add(queue)
            if not source_bin.link(queue):
                raise RuntimeError(f"source={index}: could not link source queue")
            sink_pad = streammux.request_pad_simple(f"sink_{index}")
            if sink_pad is None:
                raise RuntimeError(f"source={index}: could not request mux sink pad")
            requested_pads.append(sink_pad)
            if queue.get_static_pad("src").link(sink_pad) != Gst.PadLinkReturn.OK:
                raise RuntimeError(f"source={index}: could not link mux sink pad")

        pgie = make_element("nvinfer", "primary-inference")
        pgie.set_property("config-file-path", pgie_config)
        pgie.set_property("batch-size", len(inputs))
        pipeline.add(pgie)
        sink = make_element("fakesink", "headless-sink")
        sink.set_property("sync", False)
        sink.set_property("async", False)
        sink.set_property("enable-last-sample", False)
        pipeline.add(sink)
        if not streammux.link(pgie) or not pgie.link(sink):
            raise RuntimeError("Could not link nvstreammux to PGIE to fakesink")
        pgie.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_batch)

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus_handler = bus.connect("message", on_message)
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, on_signal)
        timer_id = GLib.timeout_add_seconds(5, report)
        previous_time = time.monotonic()
        print(f"Starting {len(inputs)} RTSP sources; batch-size={len(inputs)}; headless", flush=True)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            stop("Could not start GStreamer pipeline; details hidden", True)
        else:
            loop.run()
    except KeyboardInterrupt:
        stop("Stop requested: shutting down")
    except Exception:
        # Even setup exceptions must not reveal the supplied URIs.
        stop("Pipeline setup/runtime failed; check plugins and legacy nvstreammux settings", True)
    finally:
        pipeline.set_state(Gst.State.NULL)
        if timer_id is not None:
            GLib.source_remove(timer_id)
        if bus is not None and bus_handler is not None:
            bus.disconnect(bus_handler)
            bus.remove_signal_watch()
        if streammux is not None:
            for pad in requested_pads:
                streammux.release_request_pad(pad)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        report(final=True)
        print(f"Stopped; exit-code={exit_code}", flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(run(parse_args().input))
