import configparser
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from person_metadata import vehicle_from_object, FrameMetadata
from preview_socket import detection_message
from vehicle_detection import (prepare_vehicle_detector, add_vehicle_rois, prepare_vehicle_tracks,
                               VehicleDetector, VEHICLE_TRACKER_CLASS)


def nodes(values):
    node = None
    for value in reversed(values):
        node = NS(data=value, next=node)
    return node


class VehicleTests(unittest.TestCase):
    def test_disabled_does_not_require_model_and_unknown_camera_rejected(self):
        self.assertIsNone(prepare_vehicle_detector([], '.', {}))
        with self.assertRaises(ValueError):
            prepare_vehicle_detector([], '.', {'VEHICLE_CAMERA_IDS': 'unknown'})

    def test_config_is_camera_roi_detector_and_reuses_native_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'model.onnx').write_bytes(b'fake model')
            (root / 'labels.txt').write_text('car\nperson\nsign\nbicycle\n')
            path = root / 'infer.txt'
            path.write_text('[property]\nonnx-file=model.onnx\nlabelfile-path=labels.txt\nnum-detected-classes=4\n')
            captured = {}
            def cache(props, target):
                captured.update(dict(props))
                captured['cache_dir'] = str(target)
                props['model-engine-file'] = str(root / 'vehicle.engine')
                return root, 'fixture'
            with patch('vehicle_detection.prepare_engine_cache', cache):
                detector = prepare_vehicle_detector([NS(camera_id='parking'), NS(camera_id='lobby')], root,
                    {'VEHICLE_CAMERA_IDS': 'parking', 'VEHICLE_INFER_CONFIG': str(path)})
            self.assertEqual(detector.cameras, frozenset({'parking'}))
            self.assertEqual(captured['process-mode'], '2')
            self.assertEqual(captured['operate-on-gie-id'], '90')
            self.assertEqual(captured['gie-unique-id'], '2')
            self.assertEqual(captured['batch-size'], '1')
            self.assertEqual(captured['filter-out-class-ids'], '1;2;3')
            self.assertEqual(captured['cache_dir'], str(root / 'vehicle'))
            parser = configparser.ConfigParser()
            parser.read(detector.config_path)
            self.assertEqual(parser['class-attrs-0']['pre-cluster-threshold'], '0.4')

    def test_coco_car_truck_bus_share_tracker_class_and_config_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'model.onnx').write_bytes(b'fake model')
            (root / 'labels.txt').write_text('person\nbicycle\ncar\nmotorcycle\nairplane\nbus\ntrain\ntruck\n')
            path = root / 'infer.txt'
            path.write_text('[property]\nonnx-file=model.onnx\nlabelfile-path=labels.txt\nnum-detected-classes=8\n'
                            'maintain-aspect-ratio=1\n[class-attrs-all]\npre-cluster-threshold=0.25\n')
            def cache(props, target):
                props['model-engine-file'] = str(root / 'vehicle.engine')
                return root, 'fixture'
            cameras = [NS(camera_id='parking')]
            env = {'VEHICLE_CAMERA_IDS': 'parking', 'VEHICLE_INFER_CONFIG': str(path)}
            with patch('vehicle_detection.prepare_engine_cache', cache):
                detector = prepare_vehicle_detector(cameras, root, env)
                parser = configparser.ConfigParser()
                parser.read(detector.config_path)
                self.assertEqual((detector.car_class_id, detector.vehicle_class_ids), (2, frozenset({2, 5, 7})))
                self.assertEqual(parser['property']['filter-out-class-ids'], '0;1;3;4;6')
                self.assertEqual(parser['property']['maintain-aspect-ratio'], '1')
                for class_id in (2, 5, 7):
                    self.assertEqual(parser[f'class-attrs-{class_id}']['pre-cluster-threshold'], '0.25')
                parser = configparser.ConfigParser()
                parser.read(prepare_vehicle_detector(cameras, root, {**env, 'VEHICLE_MIN_CONFIDENCE': '0.3'}).config_path)
                self.assertEqual(parser['class-attrs-7']['pre-cluster-threshold'], '0.3')
                (root / 'labels.txt').write_text('person\ncar\ncar\n')
                path.write_text(path.read_text().replace('num-detected-classes=8', 'num-detected-classes=3'))
                with self.assertRaises(ValueError):
                    prepare_vehicle_detector(cameras, root, env)
        frame = NS(camera_id='parking', obj_meta_list=None)
        truck, bus, person = NS(unique_component_id=2, class_id=7, parent=1), NS(unique_component_id=2, class_id=5, parent=1), NS(unique_component_id=2, class_id=0, parent=1)
        frame.obj_meta_list = nodes([truck, bus, person])
        removed = []
        pyds = NS(NvDsFrameMeta=NS(cast=lambda v: v), NvDsObjectMeta=NS(cast=lambda v: v),
                  nvds_remove_obj_meta_from_frame=lambda f, obj: removed.append(obj))
        prepare_vehicle_tracks(NS(frame_meta_list=nodes([frame])),
                               VehicleDetector(frozenset({'parking'}), 'unused', 2, frozenset({2, 5, 7})), lambda f: f, pyds)
        self.assertEqual((truck.class_id, bus.class_id), (VEHICLE_TRACKER_CLASS, VEHICLE_TRACKER_CLASS))
        self.assertEqual(removed, [person])

    def test_only_selected_frame_gets_roi_and_parent_removed_before_tracking(self):
        parking = NS(camera_id='parking', bInferDone=True, obj_meta_list=None, pad_index=0, frame_num=1, buf_pts=1)
        lobby = NS(camera_id='lobby', bInferDone=True, obj_meta_list=None)
        batch = NS(frame_meta_list=nodes([parking, lobby]))
        allocated, removed = [], []
        def allocate(batch):
            obj = NS(rect_params=NS())
            allocated.append(obj)
            return obj
        pyds = NS(NvDsFrameMeta=NS(cast=lambda v: v), NvDsObjectMeta=NS(cast=lambda v: v),
                  nvds_acquire_obj_meta_from_pool=allocate,
                  nvds_add_obj_meta_to_frame=lambda frame, obj, parent: setattr(frame, 'obj_meta_list', nodes([obj])),
                  nvds_remove_obj_meta_from_frame=lambda frame, obj: removed.append(obj))
        detector = VehicleDetector(frozenset({'parking'}), 'unused', 0)
        add_vehicle_rois(batch, detector, lambda frame: frame, pyds)
        self.assertEqual(len(allocated), 1)
        self.assertFalse(parking.bInferDone)
        self.assertTrue(lobby.bInferDone)
        self.assertEqual((allocated[0].rect_params.width, allocated[0].rect_params.height), (1920, 1080))
        person = NS(unique_component_id=1, class_id=0)
        car = NS(unique_component_id=2, class_id=0, parent=allocated[0])
        wrong = NS(unique_component_id=2, class_id=1)
        parking.obj_meta_list = nodes([allocated[0], person, car, wrong])
        prepare_vehicle_tracks(batch, detector, lambda frame: frame, pyds)
        self.assertIsNone(car.parent)
        self.assertEqual(car.class_id, VEHICLE_TRACKER_CLASS)
        self.assertEqual(person.class_id, 0)
        self.assertIn(allocated[0], removed)
        self.assertIn(wrong, removed)
        self.assertNotIn(person, removed)

    def test_detached_metadata_retains_native_class_and_lossless_id(self):
        obj = NS(object_id=(1 << 63) + 3, class_id=100, confidence=.9, tracker_confidence=.7,
                 rect_params=NS(left=192, top=108, width=384, height=216))
        vehicle = vehicle_from_object('parking', '2026-01-01T00:00:00Z', obj, 0)
        frame = FrameMetadata(0, 'parking', 1, vehicle.timestamp, None, True, (), vehicles=(vehicle,),
                              vehicle_detection_enabled=True, vehicle_inference_done=True).to_dict()
        obj.rect_params.left = 0
        self.assertEqual(frame['vehicles'][0]['bbox']['x'], 192)
        self.assertEqual(frame['vehicles'][0]['class_id'], 0)
        message = detection_message(frame, 'a' * 32)
        self.assertEqual(message['persons'], [])
        self.assertEqual(message['vehicles'][0]['trackId'], str((1 << 63) + 3))
        self.assertEqual(message['vehicles'][0]['bbox']['x'], .1)


if __name__ == '__main__':
    unittest.main()
