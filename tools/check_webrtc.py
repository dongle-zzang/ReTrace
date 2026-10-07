#!/usr/bin/env python3
"""Check native plugins and optional synthetic H.264/SDP/ICE loopback.

No RTSP addresses are read. This is not a browser or DeepStream detection test.
"""
import argparse
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loopback", action="store_true")
    parser.add_argument("--software-test-source", action="store_true",
                        help="Use x264 only for this synthetic checker, never for production preview")
    args = parser.parse_args()
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstWebRTC", "1.0")
    gi.require_version("GstSdp", "1.0")
    from gi.repository import Gst, GstWebRTC, GstSdp
    from preview_webrtc import EncodedStore, WebRTCPeer
    Gst.init(None)
    encoder = "x264enc" if args.software_test_source else "nvv4l2h264enc"
    required = (encoder, "h264parse", "rtph264pay", "rtph264depay", "webrtcbin", "appsrc",
                "appsink", "nicesrc", "nicesink", "dtlssrtpenc", "dtlssrtpdec")
    missing = [factory for factory in required if Gst.ElementFactory.find(factory) is None]
    print(json.dumps({"gstreamer": Gst.version_string(), "missing": missing, "encoder": encoder}))
    if missing:
        return 1
    if not args.loopback:
        return 0
    store = EncodedStore()
    encoded, received = [0], [0]
    codec = ("x264enc tune=zerolatency speed-preset=ultrafast key-int-max=10 ! "
             if args.software_test_source else
             "nvvideoconvert ! video/x-raw(memory:NVMM),format=NV12 ! "
             "nvv4l2h264enc profile=0 iframeinterval=10 ! ")
    source = Gst.parse_launch("videotestsrc is-live=true ! video/x-raw,width=640,height=360,framerate=10/1 ! " + codec +
        "h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au,profile=constrained-baseline ! "
        "appsink name=encoded emit-signals=true sync=false max-buffers=1")
    receiver = Gst.Pipeline.new("receiver")
    rtc = Gst.ElementFactory.make("webrtcbin", "receiver-rtc")
    rtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
    receiver.add(rtc)
    peer = None
    pending_ice = []
    lock = threading.Lock()
    remote_ready = threading.Event()

    def sample(sink):
        item = sink.emit("pull-sample")
        buffer = item.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if ok:
            store.put(bytes(info.data), not buffer.has_flags(Gst.BufferFlags.DELTA_UNIT))
            buffer.unmap(info)
            encoded[0] += 1
        return Gst.FlowReturn.OK

    def pad_added(_rtc, pad):
        if pad.get_direction() != Gst.PadDirection.SRC:
            return
        sink_bin = Gst.parse_bin_from_description(
            "queue ! rtph264depay ! h264parse ! fakesink name=received signal-handoffs=true sync=false async=false", True)
        sink_bin.get_by_name("received").connect("handoff", lambda *_args: received.__setitem__(0, received[0] + 1))
        receiver.add(sink_bin)
        pad.link(sink_bin.get_static_pad("sink"))
        sink_bin.sync_state_with_parent()

    def answer_created(promise, *_args):
        reply = promise.get_reply()
        answer = reply.get_value("answer")
        local = Gst.Promise.new()
        rtc.emit("set-local-description", answer, local)
        local.interrupt()
        peer.answer(answer.sdp.as_text())

    def offer_set(_promise, *_args):
        remote_ready.set()
        rtc.emit("create-answer", None, Gst.Promise.new_with_change_func(answer_created, None))

    def send(message):
        if message["type"] == "offer":
            _, sdp = GstSdp.SDPMessage.new()
            GstSdp.sdp_message_parse_buffer(message["sdp"].encode(), sdp)
            desc = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.OFFER, sdp)
            rtc.emit("set-remote-description", desc, Gst.Promise.new_with_change_func(offer_set, None))
        elif message["type"] == "ice":
            with lock:
                pending_ice.append((message["sdpMLineIndex"], message["candidate"]))
        elif message["type"] == "error":
            print(json.dumps({"peer_error": message["code"]}))

    source.get_by_name("encoded").connect("new-sample", sample)
    rtc.connect("pad-added", pad_added)
    rtc.connect("on-ice-candidate", lambda _rtc, mline, candidate: peer.ice(candidate, mline) if peer else None)
    try:
        receiver.set_state(Gst.State.PLAYING)
        peer = WebRTCPeer("synthetic", "loopback", store, send)
        source.set_state(Gst.State.PLAYING)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and received[0] < 5:
            error_message = source.get_bus().pop_filtered(Gst.MessageType.ERROR)
            if error_message is not None:
                error, _debug = error_message.parse_error()
                print(json.dumps({"source_error": {"domain": error.domain, "code": error.code}}))
                break
            if remote_ready.is_set():
                with lock:
                    for mline, candidate in pending_ice:
                        rtc.emit("add-ice-candidate", mline, candidate)
                    pending_ice.clear()
            if not peer.pump():
                break
            time.sleep(0.02)
        print(json.dumps({"encoded_units": encoded[0], "received_units": received[0],
                          "loopback": "passed" if received[0] >= 5 else "failed"}))
        return int(received[0] < 5)
    finally:
        if peer:
            peer.close()
        receiver.set_state(Gst.State.NULL)
        source.set_state(Gst.State.NULL)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({"check": "failed", "error_type": type(error).__name__}))
        sys.exit(1)
