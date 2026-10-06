#!/usr/bin/env python3
"""Batched DeepStream person detection/tracking with MJPEG preview."""

import argparse
import functools
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

gst_debug = os.environ.get("GST_DEBUG", "0")
sys.dont_write_bytecode = True

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstRtsp", "1.0")
from gi.repository import GLib, Gst, GstRtsp
import pyds
from app import PGIE_ID, create_source_bin, prepare_pgie_config, resolve_inputs
from preview_timing import FrameTiming
from preview_mjpeg import FrameStore, serve_mjpeg

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
                        help="RTSP input; repeat for each camera (overrides environment inputs)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--jpeg-quality", type=int, default=80)
    parser.add_argument(
        "--rtsp-latency", type=int, default=1000, metavar="MS",
        help="RTSP jitter-buffer latency in milliseconds (default: 1000)",
    )
    parser.add_argument(
        "--rtsp-drop-on-latency", action="store_true",
        help="Discard RTSP jitter-buffer frames beyond latency; default: disabled",
    )
    parser.add_argument(
        "--mux-live-source", type=int, choices=(0, 1), default=1,
        help="Legacy mux timestamp mode; 0 is an experiment for matching-FPS sources",
    )
    parser.add_argument(
        "--diagnostics", action="store_true",
        help="Log per-stage arrival gaps and PTS deltas every five seconds",
    )
    args = parser.parse_args()
    args.input = resolve_inputs(parser, args.input)
    if not 1 <= args.port <= 65535:
        parser.error("port")
    if not 1 <= args.jpeg_quality <= 100:
        parser.error("JPEG quality must be between 1 and 100")
    if not 0 <= args.rtsp_latency <= (1 << 32) - 1:
        parser.error("RTSP latency must be a non-negative guint in milliseconds")
    for index, uri in enumerate(args.input):
        try:
            parsed = urlsplit(uri)
            valid = parsed.scheme.lower() == "rtsp" and bool(parsed.hostname)
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            parser.exit(2, f"source={index}: invalid RTSP URI; value hidden.\n")
    return args


class PreviewHandler(SimpleHTTPRequestHandler):
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
        if path in self.server.mjpeg_streams:
            serve_mjpeg(self, self.server.mjpeg_streams[path])
            return
        if path == "/streams.json":
            body = json.dumps(self.server.streams).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def send_head(self):
        # Serve only the preview assets; old generated files are not public.
        if urlsplit(self.path).path not in ("/", "/index.html", "/preview.js"):
            self.send_error(404)
            return None
        return super().send_head()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def list_directory(self, path):
        self.send_error(404)
        return None

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


def create_preview_source_bin(index, uri, latency, fail, drop_on_latency=False):
    """Reuse app.py's NVMM source, with preview-specific transport/buffering."""
    source_bin = create_source_bin(index, uri, fail)
    decoder = source_bin.get_by_name(f"uri-decode-bin-{index}")
    if decoder is None:
        raise RuntimeError(f"source={index}: uridecodebin not found in source bin")

    def on_source_setup(decodebin, source):
        factory = source.get_factory()
        if factory is None or factory.get_name() != "rtspsrc":
            fail(f"source={index}: expected rtspsrc for RTSP input")
            return
        try:
            source.set_property("protocols", GstRtsp.RTSPLowerTrans.TCP)
            source.set_property("latency", latency)
            source.set_property("drop-on-latency", drop_on_latency)
            # Avoid SETUP/decoding of the camera's ONVIF metadata and audio.
            source.connect(
                "select-stream", lambda _source, _number, caps:
                caps.get_size() > 0 and caps.get_structure(0).get_value("media") == "video",
            )
            print(
                f"source={index}: RTSP transport=TCP latency={source.get_property('latency')}ms "
                f"drop-on-latency={source.get_property('drop-on-latency')}", flush=True,
            )
        except Exception:
            fail(f"source={index}: RTSP setup failed:\n{traceback.format_exc()}")

    # Connect after app.py's default callback so these properties override its
    # 200ms/drop-on-latency settings, before the source starts RTSP negotiation.
    decoder.connect("source-setup", on_source_setup)

    def on_child_added(proxy, child, name):
        if name.startswith("decodebin"):
            child.connect("child-added", on_child_added)
        factory = child.get_factory() if isinstance(child, Gst.Element) else None
        if factory is not None and factory.get_name() == "nvv4l2decoder":
            if child.find_property("num-extra-surfaces") is not None:
                child.set_property("num-extra-surfaces", 4)

    decoder.connect("child-added", on_child_added)
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
        sink.set_property("sync", True)
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
                    store.put(bytes(info.data))
                finally:
                    buffer.unmap(info)
                return Gst.FlowReturn.OK
            except Exception:
                fail(f"JPEG output failed:\n{traceback.format_exc()}")
                return Gst.FlowReturn.ERROR

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


