"""CPU-only private configuration resolution tests (native reconnect uses startup URI)."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from camera_config import Camera, CameraConfigError, resolve_camera_rtsp


class ReconnectTests(unittest.TestCase):
    def test_file_removal_does_not_fall_back_to_stale_environment(self):
        camera = Camera(0, 'target', 1, 'Target', 'rtsp://cached.example/video', 'TARGET_RTSP')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            for value in ('', 'TARGET_RTSP=\n', 'TARGET_RTSP=bad-private-value\n'):
                path.write_text(value)
                with self.assertRaises(CameraConfigError) as caught:
                    resolve_camera_rtsp(camera, path, environ={'TARGET_RTSP': 'rtsp://stale.example/video'})
                self.assertNotIn('bad-private-value', str(caught.exception))
            path.unlink()
            with self.assertRaises(CameraConfigError):
                resolve_camera_rtsp(camera, path, environ={'TARGET_RTSP': 'rtsp://stale.example/video'}, require_env_file=True)

    def test_cli_uri_is_preserved_and_file_interpolation_disabled(self):
        camera = Camera(0, 'cli_0', None, 'CLI', 'rtsp://cli.example/video')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text("TARGET_RTSP='rtsp://user:p$word@camera.example/video'\n")
            self.assertEqual(resolve_camera_rtsp(camera, path), camera.rtsp_url)
            named = replace(camera, rtsp_env='TARGET_RTSP')
            self.assertEqual(resolve_camera_rtsp(named, path), 'rtsp://user:p$word@camera.example/video')


if __name__ == '__main__':
    unittest.main()
