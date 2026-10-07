"""Safe, bounded observation of preview's actual source helper and mux input.

Imported only in the child process after all native output has been redirected.
No inference/tracker is created; input topology/properties match preview.
"""
import time
from rtsp_diagnostics import CODECS, DECODERS, classify_bus_error, source_failure_detail

MEDIA = frozenset({'video/x-raw', 'image/jpeg', 'video/x-h264', 'video/x-h265', 'application/x-rtp'})
FORMATS = frozenset({'NV12', 'RGBA', 'I420', 'YUY2', 'UYVY', 'RGB', 'BGR', 'BGRx', 'RGBx', 'GRAY8', 'Y444'})
FACTORIES = DECODERS | frozenset({'nvurisrcbin', 'uridecodebin', 'rtspsrc', 'nvvideoconvert',
                                'videoconvert', 'capsfilter', 'queue', 'nvstreammux', 'rtpjpegdepay', 'jpegparse'})


def safe_caps(caps):
    """Whitelist individual fields; never stringify caps/features/structures."""
    if caps is None or caps.get_size() == 0:
        return {'media': 'unknown', 'memory': 'unknown'}
    structure = caps.get_structure(0)
    media = structure.get_name()
    features = caps.get_features(0)
    memory = 'unknown'
    if features is not None and not features.is_any():
        if features.contains('memory:NVMM'):
            memory = 'NVMM'
        elif features.get_size() == 0 or features.contains('memory:SystemMemory'):
            memory = 'SYSTEM'
    result = {'media': media if media in MEDIA else 'other', 'memory': memory}
    for key in ('width', 'height'):
        value = structure.get_value(key) if structure.has_field(key) else None
        if isinstance(value, int) and 0 < value < (1 << 31):
            result[key] = value
    value = structure.get_value('format') if structure.has_field('format') else None
    if isinstance(value, str) and value in FORMATS:
        result['format'] = value
    codec = structure.get_value('encoding-name') if structure.has_field('encoding-name') else None
    if isinstance(codec, str) and codec.upper() in CODECS:
        result['codec'] = codec.upper()
    return result


def helper_failure(reason):
    """Only inspect app-owned fixed helper messages, never emit their text."""
    return source_failure_detail(reason)


