"""Shared camera H.264 encoders and isolated, bounded WebRTC senders.

Only compressed access units cross Python. GI is loaded lazily so storage and
metadata contracts can be exercised without NVIDIA/DeepStream imports.
"""
from collections import deque
from dataclasses import dataclass
import threading
import time


@dataclass(frozen=True)
class AccessUnit:
    sequence: int
    data: bytes
    keyframe: bool
    created: float


class EncodedStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._frames = deque(maxlen=8)
        self._sequence = 0
        self._closed = False

    def put(self, data, keyframe=False):
        with self._lock:
            if not self._closed:
                self._sequence += 1
                self._frames.append(AccessUnit(self._sequence, data, keyframe, time.monotonic()))

    def read_after(self, sequence):
        with self._lock:
            cutoff = time.monotonic() - 0.25
            return [frame for frame in self._frames
                    if frame.sequence > sequence and frame.created >= cutoff]

    def clear(self):
        with self._lock:
            self._frames.clear()
            # A discontinuity must invalidate every viewer's reference frames.
            self._sequence += 1

    def close(self):
        with self._lock:
            self._closed = True
            self._frames.clear()


def build_h264_output(index, args, store, fail, Gst, make_element):
    output = Gst.Bin.new(f"h264-source-{index}")
    try:
        queue = make_element("queue", f"video-queue-{index}")
        for name, value in (("max-size-buffers", 1), ("max-size-bytes", 0),
                            ("max-size-time", 0), ("leaky", 2)):
            queue.set_property(name, value)
        converter = make_element("nvvideoconvert", f"video-convert-{index}")
        caps = make_element("capsfilter", f"video-caps-{index}")
        caps.set_property("caps", Gst.Caps.from_string(
            f"video/x-raw(memory:NVMM),format=NV12,width={args.video_width},height={args.video_height}"))
        encoder = make_element("nvv4l2h264enc", f"video-encoder-{index}")
        for name, value in (("bitrate", args.video_bitrate), ("control-rate", 1),
                            ("profile", 0), ("iframeinterval", args.video_gop),
                            ("idrinterval", args.video_gop), ("insert-sps-pps", True)):
            encoder.set_property(name, value)
        for name, value in (("preset-id", 1), ("tuning-info-id", 2)):
            if encoder.find_property(name) is not None:
                encoder.set_property(name, value)
        # Baseline excludes B frames; do not enable lookahead/two-pass encoding.
        parser = make_element("h264parse", f"video-parser-{index}")
        parser.set_property("config-interval", -1)
        encoded_caps = make_element("capsfilter", f"encoded-caps-{index}")
        encoded_caps.set_property("caps", Gst.Caps.from_string(
            "video/x-h264,stream-format=byte-stream,alignment=au,profile=constrained-baseline"))
        sink = make_element("appsink", f"video-sink-{index}")
        for name, value in (("emit-signals", True), ("sync", False), ("async", False),
                            ("max-buffers", 1), ("drop", False), ("wait-on-eos", False)):
            sink.set_property(name, value)

        def sample(appsink):
            try:
                item = appsink.emit("pull-sample")
                if item is None:
                    return Gst.FlowReturn.EOS
                buffer = item.get_buffer()
                mapped, info = buffer.map(Gst.MapFlags.READ)
                if not mapped:
                    raise RuntimeError("Encoded access unit unavailable")
                try:
                    # Never drop compressed delta units silently at the appsink.
                    store.put_access_unit(bytes(info.data), buffer.pts,
                                          not buffer.has_flags(Gst.BufferFlags.DELTA_UNIT))
                finally:
                    buffer.unmap(info)
            except Exception:
                fail("H264 handoff failed; details hidden")
            return Gst.FlowReturn.OK

        sink.connect("new-sample", sample)
        chain = (queue, converter, caps, encoder, parser, encoded_caps, sink)
        for element in chain:
            output.add(element)
        for left, right in zip(chain, chain[1:]):
            if not left.link(right):
                raise RuntimeError("H264 branch link failed")
        if not output.add_pad(Gst.GhostPad.new("sink", queue.get_static_pad("sink"))):
            raise RuntimeError("H264 branch sink unavailable")
        return output
    except Exception:
        output.set_state(Gst.State.NULL)
        raise


