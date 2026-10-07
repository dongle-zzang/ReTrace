"""First-arrival diagnostics; never retain URLs, SDP, or native messages."""

import json
import os
from pathlib import Path
import subprocess
import threading
import time
import weakref


def startup_origin(process_entry):
    """Linux PID 1 start in a PID namespace, otherwise Python entry fallback."""
    try:
        if Path('/proc/1/comm').read_text().strip() in ('systemd', 'init'):
            return process_entry, 'python_entry'
        stat = Path('/proc/1/stat').read_text().rsplit(')', 1)[1].split()
        age = time.clock_gettime(time.CLOCK_BOOTTIME) - int(stat[19]) / os.sysconf('SC_CLK_TCK')
        return time.monotonic() - age, 'pid1_start'
    except (OSError, ValueError, IndexError, AttributeError):
        return process_entry, 'python_entry'


class StartupTiming:
    def __init__(self, cameras, origin, origin_name, clock=time.monotonic):
        self.clock, self.origin, self.origin_name = clock, origin, origin_name
        self.cameras = {slot: camera.camera_id for slot, camera in enumerate(cameras)}
        self.lock = threading.Lock()
        self.events = {}
        self.pending = []
        self.gpu_stop = threading.Event()
        self.gpu_thread = None
        self.gpu_sample = None

    def start_gpu_sampling(self):
        def sample():
            while not self.gpu_stop.is_set():
                memory = gpu_memory()
                with self.lock:
                    self.gpu_sample = {'elapsed_ms': round((self.clock() - self.origin) * 1000, 3),
                                       'devices': memory}
                self.gpu_stop.wait(5)
        self.gpu_thread = threading.Thread(target=sample, daemon=True)
        self.gpu_thread.start()

    def stop_gpu_sampling(self):
        self.gpu_stop.set()
        if self.gpu_thread is not None:
            self.gpu_thread.join(timeout=1.1)

    def mark(self, stage, slot=None, pts_ns=None, at=None):
        with self.lock:
            key = (slot, stage)
            if key in self.events:
                return
            event = {'stage': stage, 'elapsed_ms': round(((self.clock() if at is None else at) - self.origin) * 1000, 3)}
            if slot is not None:
                event['camera_id'] = self.cameras[slot]
            if pts_ns is not None:
                event['pts_ns'] = int(pts_ns)
            self.events[key] = event
            self.pending.append(event)

    def complete(self, stage):
        with self.lock:
            return all((slot, stage) in self.events for slot in self.cameras)

    def drain(self):
        with self.lock:
            pending, self.pending = self.pending, []
        for event in pending:
            # JSON quotes configured IDs to prevent newline/log injection.
            print('startup ' + json.dumps(event, separators=(',', ':')), flush=True)

    def summary(self):
        with self.lock:
            gpu_sample = self.gpu_sample
            per_camera = {camera_id: {stage: event['elapsed_ms']
                                     for (index, stage), event in self.events.items() if index == slot}
                          for slot, camera_id in self.cameras.items()}
            shared = {stage: event['elapsed_ms'] for (slot, stage), event in self.events.items() if slot is None}
        first = [stages.get('first_h264_buffer', stages.get('first_mjpeg_available'))
                 for stages in per_camera.values()
                 if 'first_h264_buffer' in stages or 'first_mjpeg_available' in stages]
        online = [stages['online'] for stages in per_camera.values() if 'online' in stages]
        return {'origin': self.origin_name, 'sources': len(self.cameras), 'shared_ms': shared,
                'gpu_memory': gpu_sample,
                'elapsed_ms': round((self.clock() - self.origin) * 1000, 3),
                'cameras_ms': per_camera, 'frames_observed': len(first), 'online_observed': len(online),
                'first_camera_ms': min(first) if first else None,
                'last_observed_camera_ms': max(first) if first else None,
                'first_frame_spread_ms': round(max(first) - min(first), 3) if first else None,
                'all_configured_online_ms': max(online) if len(online) == len(self.cameras) else None,
                'last_observed_online_ms': max(online) if online else None,
                'missing_online': [key for key, stages in per_camera.items() if 'online' not in stages],
                'missing_frames': [key for key, stages in per_camera.items()
                                   if 'first_mjpeg_available' not in stages and 'first_h264_buffer' not in stages]}


