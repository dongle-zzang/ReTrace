"""On-demand clean JPEG handoff. No frames are retained between requests."""
from contextlib import suppress
import threading
from datetime import datetime, timezone


class CaptureUnavailable(Exception):
    pass


class CaptureBusy(Exception):
    pass


class RawCapture:
    def __init__(self):
        self.lock = threading.Lock()
        self.request_lock = threading.Lock()
        self.event = threading.Event()
        self.gate = None
        self.pending = False
        self.accepted = None
        self.result = None

    def bind(self, gate):
        self.gate = gate

    def request(self, timeout=5):
        if not self.request_lock.acquire(blocking=False):
            raise CaptureBusy()
        try:
            if self.gate is None:
                raise CaptureUnavailable()
            with self.lock:
                self.pending, self.accepted, self.result = True, None, None
                self.event.clear()
            try:
                self.gate(True)
            except Exception:
                raise CaptureUnavailable() from None
            if not self.event.wait(timeout):
                raise CaptureUnavailable()
            with self.lock:
                if self.result is None:
                    raise CaptureUnavailable()
                return self.result
        finally:
            if self.gate is not None:
                with suppress(Exception):
                    self.gate(False)
            with self.lock:
                self.pending = False
            self.request_lock.release()

    def admit(self, pts):
        with self.lock:
            if not self.pending or self.accepted is not None:
                return False
            self.accepted = pts
        self.gate(False)
        return True

    def deliver(self, jpeg, pts):
        with self.lock:
            if self.pending and self.accepted == pts:
                self.result = (jpeg, datetime.now(timezone.utc).isoformat())
                self.event.set()


def add_capture_branch(output_bin, tee, capture, index, Gst, make_element):
    """Tee NV12 before the OSD RGBA conversion, so OSD cannot mutate this surface.

    A closed valve prevents conversion/encoding except for one admitted frame/request.
    Both branches have bounded leaky queues; no additional RTSP connection/decoder.
    """
    valve = make_element('valve', f'capture-valve-{index}')
    valve.set_property('drop', True)
    if valve.find_property('drop-mode') is not None:
        valve.set_property('drop-mode', 1)  # Forward sticky caps/segment, drop buffers.
    queue = make_element('queue', f'capture-queue-{index}')
    for key, value in [('max-size-buffers', 1), ('max-size-bytes', 0), ('max-size-time', 0), ('leaky', 2)]:
        queue.set_property(key, value)
    converter = make_element('nvvideoconvert', f'capture-convert-{index}')
    caps = make_element('capsfilter', f'capture-caps-{index}')
    caps.set_property('caps', Gst.Caps.from_string('video/x-raw(memory:NVMM),format=I420'))
    encoder = make_element('nvjpegenc', f'capture-jpeg-{index}')
    encoder.set_property('quality', 95)
    sink = make_element('appsink', f'capture-sink-{index}')
    for key, value in [('emit-signals', True), ('sync', False), ('async', False), ('max-buffers', 1), ('drop', True), ('wait-on-eos', False)]:
        sink.set_property(key, value)
    capture.bind(lambda enabled: valve.set_property('drop', not enabled))

    def admit(_pad, info):
        buffer = info.get_buffer()
        return Gst.PadProbeReturn.OK if buffer is not None and capture.admit(buffer.pts) else Gst.PadProbeReturn.DROP

    def sample(appsink):
        try:
            sample = appsink.emit('pull-sample')
            if sample is not None:
                buffer = sample.get_buffer()
                mapped, data = buffer.map(Gst.MapFlags.READ)
                if mapped:
                    try:
                        capture.deliver(bytes(data.data), buffer.pts)
                    finally:
                        buffer.unmap(data)
        except Exception:
            # A request will time out; Python handoff failures must not stop shared flow.
            pass
        return Gst.FlowReturn.OK

    valve.get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, admit)
    sink.connect('new-sample', sample)
    chain = (valve, queue, converter, caps, encoder, sink)
    for element in chain:
        output_bin.add(element)
    if not tee.link(valve):
        raise RuntimeError('Could not link clean capture branch')
    for left, right in zip(chain, chain[1:]):
        if not left.link(right):
            raise RuntimeError('Could not link clean capture encoder')