def run(args):
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    os.environ["GST_REGISTRY"] = str(cache / "gstreamer-registry.bin")
    Gst.init(None)
    Gst.debug_set_active(True)
    required = (
        "uridecodebin", "rtspsrc", "nvv4l2decoder", "nvvideoconvert",
        "queue", "capsfilter", "nvjpegenc", "appsink",
        "nvstreammux", "nvinfer", "nvtracker", "nvdsosd", "nvstreamdemux",
    )
    missing = [name for name in required if Gst.ElementFactory.find(name) is None]
    if missing:
        print(f"Preview setup ERROR: missing GStreamer elements: {', '.join(missing)}", flush=True)
        return 1
    if not (WEB_ROOT / "index.html").is_file():
        print("Preview setup ERROR: web/index.html is missing", flush=True)
        return 1
    pgie_config, person_class_id = prepare_pgie_config(str(cache), len(args.input))
    handler = functools.partial(PreviewHandler, directory=str(WEB_ROOT))
    # Bind before starting the pipeline, so a second process on this port fails safely.
    server = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    stores = [FrameStore() for _ in args.input]
    server.mjpeg_streams = {f"/mjpeg/source{index}": store for index, store in enumerate(stores)}
    server.streams = [
        {"id": index, "format": "mjpeg", "status": "starting", "url": f"/mjpeg/source{index}"}
        for index in range(len(args.input))
    ]
    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    loop = GLib.MainLoop()
    pipeline = None
    requested_pads = []
    bus = None
    bus_handler = None
    timer_id = None
    old_handlers = {}
    failed = False
    stopping = False
    scope = "all"
    counts = [0] * len(args.input)
    persons = [None] * len(args.input)
    persons_max = [0] * len(args.input)
    track_ids = [[] for _ in args.input]
    previous_counts = [0] * len(args.input)
    counts_lock = threading.Lock()
    previous_time = time.monotonic()
    timings = {}

    def add_timing_probe(pad, stage, source):
        if not args.diagnostics:
            return
        counter = FrameTiming()
        timings[(stage, source)] = counter

        def observe(_pad, info):
            buffer = info.get_buffer()
            if buffer is not None:
                counter.observe(None if buffer.pts == Gst.CLOCK_TIME_NONE else int(buffer.pts))
            return Gst.PadProbeReturn.OK

        pad.add_probe(Gst.PadProbeType.BUFFER, observe)

    def stop(reason, error=False, source="all"):
        nonlocal failed, stopping
        failed |= error
        if not stopping:
            stopping = True
            for store in stores:
                store.close()
            sources = ",".join(str(item["id"]) for item in server.streams)
            label = f"all({sources})" if source == "all" else source
            print(f"source={label}: {'ERROR' if error else 'STOP'} {reason}", flush=True)
            for stream in server.streams:
                stream["status"] = "error" if error else "eos"
            loop.quit()
        return GLib.SOURCE_REMOVE

    def fail(reason, source="all"):
        GLib.idle_add(stop, reason, True, source)

    def report(final=False):
        nonlocal previous_time
        now = time.monotonic()
        elapsed = max(now - previous_time, 1e-9)
        with counts_lock:
            snapshot = counts.copy()
            person_snapshot = persons.copy()
            peak_snapshot = persons_max.copy()
            id_snapshot = [ids.copy() for ids in track_ids]
            persons_max[:] = [0] * len(args.input)
        for index, total in enumerate(snapshot):
            latest = person_snapshot[index] if person_snapshot[index] is not None else "NA"
            fps = (total - previous_counts[index]) / elapsed
            print(
                f"{'final' if final else 'stats'} source={index} frames={total} "
                f"fps={fps:.2f} persons={latest} persons_max={peak_snapshot[index]} "
                f"track_ids={id_snapshot[index]}", flush=True,
            )
        previous_counts[:] = snapshot
        previous_time = now
        for (stage, source), counter in timings.items():
            print(
                f"timing source={source} stage={stage} "
                f"{json.dumps(counter.snapshot(), separators=(',', ':'))}", flush=True,
            )
        return GLib.SOURCE_CONTINUE

    def on_batch(pad, info):
        index = "all"
        try:
            buffer = info.get_buffer()
            if buffer is None:
                return Gst.PadProbeReturn.OK
            batch = pyds.gst_buffer_get_nvds_batch_meta(hash(buffer))
            if batch is None:
                raise RuntimeError("Tracker output has no DeepStream batch metadata")
            frame_list = batch.frame_meta_list
            while frame_list is not None:
                frame = pyds.NvDsFrameMeta.cast(frame_list.data)
                index = int(frame.pad_index)
                if not 0 <= index < len(counts):
                    raise RuntimeError(f"Unexpected source index: {index}")
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
                with counts_lock:
                    counts[index] += 1
                    persons[index] = person_count
                    persons_max[index] = max(persons_max[index], person_count)
                    track_ids[index] = sorted(current_ids)
                if not stopping:
                    server.streams[index]["status"] = "connected"
                try:
                    frame_list = frame_list.next
                except StopIteration:
                    break
        except Exception:
            fail(f"Metadata/OSD preparation failed:\n{traceback.format_exc()}", str(index))
        return Gst.PadProbeReturn.OK

    def message_source(message):
        element = message.src
        while element is not None:
            name = element.get_name()
            for index in range(len(args.input)):
                if name in (
                    f"source-bin-{index}", f"mjpeg-source-{index}",
                    f"input-queue-{index}", f"input-convert-{index}", f"input-caps-{index}",
                ):
                    return str(index)
            element = element.get_parent()
        return "all"

    def on_message(bus, message):
        source = message_source(message)
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            stop(
                f"GStreamer element={message.src.get_name()} domain={error.domain} "
                f"code={error.code}: {error.message}\ndebug: {debug}", True, source,
            )
        elif message.type == Gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            print(
                f"source={source}: GStreamer WARNING element={message.src.get_name()}: "
                f"{warning.message}\ndebug: {debug}", flush=True,
            )
        elif message.type == Gst.MessageType.EOS:
            stop("GStreamer EOS", source=source)

    def on_signal(signum, frame):
        GLib.idle_add(stop, "Stop requested")

    try:
        pipeline = Gst.Pipeline.new("preview-detection")
        if pipeline is None:
            raise RuntimeError("Could not create pipeline")
        mux = make_element("nvstreammux", "stream-muxer")
        mux.set_property("batch-size", len(args.input))
        mux.set_property("live-source", bool(args.mux_live_source))
        mux.set_property("width", 1920)
        mux.set_property("height", 1080)
        mux.set_property("batched-push-timeout", 40000)
        print(f"Mux live-source={args.mux_live_source} batch-size={len(args.input)}", flush=True)
        add_timing_probe(mux.get_static_pad("src"), "mux-batch", "all")
        # Normalize every source to the mux's configured format and dimensions.
        mux_input_caps = Gst.Caps.from_string(
            "video/x-raw(memory:NVMM),format=NV12,"
            f"width={mux.get_property('width')},height={mux.get_property('height')}"
        )
        pgie = make_element("nvinfer", "primary-inference")
        pgie.set_property("config-file-path", pgie_config)
        pgie.set_property("batch-size", len(args.input))
        tracker = make_tracker()
        demux = make_element("nvstreamdemux", "stream-demuxer")
        shared = (mux, pgie, tracker, demux)
        for element in shared:
            pipeline.add(element)
        for left, right in zip(shared, shared[1:]):
            if not left.link(right):
                raise RuntimeError(f"Could not link {left.get_name()} -> {right.get_name()}")
        tracker.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_batch)

        for index, uri in enumerate(args.input):
            scope = str(index)
            source_bin = create_preview_source_bin(
                index, uri, args.rtsp_latency, lambda reason, i=index: fail(reason, str(i)),
                args.rtsp_drop_on_latency,
            )
            add_timing_probe(source_bin.get_static_pad("src"), "decoded", index)
            input_queue = make_element("queue", f"input-queue-{index}")
            input_convert = make_element("nvvideoconvert", f"input-convert-{index}")
            input_filter = make_element("capsfilter", f"input-caps-{index}")
            input_filter.set_property("caps", mux_input_caps)
            input_chain = (source_bin, input_queue, input_convert, input_filter)
            for element in input_chain:
                pipeline.add(element)
            for left, right in zip(input_chain, input_chain[1:]):
                if not left.link(right):
                    raise RuntimeError(f"Could not link {left.get_name()} -> {right.get_name()}")
            mux_pad = mux.request_pad_simple(f"sink_{index}")
            if mux_pad is None:
                raise RuntimeError(f"Could not request nvstreammux sink_{index}")
            requested_pads.append((mux, mux_pad))
            if input_filter.get_static_pad("src").link(mux_pad) != Gst.PadLinkReturn.OK:
                raise RuntimeError(f"Could not link normalized input caps to mux sink_{index}")

            output_bin = build_output(
                index, args, stores[index], lambda reason, i=index: fail(reason, str(i)),
            )
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
            add_timing_probe(demux_pad, "demux", index)
            if demux_pad.link(output_bin.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
                raise RuntimeError("Could not link demux source pad to preview output")
            print(f"source={index}: output=mjpeg url=/mjpeg/source{index}", flush=True)
        scope = "all"
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus_handler = bus.connect("message", on_message)
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, on_signal)
        timer_id = GLib.timeout_add_seconds(5, report)
        previous_time = time.monotonic()
        http_thread.start()
        print(f"Preview HTTP listening on 0.0.0.0:{args.port}; {len(args.input)} sources", flush=True)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            # State change can fail before the main loop dispatches its ERROR.
            error_message = bus.pop_filtered(Gst.MessageType.ERROR)
            if error_message is not None:
                on_message(bus, error_message)
            else:
                stop("Failed to enter PLAYING; no GStreamer ERROR message available", True)
        else:
            loop.run()
    except KeyboardInterrupt:
        stop("Stop requested")
    except Exception:
        stop(f"Pipeline setup/runtime failed:\n{traceback.format_exc()}", True, scope)
    finally:
        for store in stores:
            store.close()
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        if timer_id is not None:
            GLib.source_remove(timer_id)
        for owner, pad in reversed(requested_pads):
            owner.release_request_pad(pad)
        if bus is not None and bus_handler is not None:
            bus.disconnect(bus_handler)
            bus.remove_signal_watch()
        if http_thread.is_alive():
            server.shutdown()
            http_thread.join(timeout=2)
        server.server_close()
        for signum, previous in old_handlers.items():
            signal.signal(signum, previous)
        report(final=True)
        print("Preview stopped", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(run(parse_args()))
    except Exception:
        print("source=all: Preview setup ERROR", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