def gpu_memory():
    """Device-wide MiB (includes other processes); numeric output only."""
    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=index,memory.used',
                                 '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=1)
        if result.returncode:
            return None
        return [{'gpu': int(row.split(',')[0]), 'used_mib': int(row.split(',')[1])}
                for row in result.stdout.strip().splitlines()]
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def source_observer(timing, slot, Gst):
    """Observe native child creation and RTSP/decoder milestones, once each."""
    seen = weakref.WeakSet()  # Do not keep retired reconnect children alive.

    def probe(pad, stage):
        if pad is None:
            return
        def first(_pad, info):
            buffer = info.get_buffer()
            if buffer is None:
                return Gst.PadProbeReturn.OK
            timing.mark(stage, slot, None if buffer.pts == Gst.CLOCK_TIME_NONE else buffer.pts)
            return Gst.PadProbeReturn.REMOVE
        pad.add_probe(Gst.PadProbeType.BUFFER, first)

    def observe(kind, item):
        try:
            if kind == 'source_pad':
                caps = item.get_current_caps() or item.query_caps(None)
                if caps and caps.get_size() and caps.get_structure(0).get_name().startswith('video/'):
                    timing.mark('pad_added', slot)
                return
            if kind != 'child' or item in seen:
                return
            seen.add(item)
            factory = item.get_factory() if isinstance(item, Gst.Element) else None
            name = factory.get_name() if factory else None
            if name == 'rtspsrc':
                timing.mark('rtsp_source_created', slot)
                def before_send(_source, _message):
                    timing.mark('rtsp_request_start', slot)
                    return True
                item.connect('before-send', before_send)
                item.connect('on-sdp', lambda *_: timing.mark('rtsp_sdp_received', slot))
                def rtp_pad(_source, pad):
                    caps = pad.get_current_caps() or pad.query_caps(None)
                    if caps and caps.get_size() and caps.get_structure(0).get_value('media') == 'video':
                        timing.mark('rtsp_video_pad_added', slot)
                        probe(pad, 'first_rtp_buffer')
                item.connect('pad-added', rtp_pad)
            elif name == 'nvv4l2decoder':
                timing.mark('decoder_created', slot)
                probe(item.get_static_pad('sink'), 'first_decoder_input')
                probe(item.get_static_pad('src'), 'first_decoded_frame')
        except Exception:
            timing.mark('source_observer_unavailable', slot)
    return observe


def bus_observer(timing, Gst, elements, scope):
    """Timestamp on the posting thread, even while set_state blocks main polling."""
    def observe(_bus, message, _data):
        try:
            if message.type == Gst.MessageType.STATE_CHANGED:
                _old, current, _pending = message.parse_state_changed()
                states = {Gst.State.READY: 'ready', Gst.State.PAUSED: 'paused', Gst.State.PLAYING: 'playing'}
                if current in states:
                    for name, element in elements.items():
                        if message.src == element:
                            timing.mark(name + '_' + states[current])
            elif message.type == Gst.MessageType.PROGRESS:
                source, _reason = scope(message)
                factory = message.src.get_factory()
                if source != 'all' and factory and factory.get_name() == 'rtspsrc':
                    kind, code, _private_text = message.parse_progress()
                    # Native progress text may contain credentials; never emit it.
                    stages = {('connect', Gst.ProgressType.CONTINUE): 'rtsp_connect_start',
                              ('open', Gst.ProgressType.START): 'rtsp_open_start',
                              ('open', Gst.ProgressType.COMPLETE): 'rtsp_open_complete'}
                    stage = stages.get((code, kind))
                    if stage:
                        timing.mark(stage, int(source))
        except Exception:
            timing.mark('bus_observer_unavailable')
        return Gst.BusSyncReply.PASS
    return observe
