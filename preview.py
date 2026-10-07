#!/usr/bin/env python3
"""Batched DeepStream person detection/tracking with MJPEG preview."""

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid

PROCESS_ENTRY = time.monotonic()
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

gst_debug = "0"
sys.dont_write_bytecode = True

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstRtsp", "1.0")
from gi.repository import GLib, Gst, GstRtsp
import pyds
from app import PGIE_ID, create_source_bin, prepare_pgie_config
from pgie_cache import configure_pgie
from camera_config import CameraConfigError, load_cameras
from camera_runtime import CameraRuntime, CameraRuntimeManager
from person_metadata import FrameMetadata, MetadataStore, person_from_object, utc_timestamp
from preview_diagnostics import InferenceDiagnostics, log_safe_traceback, log_settings
from startup_timing import StartupTiming, startup_origin, source_observer, bus_observer
from preview_timing import FrameTiming
from preview_mjpeg import FrameStore, serve_mjpeg
from preview_web import FrontendFiles, BackendAPIProxy
from rtsp_diagnostics import classify_bus_error, source_failure_detail

# app.py sets its own debug default at import; retain preview's requested level.
os.environ["GST_DEBUG"] = gst_debug

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DEEPSTREAM_ROOT = Path("/opt/nvidia/deepstream/deepstream")
TRACKER_CONFIG = DEEPSTREAM_ROOT / "samples/configs/deepstream-app/config_tracker_NvDCF_perf.yml"
TRACKER_LIBRARY = DEEPSTREAM_ROOT / "lib/libnvds_nvmultiobjecttracker.so"
UNTRACKED_OBJECT_ID = (1 << 64) - 1


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid arguments; values hidden. Use --help.\n")


def parse_args():
    parser = SafeParser(description=__doc__)
    parser.add_argument("--input", action="append", metavar="RTSP_URI",
                        help="Compatibility input; repeat for each camera (overrides camera config)")
    parser.add_argument("--cameras", type=Path, default=ROOT / "configs/cameras.yaml")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--port", type=int, default=os.environ.get("WEB_PORT", "40225"),
                        help="HTTP listen port (default: WEB_PORT environment variable or 40225)")
    parser.add_argument("--jpeg-quality", type=int, default=80)
    parser.add_argument(
        "--rtsp-latency", type=int, default=1000, metavar="MS",
        help="RTSP jitter-buffer latency in milliseconds (default: 1000)",
    )
    parser.add_argument(
        "--rtsp-drop-on-latency", action=argparse.BooleanOptionalAction, default=True,
        help="Discard RTSP jitter-buffer frames beyond latency; default: enabled",
    )
    parser.add_argument(
        "--mux-live-source", type=int, choices=(0, 1), default=1,
        help="Legacy mux timestamp mode; 0 is an experiment for matching-FPS sources",
    )
    parser.add_argument(
        "--diagnostics", action="store_true",
        help="Log startup milestones and per-stage arrival gaps/PTS every five seconds",
    )
    parser.add_argument("--startup-source-count", type=int, choices=(1, 2, 4, 8),
                        help="Diagnostics experiment: first N enabled cameras in config order")
    parser.add_argument("--camera-degraded-after", type=float, default=3.0, metavar="SECONDS")
    parser.add_argument("--camera-offline-after", type=float, default=10.0, metavar="SECONDS")
    parser.add_argument("--camera-connect-timeout", type=float, default=30.0, metavar="SECONDS")
    parser.add_argument("--camera-min-fps", type=float, default=1.0)
    args = parser.parse_args()
    try:
        CameraRuntime("validation", args.camera_degraded_after, args.camera_offline_after,
                      args.camera_connect_timeout, args.camera_min_fps)
    except ValueError:
        parser.error("Invalid camera health thresholds")
    try:
        args.cameras = load_cameras(args.cameras, args.env_file, args.input)
    except CameraConfigError as error:
        parser.exit(2, f"{error}\n")
    if args.startup_source_count is not None:
        if not args.diagnostics or args.startup_source_count > len(args.cameras):
            parser.error("Source-count experiment requires diagnostics and enough enabled cameras")
        args.cameras = args.cameras[:args.startup_source_count]
    args.input = [camera.rtsp_url for camera in args.cameras]
    if not 1 <= args.port <= 65535:
        parser.error("port")
    if not 1 <= args.jpeg_quality <= 100:
        parser.error("JPEG quality must be between 1 and 100")
    if not 0 <= args.rtsp_latency <= (1 << 32) - 1:
        parser.error("RTSP latency must be a non-negative guint in milliseconds")
    return args