def gst_nvuri_probe(uri, timeout, latency, emit):
    from preview import Gst, GstRtsp, create_preview_source_bin, make_element
    Gst.init(None)
    Gst.debug_set_active(False)
    observation = {'stage': 'source_setup', 'codec': 'unknown', 'decoder': 'unknown',
                   'decoder_created': False, 'hardware_decoder': False, 'frames': 0,
                   'caps': {}, 'link_to_streammux': 'unverified', 'direct_mux_accepts_caps': None}
    watched = set()
    output_pad = [None]
    mux_pad = None
    pipeline = Gst.Pipeline.new('single-camera-nvuri-check')

    def capture(role, caps, evidence):
        summary = safe_caps(caps)
        observation['caps'][role] = {**summary, 'evidence': evidence}
        if summary.get('codec'):
            observation['codec'] = summary['codec']
        if summary['media'] == 'image/jpeg':
            observation['codec'] = 'JPEG'
        if role == 'nvurisrcbin_src' and summary['media'] in MEDIA:
            observation['stage'] = 'nvurisrcbin_output'

    def watch(role, pad):
        if pad is None or (role, pad) in watched:
            return
        watched.add((role, pad))
        current = pad.get_current_caps()
        if current is not None:
            capture(role, current, 'current')
        else:
            # Advertised caps are explicitly distinguished from negotiation.
            capture(role, pad.query_caps(None), 'advertised')
        def event(_pad, info):
            downstream = info.get_event()
            if downstream is not None and downstream.type == Gst.EventType.CAPS:
                capture(role, downstream.parse_caps(), 'negotiated')
            return Gst.PadProbeReturn.OK
        pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, event)

    def observer(kind, obj):
        if kind == 'source_pad':
            caps = obj.get_current_caps() or obj.query_caps(None)
            if caps is not None and caps.get_size() and caps.get_structure(0).get_name().startswith('video/'):
                output_pad[0] = obj
                watch('nvurisrcbin_src', obj)
            return
        factory = obj.get_factory() if isinstance(obj, Gst.Element) else None
        name = factory.get_name() if factory is not None else ''
        klass = factory.get_metadata('klass') if factory is not None else ''
        if name in DECODERS or 'Decoder' in (klass or '').split('/'):
            observation.update(decoder=name if name in DECODERS else 'other_decoder',
                               decoder_created=True, hardware_decoder=name == 'nvv4l2decoder')
            watch('decoder_sink', obj.get_static_pad('sink'))
            watch('decoder_src', obj.get_static_pad('src'))
        if name == 'nvvideoconvert':
            watch('nvuri_convert_sink', obj.get_static_pad('sink'))
            watch('nvuri_convert_src', obj.get_static_pad('src'))
        if name == 'rtspsrc':
            def select(_source, _number, caps):
                summary = safe_caps(caps)
                if summary.get('codec'):
                    observation['codec'] = summary['codec']
                return caps.get_size() > 0 and caps.get_structure(0).get_value('media') == 'video'
            obj.connect('select-stream', select)

    def failed(reason):
        stage, code = helper_failure(reason)
        observation.update(stage=stage, failure_stage=stage, reason=code)

    try:
        source = create_preview_source_bin(0, uri, latency, failed, True, 10, observer=observer)
        mux = make_element('nvstreammux', 'stream-muxer')
        for key, value in (('batch-size', 1), ('live-source', 1), ('width', 1920), ('height', 1080),
                           ('batched-push-timeout', 40000), ('gpu-id', 0), ('nvbuf-memory-type', 0)):
            mux.set_property(key, value)
        if mux.find_property('sync-inputs') is not None:
            mux.set_property('sync-inputs', False)
        sink = make_element('fakesink', 'mux-output')
        for key, value in (('sync', False), ('async', False), ('signal-handoffs', True)):
            sink.set_property(key, value)
        sink.connect('handoff', lambda *_: observation.update(frames=observation['frames'] + 1))
        for element in (source, mux, sink):
            pipeline.add(element)
        if not mux.link(sink):
            observation.update(stage='input_static_link', failure_stage='input_static_link', reason='source_link_error')
            emit({'rtsp_connection': 'failed', **observation})
            return
        mux_pad = mux.request_pad_simple('sink_0')
        if mux_pad is None or source.get_static_pad('src').link(mux_pad) != Gst.PadLinkReturn.OK:
            observation.update(stage='mux_static_link', failure_stage='mux_static_link', reason='source_link_error')
            emit({'rtsp_connection': 'failed', **observation})
            return
        observation['static_link_to_streammux'] = 'ok'
        for role, pad in (('source_bin_src', source.get_static_pad('src')),
                          ('mux_sink', mux_pad), ('mux_src', mux.get_static_pad('src'))):
            watch(role, pad)
        pipeline.set_state(Gst.State.PLAYING)
        bus = pipeline.get_bus()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if 'reason' in observation or observation['frames']:
                break
            if message is not None:
                factory = message.src.get_factory() if message.src is not None else None
                name = factory.get_name() if factory is not None else ''
                stage = name if name in FACTORIES else 'other_element'
                detail = classify_bus_error(message) if message.type == Gst.MessageType.ERROR else {'reason': 'source_eos'}
                observation.update(**detail, stage=stage, failure_stage=stage)
                break
        # Refresh the pre-gate pad even when the callback prevented a ghost link.
        if output_pad[0] is not None:
            caps = output_pad[0].get_current_caps()
            if caps is not None:
                capture('nvurisrcbin_src', caps, 'current')
                observation['direct_mux_accepts_caps'] = bool(mux_pad.query_accept_caps(caps))
        if 'failure_stage' in observation:
            observation['stage'] = observation['failure_stage']
        if observation['frames']:
            observation.update(stage='mux_frame', reason='ok', link_to_streammux='ok')
        else:
            observation.setdefault('reason', 'probe_no_mux_frame')
            observation.setdefault('failure_stage', observation['stage'])
            observation['link_to_streammux'] = 'failed'
        emit({'rtsp_connection': 'ok' if observation['frames'] else 'failed', **observation})
    except Exception:
        observation.update(reason='probe_setup_failed', failure_stage='probe_setup')
        emit({'rtsp_connection': 'failed', **observation})
    finally:
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        if mux_pad is not None:
            mux.release_request_pad(mux_pad)
