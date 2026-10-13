"""Shared DeepStream RTSP source-bin helper used by preview.py.

The former headless PeopleNet person-detection CLI was removed with PeopleNet.
"""

import os
import sys

# GStreamer's default diagnostics can include RTSP URIs and credentials.
# Only the sanitized application messages below should be printed.
os.environ["GST_DEBUG"] = "0"
sys.dont_write_bytecode = True

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst


def make_element(factory, name):
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"Required GStreamer element is unavailable: {factory}")
    return element


def create_source_bin(index, uri, fail, source_factory="uridecodebin", observer=None):
    """Use test3's dynamic video pad / NVMM ghost-pad source-bin pattern."""
    source_bin = Gst.Bin.new(f"source-bin-{index}")
    if source_bin is None:
        raise RuntimeError(f"source={index}: could not create source bin")
    decoder = make_element(source_factory, f"uri-decode-bin-{index}")
    decoder.set_property("uri", uri)
    source_bin.add(decoder)
    ghost_pad = Gst.GhostPad.new_no_target("src", Gst.PadDirection.SRC)
    if ghost_pad is None or not source_bin.add_pad(ghost_pad):
        raise RuntimeError(f"source={index}: could not create source pad")

    def on_pad_added(decodebin, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        if observer is not None:
            observer("source_pad", pad)
        if caps is None or caps.get_size() == 0:
            fail(f"source={index}: decoder pad has no caps")
            return
        if not caps.get_structure(0).get_name().startswith("video/"):
            return  # Ignore RTSP audio pads.
        if not caps.get_features(0).contains("memory:NVMM"):
            fail(f"source={index}: video decoder output is not NVIDIA NVMM")
            return
        if ghost_pad.get_target() == pad:
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
    decoder.connect("pad-removed", lambda _decoder, pad:
                    ghost_pad.set_target(None) if ghost_pad.get_target() == pad else None)
    if source_factory == "uridecodebin":
        decoder.connect("source-setup", on_source_setup)
    return source_bin