class PreviewHandler(BaseHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def finish(self):
        # A disconnect may also occur when the base handler flushes on cleanup.
        try:
            super().finish()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api' or path.startswith('/api/'):
            proxy = getattr(self.server, 'backend_api_proxy', None)
            if proxy is None:
                self.send_error(503)
            else:
                proxy.serve(self)
            return
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/index.html": ("index.html", "text/html; charset=utf-8"),
                  "/preview.js": ("preview.js", "text/javascript; charset=utf-8")}
        if path in self.server.mjpeg_streams:
            if getattr(self, 'command', 'GET') == 'HEAD':
                self.send_response(200)
                self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
            else:
                serve_mjpeg(self, self.server.mjpeg_streams[path])
            return
        if path in ("/streams.json", "/metadata.json"):
            if path == "/metadata.json":
                if not hasattr(self.server, "metadata_snapshot"):
                    self.send_error(404)
                    return
                payload = self.server.metadata_snapshot()
            else:
                payload = self.server.stream_snapshot() if hasattr(self.server, "stream_snapshot") else self.server.streams
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if getattr(self, 'command', 'GET') != 'HEAD':
                self.wfile.write(body)
            return
        if path.startswith('/mjpeg/'):
            self.send_error(404)
            return
        frontend = getattr(self.server, 'frontend_files', None)
        if frontend is not None and frontend.serve(self, path):
            return
        if path in assets:
            filename, content_type = assets[path]
            try:
                body = (WEB_ROOT / filename).read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if getattr(self, 'command', 'GET') != 'HEAD':
                self.wfile.write(body)
            return
        self.send_error(404)

    def do_HEAD(self):
        self.do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format, *args):
        # Do not echo arbitrary request URLs into terminal logs.
        pass

    def log_request(self, code="-", size="-"):
        # Keep real HTTP failures visible without logging request URLs.
        if isinstance(code, int) and code >= 400:
            print(f"HTTP error status={code}", file=sys.stderr, flush=True)


def make_element(factory, name):
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"Required GStreamer element missing: {factory}")
    return element


def create_preview_source_bin(index, uri, latency, fail, drop_on_latency=True, reconnect_interval=10, observer=None):
    """Reuse app.py's NVMM source, with preview-specific transport/buffering."""
    options = {"observer": observer} if observer is not None else {}
    source_bin = create_source_bin(index, uri, fail, source_factory="nvurisrcbin", **options)
    decoder = source_bin.get_by_name(f"uri-decode-bin-{index}")
    if decoder is None:
        raise RuntimeError(f"source={index}: nvurisrcbin not found in source bin")
    # DS 7.0 native source reconnection; mux/demux pads stay registered.
    for name, value in (("select-rtp-protocol", 4), ("latency", latency),
                        ("rtsp-reconnect-interval", reconnect_interval),
                        ("rtsp-reconnect-attempts", -1), ("num-extra-surfaces", 4),
                        ("cudadec-memtype", 0), ("async-handling", True)):
        if decoder.find_property(name) is None:
            raise RuntimeError(f"nvurisrcbin missing required property: {name}")
        decoder.set_property(name, value)
    for name, value in (("drop-on-latency", drop_on_latency), ("disable-audio", True)):
        if decoder.find_property(name) is not None:
            decoder.set_property(name, value)

    # Native properties above own RTSP/decoder configuration, including reconnects.
    # Child traversal is only needed to observe diagnostics, never to set properties.
    if observer is not None:
        def on_child_added(proxy, child, name):
            observer("child", child)
            if isinstance(child, Gst.ChildProxy):
                child.connect("child-added", on_child_added)

        decoder.connect("child-added", on_child_added)

        def observe_existing(bin_element):
            if not isinstance(bin_element, Gst.Bin):
                return
            iterator = bin_element.iterate_elements()
            while True:
                result, child = iterator.next()
                if result == Gst.IteratorResult.OK:
                    on_child_added(bin_element, child, child.get_name())
                    observe_existing(child)
                elif result == Gst.IteratorResult.RESYNC:
                    iterator.resync()
                else:
                    break

        observe_existing(decoder)
    return source_bin


def make_tracker():
    """Reuse the SDK's NvDCF perf preset; no Re-ID model is added."""
    for path in (TRACKER_CONFIG, TRACKER_LIBRARY):
        if not path.is_file():
            raise FileNotFoundError(f"Required DeepStream tracker file missing: {path}")
    tracker = make_element("nvtracker", "person-tracker")
    tracker.set_property("ll-lib-file", str(TRACKER_LIBRARY))
    tracker.set_property("ll-config-file", str(TRACKER_CONFIG))
    tracker.set_property("gpu-id", 0)
    tracker.set_property("tracker-width", 640)
    tracker.set_property("tracker-height", 384)
    # Older plugins expose this switch; DS 7.x uses batched tracking by default.
    if tracker.find_property("enable-batch-process") is not None:
        tracker.set_property("enable-batch-process", True)
    # The output probe formats Person <id> explicitly, avoiding duplicate ID text.
    tracker.set_property("display-tracking-id", False)
    print(
        f"Tracker=NvDCF config={TRACKER_CONFIG} library={TRACKER_LIBRARY} "
        "gpu=0 size=640x384 batch-processing=enabled", flush=True,
    )
    return tracker