class WebRTCPeer:
    """One camera/viewer transport. No encoder, decoder or raw video in Python."""
    def __init__(self, camera_id, peer_id, store, send, stun_server="", turn_server=""):
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("GstWebRTC", "1.0")
        gi.require_version("GstSdp", "1.0")
        from gi.repository import Gst, GstWebRTC, GstSdp
        self.Gst, self.WebRTC, self.Sdp = Gst, GstWebRTC, GstSdp
        self.camera_id, self.peer_id, self.store, self.send = camera_id, peer_id, store, send
        self.closed = threading.Event()
        self.started = time.monotonic()
        self.sequence = 0
        self.wait_keyframe = True
        self.dropped = 0
        self.pending_ice = []
        self.ice_lock = threading.Lock()
        self.remote_set = threading.Event()
        self.offered = False
        self.answered = False
        self.buffer_limit = False
        self.pipeline = Gst.Pipeline.new(None)
        self.handlers = []
        self.pad = None
        try:
            def element(factory):
                value = Gst.ElementFactory.make(factory, None)
                if value is None:
                    raise RuntimeError("WebRTC plugin missing")
                self.pipeline.add(value)
                return value
            self.source = element("appsrc")
            for name, value in (("is-live", True), ("format", Gst.Format.TIME),
                                ("do-timestamp", True), ("block", False), ("max-bytes", 1048576)):
                self.source.set_property(name, value)
            if self.source.find_property("max-buffers") is not None:
                self.source.set_property("max-buffers", 2)
                self.buffer_limit = True
            self.source.set_property("caps", Gst.Caps.from_string(
                "video/x-h264,stream-format=byte-stream,alignment=au,profile=constrained-baseline"))
            parser = element("h264parse")
            parser.set_property("config-interval", -1)
            pay = element("rtph264pay")
            pay.set_property("pt", 96)
            pay.set_property("config-interval", -1)
            if pay.find_property("aggregate-mode") is not None:
                pay.set_property("aggregate-mode", 1)  # zero-latency (GStreamer >= 1.18)
            rtp_caps = element("capsfilter")
            self.rtp_caps = rtp_caps
            rtp = Gst.Caps.from_string(
                "application/x-rtp,media=video,encoding-name=H264,clock-rate=90000,"
                "payload=96,packetization-mode=(string)1")
            rtp_caps.set_property("caps", rtp)
            self.webrtc = element("webrtcbin")
            self.webrtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
            if self.webrtc.find_property("latency") is not None:
                self.webrtc.set_property("latency", 0)
            if stun_server:
                self.webrtc.set_property("stun-server", stun_server)
            if turn_server:
                self.webrtc.set_property("turn-server", turn_server)
            self.handlers.append(self.webrtc.connect("on-ice-candidate", self._ice))
            self.handlers.append(self.webrtc.connect("on-negotiation-needed", self._negotiate))
            for left, right in ((self.source, parser), (parser, pay), (pay, rtp_caps)):
                if not left.link(right):
                    raise RuntimeError("WebRTC link failed")
            request = getattr(self.webrtc, "request_pad_simple", self.webrtc.get_request_pad)
            self.pad = request("sink_%u")
            if self.pad is None or rtp_caps.get_static_pad("src").link(self.pad) != Gst.PadLinkReturn.OK:
                raise RuntimeError("WebRTC RTP pad unavailable")
            transceiver = self.webrtc.emit("get-transceiver", 0)
            if transceiver.find_property("direction") is not None:
                transceiver.set_property("direction", GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY)
            if transceiver.find_property("codec-preferences") is not None:
                transceiver.set_property("codec-preferences", rtp)
            self.bus = self.pipeline.get_bus()
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("WebRTC state failed")
        except Exception:
            self.close()
            raise

    def message(self, kind, **payload):
        if not self.closed.is_set():
            self.send({"version": 1, "type": kind, "cameraId": self.camera_id,
                       "peerId": self.peer_id, **payload})

    def _ice(self, _element, mline, candidate):
        self.message("ice", candidate=candidate, sdpMLineIndex=mline)

    def _negotiate(self, _element):
        if self.closed.is_set() or self.offered:
            return
        # Wait for actual SPS-derived RTP caps, including profile-level-id.
        # Creating an offer before the first AU can produce an empty SDP.
        if self.rtp_caps.get_static_pad("src").get_current_caps() is None:
            return
        self.offered = True
        self.webrtc.emit("create-offer", None,
                         self.Gst.Promise.new_with_change_func(self._offer, None))

    def _offer(self, promise, *_args):
        if self.closed.is_set():
            return
        try:
            # Keep the owning Gst.Structure alive while using its boxed SDP.
            reply = promise.get_reply()
            offer = reply.get_value("offer")
            local = self.Gst.Promise.new()
            self.webrtc.emit("set-local-description", offer, local)
            local.interrupt()
            self.message("offer", sdp=offer.sdp.as_text())
        except Exception:
            self.message("error", code="offer_failed")

    def answer(self, sdp):
        if self.answered or not self.offered:
            raise ValueError("Unexpected answer")
        result, message = self.Sdp.SDPMessage.new()
        if result != self.Sdp.SDPResult.OK:
            raise ValueError("SDP unavailable")
        if self.Sdp.sdp_message_parse_buffer(sdp.encode(), message) != self.Sdp.SDPResult.OK:
            raise ValueError("Invalid SDP")
        description = self.WebRTC.WebRTCSessionDescription.new(self.WebRTC.WebRTCSDPType.ANSWER, message)
        self.answered = True
        promise = self.Gst.Promise.new_with_change_func(self._remote_ready, None)
        self.webrtc.emit("set-remote-description", description, promise)

    def _remote_ready(self, promise, *_args):
        if self.closed.is_set():
            return
        reply = promise.get_reply()
        if reply is not None and reply.has_field("error"):
            self.message("error", code="answer_failed")
            return
        self.remote_set.set()

    def ice(self, candidate, mline):
        with self.ice_lock:
            if len(self.pending_ice) >= 256:
                raise ValueError("Too many ICE candidates")
            self.pending_ice.append((mline, candidate))

    def pump(self):
        if self.closed.is_set():
            return False
        error = self.bus.pop_filtered(self.Gst.MessageType.ERROR | self.Gst.MessageType.EOS)
        state = self.webrtc.get_property("connection-state")
        if error is not None or state == self.WebRTC.WebRTCPeerConnectionState.FAILED:
            self.message("error", code="transport_failed")
            return False
        if time.monotonic() - self.started > 30 and state != self.WebRTC.WebRTCPeerConnectionState.CONNECTED:
            self.message("error", code="connection_timeout")
            return False
        if self.remote_set.is_set():
            with self.ice_lock:
                candidates, self.pending_ice = self.pending_ice, []
            for mline, candidate in candidates:
                self.webrtc.emit("add-ice-candidate", mline, candidate)
        for frame in self.store.read_after(self.sequence):
            if frame.sequence != self.sequence + 1:
                self.wait_keyframe = True
            self.sequence = frame.sequence
            if (self.source.get_property("current-level-bytes") >= 1048576
                    or (self.buffer_limit and self.source.get_property("current-level-buffers") >= 2)):
                self.wait_keyframe = True
                self.dropped += 1
                continue
            if self.wait_keyframe and not frame.keyframe:
                self.dropped += 1
                continue
            self.wait_keyframe = False
            buffer = self.Gst.Buffer.new_allocate(None, len(frame.data), None)
            buffer.fill(0, frame.data)
            # appsrc stamps peer running-time; camera PTS stays in metadata.
            if self.source.emit("push-buffer", buffer) != self.Gst.FlowReturn.OK:
                self.message("error", code="sender_failed")
                return False
        if not self.offered:
            self._negotiate(self.webrtc)
        return True

    def close(self):
        self.closed.set()
        if getattr(self, "webrtc", None) is not None:
            for handler in self.handlers:
                self.webrtc.disconnect(handler)
        self.pipeline.set_state(self.Gst.State.NULL)
        if self.pad is not None:
            self.webrtc.release_request_pad(self.pad)
            self.pad = None


class PeerRegistry:
    """A server-wide cap shared by all WebSocket connections."""
    def __init__(self, limit=32):
        self.limit = limit
        self.lock = threading.Lock()
        self.active = 0

    def acquire(self):
        with self.lock:
            if self.active >= self.limit:
                raise ValueError("Preview capacity reached")
            self.active += 1

    def release(self):
        with self.lock:
            self.active -= 1
