"""Execute shared pipeline construction and camera lifecycle without GPU imports."""
import ast
import contextlib
import io
from pathlib import Path
import threading
from types import SimpleNamespace as NS
import unittest

from camera_config import Camera, load_cameras
from camera_runtime import CameraRuntime, CameraRuntimeManager
from person_metadata import FrameMetadata, MetadataStore
from preview_mjpeg import FrameStore
from preview_timing import FrameTiming
from startup_timing import StartupTiming
from pgie_cache import configure_pgie, prepare_engine_cache, PRECISIONS
from unittest.mock import patch
from test_camera_runtime import load_preview

ROOT = Path(__file__).resolve().parents[1]


class SharedTests(unittest.TestCase):
    def manager(self, count=2, ids=None):
        self.now = 0.0
        cameras = [Camera((ids or list(range(count)))[i], f'camera_{i}', 1, f'Camera {i}',
                          'rtsp://private.example/video') for i in range(count)]
        runtimes = {c.source_id: CameraRuntime(c.camera_id, clock=lambda: self.now) for c in cameras}
        stores = {c.source_id: FrameStore() for c in cameras}
        manager = CameraRuntimeManager(cameras, runtimes, stores, MetadataStore())
        manager.begin()
        manager.arm_watchdogs()
        return manager

    def test_mapping_reverse_and_metadata_slots(self):
        manager = self.manager(ids=[7, 9])
        self.assertEqual(manager.slots_by_camera, {'camera_0': 0, 'camera_1': 1})
        self.assertEqual(manager.slots_by_source, {7: 0, 9: 1})
        self.assertEqual(manager.camera_for_frame(1, 1).source_id, 9)
        with self.assertRaises(RuntimeError):
            manager.camera_for_frame(1, 0)
        with self.assertRaises(RuntimeError):
            manager.camera_for_frame(7, 7)

    def test_disabled_excluded_from_batch_and_mapping(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'cameras.yaml'
            config.write_text('cameras: [{id: disabled, floor: 1, name: D, rtsp_env: D, enabled: false}, '
                              '{id: enabled, floor: 1, name: E, rtsp_env: E}]')
            cameras = load_cameras(config, Path(directory) / 'missing.env', environ={'E': 'rtsp://private.example/video'})
        manager = CameraRuntimeManager(cameras, {0: CameraRuntime('enabled')}, {0: FrameStore()}, MetadataStore())
        self.assertEqual(manager.batch_size, 1)
        self.assertEqual(manager.slots_by_camera, {'enabled': 0})

    def test_camera_fault_keeps_healthy_generation_and_clears_only_failed_data(self):
        manager = self.manager()
        for slot in (0, 1):
            manager.observe_decoded(slot, 100)
            manager.publish(slot, 100, lambda slot=slot: manager.stores[slot].put(b'jpeg'), output=True)
            manager.metadata_store.put(NS(source_id=slot))
        before = manager.runtimes[1].snapshot()
        manager.fail_source(0, 'rtsp_error')
        self.assertEqual(manager.runtimes[1].snapshot(), before)
        self.assertIsNone(manager.stores[0].jpeg)
        self.assertEqual(manager.stores[1].jpeg, b'jpeg')
        self.assertEqual(set(manager.metadata_store.snapshot()), {1})
        self.assertFalse(manager.publish(0, 100, lambda: self.fail('stale write')))
        self.now = 1
        manager.tick()
        self.assertEqual(manager.runtimes[0].snapshot().generation, 2)
        self.assertEqual(manager.runtimes[0].snapshot().state, 'reconnecting')
        self.assertEqual(manager.runtimes[1].snapshot().generation, 1)
        self.assertFalse(manager.publish(0, 100, lambda: self.fail('old PTS')))
        for i in range(30):
            self.now += .1
            manager.observe_decoded(0, 200 + i)
            manager.publish(0, 200 + i, lambda: manager.stores[0].put(b'new'), output=True)
        self.assertEqual(manager.runtimes[0].snapshot().state, 'online')
        self.assertEqual(manager.runtimes[0].snapshot().reconnect_count, 1)
        self.assertEqual(manager.stores[0].jpeg, b'new')
        self.assertFalse(manager.stores[0].closed)

    def test_watchdog_is_camera_local_and_pts_memory_bounded(self):
        manager = self.manager()
        self.now = 31
        manager.observe_decoded(1, 1)
        manager.publish(1, 1, lambda: None, output=True)
        manager.tick()
        self.assertEqual(manager.runtimes[0].snapshot().state, 'offline')
        self.assertEqual(manager.runtimes[1].snapshot().generation, 1)
        self.assertEqual(manager.runtimes[0].snapshot().last_error, 'no_frames')
        for pts in range(1000):
            manager.observe_decoded(1, pts)
        self.assertLessEqual(len(manager._frames[1]), 512)
        self.assertFalse(manager.observe_decoded(1, None))

    def test_reused_pts_cannot_publish_metadata_from_old_generation(self):
        manager = self.manager()
        manager.observe_decoded(0, 100)
        manager.fail_source(0, 'rtsp_error')
        self.now = 1
        manager.tick()
        manager.observe_decoded(0, 100)
        self.assertFalse(manager.publish(0, 100, lambda: self.fail('old metadata'), generation=1))
        self.assertTrue(manager.publish(0, 100, lambda: None, generation=2))

    def test_native_source_properties_without_child_configuration(self):
        class Child:
            def __init__(self, factory):
                self.factory, self.props, self.callbacks = factory, {}, {}
            def get_factory(self):
                return NS(get_name=lambda: self.factory)
            def get_name(self):
                return self.factory
            def set_property(self, name, value):
                self.props[name] = value
            def get_property(self, name):
                return self.props[name]
            def find_property(self, name):
                return name
            def connect(self, name, callback):
                self.callbacks[name] = callback

        class Bin(Child):
            def iterate_elements(self):
                return NS(next=lambda: ('DONE', None))

        decoder = Bin('nvurisrcbin')
        created, failures = [], []
        def create(index, uri, fail, source_factory, **options):
            created.append(source_factory)
            return NS(get_by_name=lambda name: decoder)
        namespace = load_preview({'create_preview_source_bin'}, {
            'create_source_bin': create,
            'Gst': NS(Element=Child, ChildProxy=Bin, Bin=Bin, IteratorResult=NS(OK='OK', RESYNC='RESYNC')),
            'GstRtsp': NS(RTSPLowerTrans=NS(TCP=4))})
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            namespace['create_preview_source_bin'](0, 'private-value', 1000, failures.append)
        self.assertEqual(created, ['nvurisrcbin'])
        self.assertEqual(decoder.props['rtsp-reconnect-attempts'], -1)
        self.assertEqual(decoder.props['rtsp-reconnect-interval'], 10)
        self.assertEqual(decoder.props['select-rtp-protocol'], 4)
        self.assertTrue(decoder.props['async-handling'])
        self.assertTrue(decoder.props['disable-audio'])
        self.assertTrue(decoder.props['drop-on-latency'])
        self.assertEqual(decoder.props['latency'], 1000)
        self.assertEqual(decoder.props['num-extra-surfaces'], 4)
        self.assertNotIn('child-added', decoder.callbacks)
        observations = []
        namespace['create_preview_source_bin'](0, 'private-value', 1000, failures.append,
                                              observer=lambda kind, child: observations.append((kind, child)))
        # Existing/reconnected children are observed without overriding native configuration.
        for factory in ('rtspsrc', 'nvv4l2decoder'):
            child = Child(factory)
            decoder.callbacks['child-added'](decoder, child, factory)
            self.assertEqual(observations[-1], ('child', child))
            self.assertEqual(child.props, {})
            self.assertEqual(child.callbacks, {})
        self.assertEqual(failures, [])
        self.assertNotIn('private-value', captured.getvalue())

    def test_generated_pgie_batch_and_interval_apply_person_threshold(self):
        import configparser
        import tempfile
        tree = ast.parse((ROOT / 'app.py').read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'prepare_pgie_config')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'model.onnx').write_bytes(b'mock model')
            (root / 'labels.txt').write_text('person\nother\n')
            def config():
                result = configparser.ConfigParser()
                result.read_dict({'property': {'onnx-file': 'model.onnx', 'labelfile-path': 'labels.txt',
                                 'num-detected-classes': '2', 'batch-size': '1', 'interval': '0'},
                                  'class-attrs-all': {'pre-cluster-threshold': '0.4'}})
                return root / 'sample.txt', result
            namespace = {'Path': Path, 'prepare_engine_cache': prepare_engine_cache,
                         'PRECISIONS': PRECISIONS, 'load_peoplenet_config': config, 'PGIE_ID': 1}
            exec(compile(ast.Module(body=[function], type_ignores=[]), 'app.py', 'exec'), namespace)
            for count in (1, 2, 4, 8, 25):
                with contextlib.redirect_stdout(io.StringIO()), patch(
                        'pgie_cache.engine_runtime_identity', return_value={'test': 'cpu'}):
                    path, person_id = namespace['prepare_pgie_config'](root / 'cache', count)
                generated = configparser.ConfigParser()
                generated.read(path)
                self.assertEqual(generated.getint('property', 'batch-size'), count)
                self.assertEqual(generated.getint('property', 'interval'), 0)
                self.assertEqual(generated.get('class-attrs-all', 'pre-cluster-threshold'), '0.4')
                self.assertEqual(generated.get('class-attrs-0', 'pre-cluster-threshold'), '0.2')
                self.assertIn(f'peoplenet_b{count}_', generated.get('property', 'model-engine-file'))
                self.assertEqual(person_id, 0)

    def test_output_branch_cannot_hold_preroll_and_jpeg_failure_keeps_flow(self):
        elements, received, failures = {}, [], []
        class Element:
            def __init__(self, factory, name):
                self.props, self.callbacks = {}, {}
                elements[name] = self
            def set_property(self, name, value):
                self.props[name] = value
            def connect(self, name, callback):
                self.callbacks[name] = callback
            def add(self, element):
                pass
            def add_pad(self, pad):
                return True
            def get_static_pad(self, name):
                return name
            def link(self, element):
                return True
            def set_state(self, state):
                pass
        namespace = load_preview({'build_output'}, {
            'Gst': NS(Bin=NS(new=lambda name: Element('bin', name)),
                      Caps=NS(from_string=lambda value: value),
                      GhostPad=NS(new=lambda *args: 'pad'), State=NS(NULL='NULL'),
                      FlowReturn=NS(OK='OK', EOS='EOS'), MapFlags=NS(READ=1)),
            'make_element': Element})
        store = NS(put_frame=lambda data, pts: received.append((data, pts)))
        namespace['build_output'](0, NS(jpeg_quality=80), store, failures.append)
        self.assertEqual(elements['queue-0'].props['leaky'], 2)
        self.assertEqual(elements['queue-0'].props['max-size-buffers'], 1)
        self.assertFalse(elements['jpeg-sink-0'].props['async'])
        self.assertFalse(elements['jpeg-sink-0'].props['sync'])
        buffer = NS(pts=100, map=lambda flags: (True, NS(data=b'jpeg')), unmap=lambda info: None)
        appsink = NS(emit=lambda signal: NS(get_buffer=lambda: buffer))
        callback = elements['jpeg-sink-0'].callbacks['new-sample']
        self.assertEqual(callback(appsink), 'OK')
        self.assertEqual(received, [(b'jpeg', 100)])
        buffer.map = lambda flags: (False, None)
        self.assertEqual(callback(appsink), 'OK')
        self.assertEqual(failures, ['JPEG output failed; details hidden'])

    def test_streams_schema_and_http_response_preserved(self):
        import json
        from http.server import BaseHTTPRequestHandler
        from urllib.parse import urlsplit
        manager = self.manager()
        tree = ast.parse((ROOT / 'preview.py').read_text())
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'stream_snapshot')
        namespace = {'args': NS(cameras=list(manager.cameras_by_slot.values())),
                     'runtime_statuses': manager.runtimes, 'runtime_session': 'a' * 32}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'preview.py', 'exec'), namespace)
        snapshot = namespace['stream_snapshot']()
        self.assertEqual(set(snapshot[0]), {'id', 'camera_id', 'floor', 'name', 'format', 'status', 'url',
                                            'runtime', 'runtime_session', 'metadata_path'})
        self.assertEqual([s['url'] for s in snapshot], ['/mjpeg/source0', '/mjpeg/source1'])
        self.assertEqual(set(snapshot[0]['runtime']), {'camera_id', 'state', 'fps', 'last_frame_at',
                         'last_frame_age', 'last_error', 'reconnect_count', 'generation', 'output_frame_age', 'error_reason'})
        handler_class = load_preview({'PreviewHandler'}, {'BaseHTTPRequestHandler': BaseHTTPRequestHandler,
                                                        'urlsplit': urlsplit, 'json': json})['PreviewHandler']
        handler = handler_class.__new__(handler_class)
        handler.path = '/streams.json'
        handler.server = NS(mjpeg_streams={}, stream_snapshot=namespace['stream_snapshot'])
        handler.wfile = io.BytesIO()
        handler.send_response = lambda code: self.assertEqual(code, 200)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.do_GET()
        self.assertEqual(json.loads(handler.wfile.getvalue()), snapshot)
        self.assertNotIn('private', handler.wfile.getvalue().decode())

    def test_constructs_one_gpu_chain_with_n_mux_and_demux_pads(self):
        for count in (1, 2, 4, 8, 25):
            with self.subTest(count=count):
                self.construct(count)

    def test_vehicle_option_adds_one_camera_selected_detector_before_same_tracker(self):
        self.construct(4, vehicle_enabled=True)

    def test_diagnostics_installs_mux_and_gpu_stage_probes_without_source_state_waits(self):
        with patch('startup_timing.gpu_memory', return_value=None):
            self.construct(2, diagnostics=True)

    def construct(self, count, diagnostics=False, vehicle_enabled=False):
        manager = self.manager(count)
        elements, released, links, probes = [], [], [], []

        class Pad:
            def __init__(self, owner, name):
                self.owner, self.name = owner, name
            def add_probe(self, *args):
                probes.append((self.owner, self.name))
            def link(self, pad):
                links.append((self.owner, self.name, pad.owner, pad.name))
                return 'OK'

        class Element:
            def __init__(self, factory, name):
                self.factory, self.name, self.props, self.requests, self.states = factory, name, {}, [], []
                elements.append(self)
            def set_property(self, name, value):
                self.props[name] = value
            def get_property(self, name):
                return self.props[name]
            def get_name(self):
                return self.name
            def get_static_pad(self, name):
                return Pad(self.name, name)
            def request_pad_simple(self, name):
                self.requests.append(name)
                return Pad(self.name, name)
            def release_request_pad(self, pad):
                released.append((self.name, pad.name))
            def add(self, element):
                pass
            def link(self, element):
                return True
            def get_by_name(self, name):
                return self
            def get_bus(self):
                return NS(set_sync_handler=lambda *args: None)
            def set_state(self, state):
                self.states.append(state)
                return 'SUCCESS'

        namespace = {'Gst': NS(Pipeline=NS(new=lambda name: Element('pipeline', name)),
                         Caps=NS(from_string=lambda caps: caps), PadProbeType=NS(BUFFER=1, EVENT_DOWNSTREAM=2),
                         PadLinkReturn=NS(OK='OK'), State=NS(PLAYING='PLAYING', NULL='NULL'),
                         StateChangeReturn=NS(FAILURE='FAILURE'), MessageType=NS(ERROR=1, WARNING=2, EOS=4,
                                                ELEMENT=8, STATE_CHANGED=16), debug_set_active=lambda *args: None,
                         init=lambda *args: None), 'os': NS(environ={}), 'threading': threading,
                     'time': __import__('time'), 'json': __import__('json'),
                     'configure_pgie': configure_pgie,
                     'make_element': Element, 'make_tracker': lambda: Element('nvtracker', 'person-tracker'),
                     'create_preview_source_bin': lambda index, *args, **kwargs: Element('source', f'source-bin-{index}'),
                     'build_output': lambda index, *args: Element('output', f'mjpeg-source-{index}')}
        namespace.update(FrameTiming=FrameTiming, source_observer=lambda *args: lambda *args: None,
                         bus_observer=lambda *args: lambda *args: None,
                         InferenceDiagnostics=lambda: NS(), TRACKER_CONFIG='unused',
                         log_settings=lambda *args: None)
        from vehicle_detection import add_vehicle_rois, prepare_vehicle_tracks
        namespace.update(add_vehicle_rois=add_vehicle_rois, prepare_vehicle_tracks=prepare_vehicle_tracks)
        namespace = load_preview({'run_shared_pipeline', 'CameraFrameSink'}, namespace)
        shutdown = threading.Event()
        shutdown.set()
        args = NS(cameras=list(manager.cameras_by_slot.values()), input=['private'] * count,
                  diagnostics=diagnostics, mux_live_source=1, rtsp_latency=1000, rtsp_drop_on_latency=False,
                  camera_offline_after=10)
        if diagnostics:
            args.startup_timing = StartupTiming(args.cameras, 0, 'test')
        captured = io.StringIO()
        import tempfile
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(captured):
            config = Path(directory) / 'config.txt'
            engine = str(Path(directory) / 'cached.engine')
            config.write_text(f'[property]\nmodel-engine-file={engine}\n')
            if vehicle_enabled:
                args.vehicle_detector = NS(cameras=frozenset({'camera_0'}), config_path=str(config), car_class_id=0)
            error = namespace['run_shared_pipeline'](args, manager.metadata_store, manager.stores, manager,
                                                     shutdown, str(config), 0)
        self.assertIsNone(error)
        pipeline = next(e for e in elements if e.factory == 'pipeline')
        self.assertEqual(pipeline.states, ['PLAYING', 'NULL'])
        self.assertTrue(all(not e.states for e in elements if e.factory == 'source'))
        if diagnostics:
            for owner, pad in (('stream-muxer', 'src'), ('primary-inference', 'sink'),
                               ('primary-inference', 'src'), ('person-tracker', 'sink'),
                               ('person-tracker', 'src')):
                self.assertIn((owner, pad), probes)
            for index in range(count):
                self.assertIn(('stream-muxer', f'sink_{index}'), probes)
                self.assertIn(('stream-demuxer', f'src_{index}'), probes)
        for factory in ('pipeline', 'nvstreammux', 'nvinfer', 'nvtracker', 'nvstreamdemux'):
            expected = 2 if vehicle_enabled and factory == 'nvinfer' else 1
            self.assertEqual(sum(e.factory == factory for e in elements), expected)
        mux = next(e for e in elements if e.factory == 'nvstreammux')
        pgie = next(e for e in elements if e.factory == 'nvinfer')
        self.assertEqual(pgie.props['model-engine-file'], engine)
        demux = next(e for e in elements if e.factory == 'nvstreamdemux')
        self.assertEqual((mux.props['batch-size'], pgie.props['batch-size']), (count, count))
        if vehicle_enabled:
            vehicle = next(e for e in elements if e.name == 'vehicle-inference')
            self.assertEqual(vehicle.props['batch-size'], 1)
            self.assertIn(('vehicle-inference', 'sink'), probes)
            self.assertIn(('vehicle-inference', 'src'), probes)
        self.assertEqual(mux.props['batched-push-timeout'], 40000)
        self.assertFalse(mux.props['sync-inputs'])
        self.assertEqual(mux.requests, [f'sink_{i}' for i in range(count)])
        self.assertEqual(demux.requests, [f'src_{i}' for i in range(count)])
        self.assertEqual(len(released), count * 2)
        self.assertFalse(any(e.factory in ('queue', 'nvvideoconvert', 'capsfilter') for e in elements))
        self.assertEqual((mux.props['width'], mux.props['height']), (1920, 1080))
        for i in range(count):
            self.assertIn((f'source-bin-{i}', 'src', 'stream-muxer', f'sink_{i}'), links)
            self.assertIn(('stream-demuxer', f'src_{i}', f'mjpeg-source-{i}', 'sink'), links)
        self.assertIn(f'sources={count} streammux batch-size={count} pgie batch-size={count}', captured.getvalue())
        self.assertNotIn('private', captured.getvalue())

    def test_bus_source_error_and_eos_do_not_stop_shared_pipeline(self):
        tree = ast.parse((ROOT / 'preview.py').read_text())
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'on_message')
        calls = []
        namespace = {'Gst': NS(MessageType=NS(ERROR=1, WARNING=2, EOS=3, ELEMENT=4, STATE_CHANGED=5)),
                     'message_scope': lambda message: (message.source, 'rtsp_error' if message.source != 'all' else 'pipeline_error'),
                     'stop': lambda *args: calls.append(args),
                     'classify_bus_error': lambda *args, **kwargs: {'reason': 'rtsp_error', 'domain': 'unknown', 'code': None},
                     'manager': NS(record_error_reason=lambda *args: False),
                     'args': NS(diagnostics=False)}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'preview.py', 'exec'), namespace)
        for kind in (1, 3, 4):
            namespace['on_message'](None, NS(type=kind, source='1',
                get_structure=lambda: NS(get_name=lambda: 'GstRTSPSrcTimeout')))
        self.assertEqual(calls, [('rtsp_error', True, '1'), ('source_eos', True, '1'), ('rtsp_timeout', True, '1')])
        namespace['on_message'](None, NS(type=1, source='all'))
        self.assertEqual(calls[-1], ('pipeline_error', True, 'all'))

    def test_bus_ancestor_maps_source_and_output_errors_separately(self):
        tree = ast.parse((ROOT / 'preview.py').read_text())
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'message_scope')
        namespace = {'args': NS(input=[None, None])}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'preview.py', 'exec'), namespace)
        def element(name, parent=None):
            return NS(get_name=lambda: name, get_parent=lambda: parent)
        self.assertEqual(namespace['message_scope'](NS(src=element('rtsp', element('source-bin-1')))),
                         ('1', 'rtsp_error'))
        self.assertEqual(namespace['message_scope'](NS(src=element('encoder', element('mjpeg-source-1')))),
                         ('1', 'pipeline_error'))
        self.assertEqual(namespace['message_scope'](NS(src=element('person-tracker'))),
                         ('all', 'pipeline_error'))


if __name__ == '__main__':
    unittest.main()