def build_output(index, args, store, fail):
    """Draw the existing OSD result and publish only the latest encoded JPEG."""
    output_bin = Gst.Bin.new(f"mjpeg-source-{index}")
    if output_bin is None:
        raise RuntimeError(f"source={index}: could not create output bin")
    try:
        queue = make_element("queue", f"queue-{index}")
        queue.set_property("max-size-buffers", 1)
        queue.set_property("max-size-bytes", 0)
        queue.set_property("max-size-time", 0)
        queue.set_property("leaky", 2)
        osd_convert = make_element("nvvideoconvert", f"osd-convert-{index}")
        rgba_caps = make_element("capsfilter", f"osd-rgba-caps-{index}")
        rgba_caps.set_property("caps", Gst.Caps.from_string("video/x-raw(memory:NVMM),format=RGBA"))
        osd = make_element("nvdsosd", f"person-osd-{index}")
        osd.set_property("process-mode", 1)
        osd.set_property("display-bbox", True)
        osd.set_property("display-text", True)
        osd.set_property("display-mask", False)
        converter = make_element("nvvideoconvert", f"convert-mjpeg-{index}")
        raw_caps = make_element("capsfilter", f"raw-caps-mjpeg-{index}")
        raw_caps.set_property("caps", Gst.Caps.from_string(
            "video/x-raw(memory:NVMM),format=I420,width=1280,height=720"
        ))
        encoder = make_element("nvjpegenc", f"jpeg-encoder-{index}")
        encoder.set_property("quality", args.jpeg_quality)
        sink = make_element("appsink", f"jpeg-sink-{index}")
        sink.set_property("emit-signals", True)
        sink.set_property("sync", False)
        sink.set_property("async", False)  # Offline branches must not hold pipeline preroll.
        sink.set_property("max-buffers", 1)
        sink.set_property("drop", True)
        sink.set_property("wait-on-eos", False)

        def on_sample(appsink):
            try:
                sample = appsink.emit("pull-sample")
                if sample is None:
                    return Gst.FlowReturn.EOS
                buffer = sample.get_buffer()
                mapped, info = buffer.map(Gst.MapFlags.READ)
                if not mapped:
                    raise RuntimeError("Could not map encoded JPEG")
                try:
                    if hasattr(store, "put_frame"):
                        store.put_frame(bytes(info.data), buffer.pts)
                    else:
                        store.put(bytes(info.data))
                finally:
                    buffer.unmap(info)
                return Gst.FlowReturn.OK
            except Exception:
                fail("JPEG output failed; details hidden")
                # A Python JPEG handoff failure must not poison shared upstream flow.
                return Gst.FlowReturn.OK

        sink.connect("new-sample", on_sample)
        chain = (queue, osd_convert, rgba_caps, osd, converter, raw_caps, encoder, sink)
        for element in chain:
            output_bin.add(element)
        for left, right in zip(chain, chain[1:]):
            if not left.link(right):
                raise RuntimeError(f"Could not link {left.get_name()} -> {right.get_name()}")
        ghost_pad = Gst.GhostPad.new("sink", queue.get_static_pad("sink"))
        if ghost_pad is None or not output_bin.add_pad(ghost_pad):
            raise RuntimeError("Could not create output sink pad")
        return output_bin
    except Exception:
        output_bin.set_state(Gst.State.NULL)
        raise


