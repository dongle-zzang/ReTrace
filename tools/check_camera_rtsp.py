#!/usr/bin/env python3
"""Test one configured RTSP camera; URI and native diagnostics never reach stdout."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from camera_config import CameraConfigError, read_camera_entries, validate_rtsp
from dotenv import dotenv_values
from rtsp_diagnostics import CODECS, DECODERS, classify_bus_error, uri_shape


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'Invalid arguments; values hidden. Use --help.\n')


def camera_input(camera_id, config_path, env_path, environ=None):
    """Match preview startup precedence, resolving only the requested camera."""
    entry = next((row for row in read_camera_entries(config_path) if row['id'] == camera_id), None)
    if entry is None:
        raise CameraConfigError('Camera not configured; values hidden')
    private = dotenv_values(env_path, interpolate=False) if Path(env_path).is_file() else {}
    values = {**private, **(os.environ if environ is None else environ)}
    uri = (values.get(entry['rtsp_env']) or '').strip()
    validate_rtsp(uri, 'Selected camera')
    return uri


def gst_probe(uri, timeout, latency, emit):
    """One uridecodebin -> fakesink, TCP, no infer/mux/tracker/retry worker.

    Native output is redirected by worker() before any GI/plugin import. Emit
    the safe result before NULL cleanup so a native teardown hang is bounded by
    the parent. One decoded video frame is required for success.
    """
    import gi
    gi.require_version('Gst', '1.0')
    gi.require_version('GstRtsp', '1.0')
    from gi.repository import Gst, GstRtsp
    Gst.init(None)
    Gst.debug_set_active(False)
    observation = {'stage': 'CONNECT', 'codec': 'unknown', 'decoder_created': False,
                   'hardware_decoder': False, 'frames': 0}
    pipeline = Gst.Pipeline.new('single-camera-rtsp-check')
    decoder = Gst.ElementFactory.make('uridecodebin', 'camera-decoder')
    sink = Gst.ElementFactory.make('fakesink', 'decoded-video')
    if pipeline is None or decoder is None or sink is None:
        emit({'rtsp_connection': 'failed', 'stage': 'SETUP', 'reason': 'probe_setup_failed'})
        return
    decoder.set_property('uri', uri)
    sink.set_property('sync', False)
    sink.set_property('async', False)
    sink.set_property('signal-handoffs', True)
    sink.connect('handoff', lambda *_args: observation.update(frames=observation['frames'] + 1))

    def before_send(_source, request):
        # parse_request returns URI as well; retain only its enum method.
        try:
            method = request.parse_request()[1]
            methods = {GstRtsp.RTSPMethod.OPTIONS: 'OPTIONS', GstRtsp.RTSPMethod.DESCRIBE: 'DESCRIBE',
                       GstRtsp.RTSPMethod.SETUP: 'SETUP', GstRtsp.RTSPMethod.PLAY: 'PLAY'}
            if method in methods:
                observation['stage'] = methods[method]
        except Exception:
            pass  # Stage stays last known; never guess a method or print request.
        return True

    def select_stream(_source, _number, caps):
        if caps.get_size() == 0:
            return False
        structure = caps.get_structure(0)
        if structure.get_value('media') != 'video':
            return False
        codec = str(structure.get_value('encoding-name') or '').upper()
        observation['codec'] = codec if codec in CODECS else 'unknown'
        return True

    def source_setup(_decoder, source):
        source.set_property('protocols', GstRtsp.RTSPLowerTrans.TCP)
        source.set_property('latency', latency)
        source.set_property('drop-on-latency', True)
        source.connect('select-stream', select_stream)
        source.connect('before-send', before_send)

    def child_added(_parent, child, _name):
        factory = child.get_factory() if isinstance(child, Gst.Element) else None
        name = factory.get_name() if factory is not None else ''
        classification = factory.get_metadata('klass') if factory is not None else ''
        if name in DECODERS or 'Decoder' in (classification or '').split('/'):
            observation['decoder_created'] = True
            observation['hardware_decoder'] = name == 'nvv4l2decoder'
            if name == 'nvv4l2decoder':
                for key, value in (('num-extra-surfaces', 4), ('cudadec-memtype', 0)):
                    if child.find_property(key) is not None:
                        child.set_property(key, value)
        if isinstance(child, Gst.ChildProxy):
            child.connect('child-added', child_added)

    def pad_added(_decoder, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps is None or caps.get_size() == 0:
            return
        if caps.get_structure(0).get_name().startswith('video/x-raw'):
            observation['stage'] = 'DECODING'
            if not sink.get_static_pad('sink').is_linked():
                pad.link(sink.get_static_pad('sink'))

    decoder.connect('source-setup', source_setup)
    decoder.connect('child-added', child_added)
    decoder.connect('pad-added', pad_added)
    pipeline.add(decoder)
    pipeline.add(sink)
    result = None
    try:
        state = pipeline.set_state(Gst.State.PLAYING)
        if state == Gst.StateChangeReturn.FAILURE:
            # Read the bus before attributing a state failure to any cause.
            observation['stage'] = 'SETUP'
        bus = pipeline.get_bus()
        deadline = time.monotonic() + timeout
        types = Gst.MessageType.ERROR | Gst.MessageType.WARNING | Gst.MessageType.EOS | Gst.MessageType.ELEMENT
        while time.monotonic() < deadline:
            message = bus.timed_pop_filtered(100 * Gst.MSECOND, types)
            if observation['frames']:
                result = {'rtsp_connection': 'ok', 'reason': 'ok', **observation, 'stage': 'FRAME'}
                break
            if message is None:
                continue
            if message.type == Gst.MessageType.ERROR:
                result = {'rtsp_connection': 'failed', **observation, **classify_bus_error(message)}
                break
            if message.type == Gst.MessageType.EOS:
                result = {'rtsp_connection': 'failed', **observation, 'reason': 'source_eos'}
                break
            if message.type == Gst.MessageType.ELEMENT:
                structure = message.get_structure()
                if structure is not None and structure.get_name() == 'GstRTSPSrcTimeout':
                    result = {'rtsp_connection': 'failed', **observation, 'reason': 'rtsp_timeout'}
                    break
        if result is None:
            result = {'rtsp_connection': 'failed', **observation, 'reason': 'rtsp_timeout'}
        emit(result)
    finally:
        pipeline.set_state(Gst.State.NULL)


def worker():
    # Only this saved pipe descriptor carries Python-created safe JSON. C/C++
    # stdout/stderr and Python callback tracebacks go to /dev/null throughout.
    output_fd = os.dup(1)
    with open(os.devnull, 'wb') as hidden:
        os.dup2(hidden.fileno(), 1)
        os.dup2(hidden.fileno(), 2)
    os.environ['GST_DEBUG'] = '0'
    os.environ.pop('GST_DEBUG_FILE', None)
    emitted = False
    def emit(result):
        nonlocal emitted
        if not emitted:
            os.write(output_fd, (json.dumps(result, separators=(',', ':')) + '\n').encode())
            emitted = True
    try:
        request = json.load(sys.stdin)
        if request.get('source_mode') == 'nvurisrcbin':
            from tools.nvuri_probe import gst_nvuri_probe
            gst_nvuri_probe(request['uri'], request['timeout'], request['latency'], emit)
        else:
            gst_probe(request['uri'], request['timeout'], request['latency'], emit)
    except Exception:
        emit({'rtsp_connection': 'failed', 'stage': 'SETUP', 'reason': 'probe_setup_failed'})
    finally:
        os.close(output_fd)


def probe(uri, timeout=15, latency=1000, runner=subprocess.run, source_mode='uridecodebin'):
    request = json.dumps({'uri': uri, 'timeout': timeout, 'latency': latency, 'source_mode': source_mode}).encode()
    try:
        response = runner([sys.executable, str(Path(__file__).resolve()), '--_worker'],
                          input=request, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          timeout=timeout + 5, check=False)
        body = response.stdout
    except subprocess.TimeoutExpired as error:
        # worker may already have emitted its result before a NULL teardown hang.
        body = error.output or b''
    except OSError:
        body = b''
    try:
        return json.loads(body)
    except (ValueError, UnicodeError):
        return {'rtsp_connection': 'failed', 'stage': 'UNKNOWN', 'reason': 'probe_timeout_or_setup_failed'}


def diagnose(camera_id, config, env_file, timeout=15, latency=1000, compare_to=None, prober=probe,
             source_mode='uridecodebin'):
    def check(uri):
        if source_mode == 'nvurisrcbin':
            return prober(uri, timeout, latency, source_mode=source_mode)
        return prober(uri, timeout, latency)
    uri = camera_input(camera_id, config, env_file)
    baseline = None
    if compare_to and compare_to != camera_id:
        reference = camera_input(compare_to, config, env_file)
        baseline = {'camera_id': compare_to, 'uri_shape': uri_shape(reference), **check(reference)}
    result = {'camera_id': camera_id, 'uri_shape': uri_shape(uri), 'transport': 'TCP',
              'latency_ms': latency, 'source_mode': source_mode, **check(uri)}
    if baseline is not None:
        result['comparison'] = {'camera_id': compare_to, 'baseline_connection': baseline['rtsp_connection'],
                                'baseline_uri_shape': baseline['uri_shape'], 'baseline_codec': baseline.get('codec', 'unknown'),
                                'profile_differs': result['uri_shape']['profile'] != baseline['uri_shape']['profile'],
                                'codec_differs': (result.get('codec') != baseline.get('codec')
                                     if result.get('codec', 'unknown') != 'unknown' and baseline.get('codec', 'unknown') != 'unknown' else None)}
        if source_mode == 'nvurisrcbin':
            result['comparison']['baseline_probe'] = baseline
    return result


def main():
    parser = SafeParser(description=__doc__)
    parser.add_argument('--camera-id')
    parser.add_argument('--compare-to', help='Probe this baseline first, then the selected camera, sequentially')
    parser.add_argument('--cameras', type=Path, default=ROOT / 'configs/cameras.yaml')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--timeout', type=float, default=15)
    parser.add_argument('--rtsp-latency', type=int, default=1000)
    parser.add_argument('--source-mode', choices=('uridecodebin', 'nvurisrcbin'), default='uridecodebin',
                        help='nvurisrcbin reuses preview source and its existing converter/mux input path')
    parser.add_argument('--_worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._worker:
        worker()
        return 0
    if not args.camera_id or not 1 <= args.timeout <= 120 or not 0 <= args.rtsp_latency <= (1 << 32) - 1:
        parser.error('Invalid selection or timeout')
    try:
        result = diagnose(args.camera_id, args.cameras, args.env_file, args.timeout, args.rtsp_latency,
                          args.compare_to, source_mode=args.source_mode)
    except Exception:
        # Bad private config/path/credentials must not appear in exception output.
        print('rtsp_connection=failed reason=rtsp_config_error')
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    return int(result['rtsp_connection'] != 'ok')


if __name__ == '__main__':
    sys.exit(main())
