"""Five-second inference summaries independent of PyDS and GStreamer."""

import configparser
from pathlib import Path
import threading
import traceback

import yaml
from person_metadata import valid_confidence
from preview_timing import FrameTiming


def log_safe_traceback(error):
    """Print Python call locations, never exception text, source lines or locals.

    PyYAML exception strings/marks may include the entire offending config line
    and input filename. Only numeric parser coordinates are safe to retain.
    """
    root = Path(__file__).resolve().parent
    print(f"safe_traceback type={type(error).__name__}", flush=True)
    for frame, line in traceback.walk_tb(error.__traceback__):
        path = Path(frame.f_code.co_filename)
        module = frame.f_globals.get("__name__", "")
        if path.parent == root:
            filename = path.name
        elif module == "yaml" or module.startswith("yaml."):
            filename = "yaml/" + path.name
        else:
            filename = "<external>"
        print(f"  at file={filename} function={frame.f_code.co_name} line={line}", flush=True)
    mark = getattr(error, "problem_mark", None)
    if mark is not None:
        print(f"  parser_location line={mark.line + 1} column={mark.column + 1}", flush=True)


def tracker_diagnostic_settings(tracker_path):
    """Read numeric diagnostics from OpenCV or standard YAML, not plugin config.

    NVIDIA presets use OpenCV's %YAML:1.0 header, which is not a valid PyYAML
    directive. Blank just that first line in memory, retaining parser line
    numbers. nvtracker receives the original unmodified file via ll-config-file.
    """
    with open(tracker_path, encoding="utf-8-sig") as source:
        text = source.read()
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == "%YAML:1.0":
        lines[0] = "\n"
    tracker = yaml.safe_load("".join(lines))
    if not isinstance(tracker, dict):
        raise ValueError("Tracker diagnostics require a mapping")
    return {section: {key: value for key, value in values.items()
                      if isinstance(value, (int, float, bool))}
            for section, values in tracker.items() if isinstance(values, dict)}


def log_settings(pgie_path, person_class_id, tracker_path):
    # Diagnostics are optional: their parser must not decide pipeline validity.
    try:
        _log_settings(pgie_path, person_class_id, tracker_path)
    except (OSError, UnicodeError, configparser.Error, yaml.YAMLError, ValueError, KeyError) as error:
        print("diagnostics settings unavailable; details hidden", flush=True)
        log_safe_traceback(error)


def _log_settings(pgie_path, person_class_id, tracker_path):
    config = configparser.ConfigParser(interpolation=None)
    config.read(pgie_path)
    keys = ("pre-cluster-threshold", "post-cluster-threshold", "nms-iou-threshold", "topk")
    effective = dict(config["class-attrs-all"]) if config.has_section("class-attrs-all") else {}
    effective.update(dict(config[f"class-attrs-{person_class_id}"]) if config.has_section(f"class-attrs-{person_class_id}") else {})
    props = config["property"]
    print(f"diagnostics PGIE interval={props.get('interval')} batch-size={props.get('batch-size')} "
          f"cluster-mode={props.get('cluster-mode')} person-class-id={person_class_id} "
          f"thresholds={ {key: effective.get(key, 'SDK default') for key in keys} }", flush=True)
    # Print the preset's actual numeric switches/thresholds, excluding paths.
    settings = tracker_diagnostic_settings(tracker_path)
    print(f"diagnostics NvDCF size=640x384 settings={settings}", flush=True)


class InferenceDiagnostics:
    def __init__(self):
        self.timing = FrameTiming()
        self._lock = threading.Lock()
        self._frames = self._inferred = self._detected = self._count = 0
        self._minimum = self._maximum = None

    def observe(self, inference_done, confidences, pts_ns):
        if inference_done:
            self.timing.observe(pts_ns)
        with self._lock:
            self._frames += 1
            self._inferred += bool(inference_done)
            self._detected += len(confidences)
            for confidence in confidences:
                confidence = valid_confidence(confidence)
                if confidence is not None:
                    self._count += 1
                    self._minimum = confidence if self._minimum is None else min(self._minimum, confidence)
                    self._maximum = confidence if self._maximum is None else max(self._maximum, confidence)

    def snapshot(self):
        with self._lock:
            result = {"pgie_frames": self._frames, "inferred_frames": self._inferred,
                      "non_inferred_frames": self._frames - self._inferred,
                      "person_detections": self._detected, "confidence_count": self._count,
                      "confidence_min": self._minimum, "confidence_max": self._maximum}
            self._frames = self._inferred = self._detected = self._count = 0
            self._minimum = self._maximum = None
        result["inference_frame_timing"] = self.timing.snapshot()
        return result