def run_shared_pipeline(args, metadata_store, stores, manager, shutdown,
                        pgie_config, person_class_id):
    """One mux/PGIE/tracker/demux; source bins own native RTSP reconnection."""
    startup = getattr(args, "startup_timing", None)
    if startup is not None:
        startup.start_gpu_sampling()
    cameras_by_source = manager.cameras_by_slot
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    os.environ["GST_REGISTRY"] = str(cache / "gstreamer-registry.bin")
    Gst.init(None)
    Gst.debug_set_active(False)
    pipeline = None
    requested_pads = []
    bus = None
    failed = None
    pipeline_stop = threading.Event()
    setup_stage = "shared-elements"
    counts = [0] * len(args.input)
    persons = [None] * len(args.input)
    persons_max = [0] * len(args.input)
    track_ids = [[] for _ in args.input]
    previous_counts = [0] * len(args.input)
    counts_lock = threading.Lock()
    previous_time = time.monotonic()
    timings = {}
    inference_stats = {index: InferenceDiagnostics() for index in cameras_by_source} if args.diagnostics else {}
    metadata_samples = {}  # At most five people from the latest nonempty frame per report.

    def camera_for_frame(frame):
        return manager.camera_for_frame(int(frame.source_id), int(frame.pad_index))

    def on_inference(_pad, info):
        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        try:
            batch = pyds.gst_buffer_get_nvds_batch_meta(hash(buffer))
            if batch is None:
                raise RuntimeError("PGIE output has no batch metadata")
            frames = batch.frame_meta_list
            while frames is not None:
                frame = pyds.NvDsFrameMeta.cast(frames.data)
                camera_for_frame(frame)
                confidences = []
                objects = frame.obj_meta_list
                while objects is not None:
                    obj = pyds.NvDsObjectMeta.cast(objects.data)
                    if obj.unique_component_id == PGIE_ID and obj.class_id == person_class_id:
                        confidences.append(float(obj.confidence))
                    try:
                        objects = objects.next
                    except StopIteration:
                        break
                pts = int(frame.buf_pts)
                inference_stats[int(frame.pad_index)].observe(
                    bool(frame.bInferDone), confidences,
                    None if pts == Gst.CLOCK_TIME_NONE else pts,
                )
                try:
                    frames = frames.next
                except StopIteration:
                    break
        except Exception:
            fail("Inference diagnostics failed; details hidden")
        return Gst.PadProbeReturn.OK

    def startup_probe(pad, stage, slot=None, batched=False):
        if startup is None:
            return

        def observe(_pad, info):
            buffer = info.get_buffer()
            if buffer is None:
                return Gst.PadProbeReturn.OK
            startup.mark(stage, slot, None if batched or buffer.pts == Gst.CLOCK_TIME_NONE else buffer.pts)
            if batched:
                try:
                    batch = pyds.gst_buffer_get_nvds_batch_meta(hash(buffer))
                    frames = batch.frame_meta_list if batch else None
                    while frames is not None:
                        frame = pyds.NvDsFrameMeta.cast(frames.data)
                        camera_for_frame(frame)
                        index = int(frame.pad_index)
                        startup.mark(stage, index, None if frame.buf_pts == Gst.CLOCK_TIME_NONE else frame.buf_pts)
                        if stage == "first_pgie_output" and frame.bInferDone:
                            startup.mark("first_inference_done", index, frame.buf_pts)
                            startup.mark("first_inference_done")
                        try:
                            frames = frames.next
                        except StopIteration:
                            break
                except Exception:
                    startup.mark("batch_observer_unavailable")
                if startup.complete(stage) and (stage != "first_pgie_output" or startup.complete("first_inference_done")):
                    return Gst.PadProbeReturn.REMOVE
                return Gst.PadProbeReturn.OK
            return Gst.PadProbeReturn.REMOVE

        pad.add_probe(Gst.PadProbeType.BUFFER, observe)

    def startup_report(final=False):
        if startup is None:
            return
        startup.drain()
        summary = startup.summary()
        summary.update(final=final)
        print("startup_summary " + json.dumps(summary, separators=(",", ":")), flush=True)

    def add_timing_probe(pad, stage, source):
        if not args.diagnostics:
            return
        counter = FrameTiming()
        timings[(stage, source)] = counter
        caps_reported = False

        def observe(_pad, info):
            nonlocal caps_reported
            buffer = info.get_buffer()
            if buffer is not None:
                if stage == "decoded" and not caps_reported:
                    caps = _pad.get_current_caps()
                    if caps is not None and caps.get_size() > 0:
                        structure = caps.get_structure(0)
                        ok, numerator, denominator = structure.get_fraction("framerate")
                        print(f"source={cameras_by_source[source].source_id} camera_id={cameras_by_source[source].camera_id} "
                              f"negotiated_source_fps={numerator / denominator if ok and denominator else 'unknown'}",
                              flush=True)
                        caps_reported = True
                counter.observe(None if buffer.pts == Gst.CLOCK_TIME_NONE else int(buffer.pts))
            return Gst.PadProbeReturn.OK

        pad.add_probe(Gst.PadProbeType.BUFFER, observe)

    def stop(reason, error=False, source="all"):
        nonlocal failed
        if source != "all":
            manager.fail_source(int(source), reason)
            return GLib.SOURCE_CONTINUE
        if error and failed is None:
            failed = reason
            for slot in cameras_by_source:
                manager.fail_source(slot, reason)
            print(f"Shared pipeline ERROR code={reason}; details hidden", flush=True)
        pipeline_stop.set()
        return GLib.SOURCE_REMOVE

    def fail(_reason, source="all"):
        stop("rtsp_error" if _reason == "rtsp_error" else "pipeline_error", True, source)

    def source_failed(reason, index):
        stage, code = source_failure_detail(reason)
        manager.record_error_reason(index, code)
        if args.diagnostics:
            camera = cameras_by_source[index]
            print("source_failure " + json.dumps({"camera_id": camera.camera_id, "source_id": camera.source_id,
                  "failure_stage": stage, "reason": code}, separators=(",", ":")), flush=True)
        fail("rtsp_error" if code == "rtsp_error" else "pipeline_error", str(index))

    def report(final=False):
        nonlocal previous_time
        now = time.monotonic()
        elapsed = max(now - previous_time, 1e-9)
        with counts_lock:
            snapshot = counts.copy()
            person_snapshot = persons.copy()
            peak_snapshot = persons_max.copy()
            id_snapshot = [ids.copy() for ids in track_ids]
            samples = dict(metadata_samples)
            metadata_samples.clear()
            persons_max[:] = [0] * len(args.input)
        for index, total in enumerate(snapshot):
            latest = person_snapshot[index] if person_snapshot[index] is not None else "NA"
            fps = (total - previous_counts[index]) / elapsed
            print(
                f"{'final' if final else 'stats'} source={cameras_by_source[index].source_id} frames={total} "
                f"camera_id={cameras_by_source[index].camera_id} fps={fps:.2f} persons={latest} persons_max={peak_snapshot[index]} "
                f"track_ids={id_snapshot[index]}", flush=True,
            )
        if args.diagnostics:
            for index, diagnostics in inference_stats.items():
                print(f"inference source={cameras_by_source[index].source_id} camera_id={cameras_by_source[index].camera_id} "
                      f"{json.dumps(diagnostics.snapshot(), separators=(',', ':'))}", flush=True)
                sample = samples.get(index)
                if sample is not None:
                    print("metadata " + json.dumps(sample.to_dict(), ensure_ascii=False,
                                                   separators=(',', ':'), allow_nan=False), flush=True)
        previous_counts[:] = snapshot
        previous_time = now
        for (stage, source), counter in timings.items():
            print(
                f"timing pipeline_source={source} stage={stage} "
                f"{json.dumps(counter.snapshot(), separators=(',', ':'))}", flush=True,
            )
        return GLib.SOURCE_CONTINUE

    def on_batch(pad, info):
        index = "all"
        try:
            buffer = info.get_buffer()
            if buffer is None or pipeline_stop.is_set():
                return Gst.PadProbeReturn.OK
            batch = pyds.gst_buffer_get_nvds_batch_meta(hash(buffer))
            if batch is None:
                raise RuntimeError("Tracker output has no DeepStream batch metadata")
            frame_list = batch.frame_meta_list
            while frame_list is not None:
                frame = pyds.NvDsFrameMeta.cast(frame_list.data)
                camera = camera_for_frame(frame)
                index = int(frame.pad_index)
                pts = int(frame.buf_pts)
                generation = manager.frame_generation(index, pts)
                if generation is None:
                    try:
                        frame_list = frame_list.next
                    except StopIteration:
                        break
                    continue
                timestamp = utc_timestamp()
                records = []
                person_count = 0
                current_ids = set()
                object_list = frame.obj_meta_list
                while object_list is not None:
                    obj = pyds.NvDsObjectMeta.cast(object_list.data)
                    # Save the next node before removing any non-person metadata.
                    try:
                        next_object = object_list.next
                    except StopIteration:
                        next_object = None
                    if obj.unique_component_id == PGIE_ID and obj.class_id == person_class_id:
                        person_count += 1
                        records.append(person_from_object(camera.camera_id, timestamp, obj))
                        obj.rect_params.border_width = 3
                        obj.rect_params.border_color.set(0.0, 1.0, 0.0, 1.0)
                        obj.rect_params.has_bg_color = 0
                        track_id = int(obj.object_id)
                        text = obj.text_params
                        if track_id != UNTRACKED_OBJECT_ID:
                            current_ids.add(track_id)
                            text.display_text = f"Person {track_id}"
                        else:
                            text.display_text = "Person pending"
                        text.x_offset = max(0, int(obj.rect_params.left))
                        text.y_offset = max(0, int(obj.rect_params.top) - 22)
                        text.font_params.font_name = "Arial"
                        text.font_params.font_size = 18
                        text.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)
                        text.set_bg_clr = 1
                        text.text_bg_clr.set(0.0, 0.0, 0.0, 0.7)
                    else:
                        pyds.nvds_remove_obj_meta_from_frame(frame, obj)
                    object_list = next_object
                pts = int(frame.buf_pts)
                detached = FrameMetadata(
                    camera.source_id, camera.camera_id, int(frame.frame_num), timestamp,
                    None if pts == Gst.CLOCK_TIME_NONE else pts,
                    bool(frame.bInferDone), tuple(records), generation=generation, pipeline_source_id=int(frame.source_id),
                )
                manager.publish(index, pts, lambda: metadata_store.put(detached), generation=generation)
                with counts_lock:
                    if args.diagnostics and records:
                        metadata_samples[index] = FrameMetadata(
                            camera.source_id, camera.camera_id, detached.frame_number, timestamp,
                            detached.pts_ns, detached.inference_done, tuple(records[:5]), generation=generation, pipeline_source_id=int(frame.source_id),
                        )
                    counts[index] += 1
                    persons[index] = person_count
                    persons_max[index] = max(persons_max[index], person_count)
                    track_ids[index] = sorted(current_ids)
                try:
                    frame_list = frame_list.next
                except StopIteration:
                    break
        except Exception:
            fail("Metadata/OSD preparation failed; details hidden", str(index))
        return Gst.PadProbeReturn.OK

    def message_scope(message):
        element = message.src
        while element is not None:
            name = element.get_name()
            for index in range(len(args.input)):
                if name in (
                    f"source-bin-{index}",
                    f"mjpeg-source-{index}",
                ):
                    return str(index), "pipeline_error" if name == f"mjpeg-source-{index}" else "rtsp_error"
            element = element.get_parent()
        return "all", "pipeline_error"

    def on_message(bus, message):
        source, error_code = message_scope(message)
        if source != "all" and error_code == "rtsp_error" and message.type in (Gst.MessageType.ERROR, Gst.MessageType.WARNING):
            detail = classify_bus_error(message, warning=message.type == Gst.MessageType.WARNING)
            changed = manager.record_error_reason(int(source), detail["reason"])
            if detail["reason"] in ("source_caps_error", "source_link_error", "decoder_error"):
                error_code = "pipeline_error"
            if args.diagnostics and changed:
                camera = cameras_by_source[int(source)]
                print("source_rtsp " + json.dumps({"camera_id": camera.camera_id, "source_id": camera.source_id,
                      **detail}, separators=(",", ":")), flush=True)
        if message.type == Gst.MessageType.ERROR:
            stop(error_code, True, source)
        elif message.type == Gst.MessageType.WARNING:
            if args.diagnostics:
                print(f"pipeline_source={source}: GStreamer WARNING; details hidden", flush=True)
        elif message.type == Gst.MessageType.EOS:
            # Source EOS is intercepted before the mux. Aggregate EOS is fatal.
            stop("source_eos", True, source)
        elif message.type == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            if structure is not None and structure.get_name() == "GstRTSPSrcTimeout":
                if source != "all":
                    manager.record_error_reason(int(source), "rtsp_timeout")
                    stop("rtsp_timeout", True, source)
        elif message.type == Gst.MessageType.STATE_CHANGED and message.src == pipeline:
            _old, current, _pending = message.parse_state_changed()
            if current == Gst.State.PLAYING:
                if startup is not None:
                    startup.mark("pipeline_playing_bus_observed")
                print("DeepStream shared pipeline state=PLAYING", flush=True)

    def observe_decoded(_pad, info, index):
        buffer = info.get_buffer()
        if buffer is not None:
            pts = None if buffer.pts == Gst.CLOCK_TIME_NONE else int(buffer.pts)
            if not manager.observe_decoded(index, pts):
                return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK

    def source_event(_pad, info, index):
        event = info.get_event()
        if event is not None and event.type == Gst.EventType.EOS:
            manager.fail_source(index, "source_eos")
            # A single (or all) offline source must not terminate the shared chain.
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK

    try:
        pipeline = Gst.Pipeline.new("preview-detection")
        if pipeline is None:
            raise RuntimeError("Could not create pipeline")
        mux = make_element("nvstreammux", "stream-muxer")
        mux.set_property("batch-size", manager.batch_size)
        mux.set_property("live-source", bool(args.mux_live_source))
        mux.set_property("width", 1920)
        mux.set_property("height", 1080)
        mux.set_property("batched-push-timeout", 40000)
        mux.set_property("gpu-id", 0)
        mux.set_property("nvbuf-memory-type", 0)  # Existing platform-default NVMM memory.
        mux.set_property("sync-inputs", False)  # Partial batches must not wait for offline cameras.
        add_timing_probe(mux.get_static_pad("src"), "mux-batch", "all")
        pgie = make_element("nvinfer", "primary-inference")
        configure_pgie(pgie, pgie_config, manager.batch_size)
        tracker = make_tracker()
        demux = make_element("nvstreamdemux", "stream-demuxer")
        shared = (mux, pgie, tracker, demux)
        for element in shared:
            pipeline.add(element)
        for left, right in zip(shared, shared[1:]):
            if not left.link(right):
                raise RuntimeError(f"Could not link {left.get_name()} -> {right.get_name()}")
        if args.diagnostics:
            log_settings(pgie_config, person_class_id, TRACKER_CONFIG)
            pgie.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_inference)
        startup_probe(mux.get_static_pad("src"), "first_mux_output", batched=True)
        startup_probe(pgie.get_static_pad("sink"), "first_pgie_input", batched=True)
        startup_probe(pgie.get_static_pad("src"), "first_pgie_output", batched=True)
        startup_probe(tracker.get_static_pad("sink"), "first_tracker_input", batched=True)
        startup_probe(tracker.get_static_pad("src"), "first_tracker_output", batched=True)
        tracker.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_batch)

        for index, uri in enumerate(args.input):
            setup_stage = f"source-{index}"
            if startup is not None:
                startup.mark("source_create_start", index)
            observer_options = {"observer": source_observer(startup, index, Gst)} if startup is not None else {}
            source_bin = create_preview_source_bin(
                index, uri, args.rtsp_latency, lambda reason, i=index: source_failed(reason, i),
                args.rtsp_drop_on_latency, max(1, int(args.camera_offline_after)), **observer_options,
            )
            if startup is not None:
                startup.mark("source_created", index)
            startup_probe(source_bin.get_static_pad("src"), "first_source_output", index)
            source_bin.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, observe_decoded, index)
            source_bin.get_static_pad("src").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, source_event, index)
            add_timing_probe(source_bin.get_static_pad("src"), "decoded", index)
            pipeline.add(source_bin)
            mux_pad = mux.request_pad_simple(f"sink_{index}")
            if mux_pad is None:
                raise RuntimeError(f"Could not request nvstreammux sink_{index}")
            requested_pads.append((mux, mux_pad))
            startup_probe(mux_pad, "first_mux_input", index)
            if source_bin.get_static_pad("src").link(mux_pad) != Gst.PadLinkReturn.OK:
                raise RuntimeError(f"Could not link source bin directly to mux sink_{index}")

            setup_stage = f"jpeg-branch-{index}"
            output_bin = build_output(
                index, args, CameraFrameSink(stores[cameras_by_source[index].source_id], manager, index),
                lambda reason, i=index: fail("pipeline_error", str(i)),
            )
            startup_probe(output_bin.get_by_name(f"jpeg-encoder-{index}").get_static_pad("src"),
                          "first_jpeg", index)
            startup_probe(output_bin.get_by_name(f"jpeg-sink-{index}").get_static_pad("sink"),
                          "first_appsink_buffer", index)
            add_timing_probe(
                output_bin.get_by_name(f"jpeg-sink-{index}").get_static_pad("sink"),
                "encoded-mjpeg", index,
            )
            pipeline.add(output_bin)
            # nvstreamdemux src_%u is a request pad, requested while in NULL state.
            demux_pad = demux.request_pad_simple(f"src_{index}")
            if demux_pad is None:
                raise RuntimeError(f"Could not request nvstreamdemux src_{index}")
            requested_pads.append((demux, demux_pad))
            startup_probe(demux_pad, "first_demux_output", index)
            add_timing_probe(demux_pad, "demux", index)
            if demux_pad.link(output_bin.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
                raise RuntimeError("Could not link demux source pad to preview output")
            print(f"source={cameras_by_source[index].source_id} camera_id={cameras_by_source[index].camera_id}: "
                  f"output=mjpeg url=/mjpeg/source{cameras_by_source[index].source_id}", flush=True)
        if startup is not None:
            startup.mark("all_source_branches_built")
        bus = pipeline.get_bus()
        if startup is not None:
            bus.set_sync_handler(bus_observer(startup, Gst,
                                 {"pipeline": pipeline, "pgie": pgie, "tracker": tracker}, message_scope), None)
        previous_time = time.monotonic()
        print(f"DeepStream shared pipeline sources={manager.batch_size} "
              f"streammux batch-size={mux.get_property('batch-size')} "
              f"pgie batch-size={pgie.get_property('batch-size')} tracker=NvDCF "
              f"demux branches={manager.batch_size} live-source={args.mux_live_source}", flush=True)
        setup_stage = "playing"
        startup_report()
        if startup is not None:
            startup.mark("playing_requested")
            startup.drain()
        state_result = pipeline.set_state(Gst.State.PLAYING)
        if startup is not None:
            startup.mark("playing_call_returned")
        if state_result == Gst.StateChangeReturn.FAILURE:
            stop("pipeline_setup_failed", True)
        else:
            manager.arm_watchdogs()
            previous_time = time.monotonic()
            last_report = time.monotonic()
            message_types = (Gst.MessageType.ERROR | Gst.MessageType.WARNING | Gst.MessageType.EOS
                             | Gst.MessageType.ELEMENT | Gst.MessageType.STATE_CHANGED)
            while not shutdown.is_set() and not pipeline_stop.is_set():
                message = bus.timed_pop_filtered(100 * Gst.MSECOND, message_types)
                if message is not None:
                    on_message(bus, message)
                manager.tick()
                if startup is not None:
                    for slot, camera in cameras_by_source.items():
                        if manager.runtimes[camera.source_id].snapshot().state == "online":
                            startup.mark("online", slot)
                    startup.drain()
                if args.diagnostics and time.monotonic() - last_report >= 5:
                    report()
                    startup_report()
                    for camera in args.cameras:
                        print("camera_status " + json.dumps({"source_id": camera.source_id,
                              **manager.runtimes[camera.source_id].snapshot().to_dict()},
                              separators=(",", ":")), flush=True)
                    last_report = time.monotonic()
    except Exception as error:
        print(f"Shared pipeline setup ERROR stage={setup_stage} type={type(error).__name__}; details hidden",
              flush=True)
        if args.diagnostics:
            log_safe_traceback(error)
        stop("pipeline_setup_failed", True)
    finally:
        if startup is not None:
            startup.stop_gpu_sampling()
        startup_report(final=True)
        if bus is not None and startup is not None:
            bus.set_sync_handler(None, None)
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        for owner, pad in reversed(requested_pads):
            owner.release_request_pad(pad)
    return failed


class CameraFrameSink:
    """Keep HTTP stores alive; reject JPEG from a previous observation epoch."""
    def __init__(self, store, manager, slot):
        self.store, self.manager, self.slot = store, manager, slot

    def put_frame(self, jpeg, pts):
        self.manager.publish(self.slot, int(pts), lambda: self.store.put(jpeg), output=True)


def run(args, metadata_store=None, runtime_statuses=None):
    """One HTTP server, one shared GPU pipeline, camera-local health."""
    startup = None
    if args.diagnostics:
        origin, origin_name = startup_origin(PROCESS_ENTRY)
        startup = StartupTiming(args.cameras, origin, origin_name)
        args.startup_timing = startup
        startup.mark("python_entry", at=PROCESS_ENTRY)
        startup.mark("run_entered")
        print("startup_config " + json.dumps({"origin": origin_name, "rtsp_transport": "TCP",
              "rtsp_latency_ms": args.rtsp_latency, "drop_on_latency": args.rtsp_drop_on_latency,
              "reconnect_interval_sec": max(1, int(args.camera_offline_after)), "reconnect_attempts": -1,
              "sources": len(args.cameras), "mux_batch_size": len(args.cameras),
              "mux_live_source": args.mux_live_source, "mux_sync_inputs": False,
              "mux_timeout_us": 40000}, separators=(",", ":")), flush=True)
    metadata_store = MetadataStore() if metadata_store is None else metadata_store
    runtime_statuses = {} if runtime_statuses is None else runtime_statuses
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    os.environ["GST_REGISTRY"] = str(cache / "gstreamer-registry.bin")
    Gst.init(None)
    Gst.debug_set_active(False)
    required = ("nvurisrcbin", "uridecodebin", "rtspsrc", "nvv4l2decoder", "nvvideoconvert", "queue",
                "capsfilter", "nvjpegenc", "appsink", "nvstreammux", "nvinfer", "nvtracker",
                "nvdsosd", "nvstreamdemux")
    missing = [name for name in required if Gst.ElementFactory.find(name) is None]
    if missing:
        print(f"Preview setup ERROR: missing GStreamer elements: {', '.join(missing)}", flush=True)
        return 1
    # Config and GObject override agree; engines remain cached by batch size.
    if startup is not None:
        startup.mark("pgie_config_start")
    pgie_config, person_class_id = prepare_pgie_config(str(cache), len(args.cameras))
    if startup is not None:
        startup.mark("pgie_config_ready")
    for camera in args.cameras:
        runtime_statuses[camera.source_id] = CameraRuntime(
            camera.camera_id, args.camera_degraded_after, args.camera_offline_after,
            args.camera_connect_timeout, args.camera_min_fps)
    frontend_files = FrontendFiles(os.environ.get('FRONTEND_DIST_DIR', str(ROOT / 'frontend')))
    backend_api_proxy = BackendAPIProxy(os.environ.get('PREVIEW_BACKEND_URL', 'http://127.0.0.1:8000'))
    server = ThreadingHTTPServer(("0.0.0.0", args.port), PreviewHandler)
    server.frontend_files = frontend_files
    server.backend_api_proxy = backend_api_proxy
    stores = {camera.source_id: FrameStore() for camera in args.cameras}
    if startup is not None:
        for slot, camera in enumerate(args.cameras):
            stores[camera.source_id].startup_observer = lambda stage, i=slot: startup.mark(stage, i)
    server.mjpeg_streams = {f"/mjpeg/source{index}": store for index, store in stores.items()}

    runtime_session = uuid.uuid4().hex  # Track identities must not collide after process restart.

    def stream_snapshot():
        streams = []
        for camera in args.cameras:
            status = runtime_statuses[camera.source_id].snapshot()
            streams.append({**camera.public_info(), "id": camera.source_id, "format": "mjpeg",
                            "status": "connected" if status.state in ("online", "degraded") else "starting",
                            "url": f"/mjpeg/source{camera.source_id}", "runtime": status.to_dict(),
                            "runtime_session": runtime_session})
        return streams

    server.stream_snapshot = stream_snapshot

    def metadata_snapshot():
        frames = metadata_store.snapshot()
        # A concurrent retry may leave an old immutable snapshot; reject it.
        current = []
        for source_id, frame in frames.items():
            status = runtime_statuses[source_id].snapshot()
            if status.generation == frame.generation and status.state in ("online", "degraded"):
                current.append(frame.to_dict())
        return {"runtime_session": runtime_session, "frames": current}

    server.metadata_snapshot = metadata_snapshot
    shutdown = threading.Event()
    manager = CameraRuntimeManager(args.cameras, runtime_statuses, stores, metadata_store)
    manager.begin()
    result = 1
    old_handlers = {}
    http_thread = threading.Thread(target=server.serve_forever, daemon=True)

    def on_signal(_signum, _frame):
        shutdown.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, on_signal)
        http_thread.start()
        print(f"Preview HTTP listening on 0.0.0.0:{args.port}; shared camera pipeline", flush=True)
        error = run_shared_pipeline(args, metadata_store, stores, manager, shutdown,
                                    pgie_config, person_class_id)
        result = int(error is not None)
    finally:
        shutdown.set()
        for store in stores.values():
            store.close()
        if http_thread.is_alive():
            server.shutdown()
            http_thread.join(timeout=2)
        server.server_close()
        for signum, previous in old_handlers.items():
            signal.signal(signum, previous)
        print("Preview stopped", flush=True)
    return result


if __name__ == "__main__":
    try:
        sys.exit(run(parse_args()))
    except Exception:
        print("source=all: Preview setup ERROR", file=sys.stderr)
        sys.exit(1)
