"""GPU training worker for the ReTrace web training page (/training).

ReTrace web -> Backend API (job in PostgreSQL) -> this worker -> train.py / evaluate.py subprocess.

Runs on the GPU host with the existing training venv (no new packages, standard library only
plus the modules train.py already uses) and talks to the Backend over loopback HTTP with a
shared worker token. It never receives a path, a command line or a model file from a user:

* the dataset is the immutable export snapshot the Backend froze at job creation, downloaded
  and checked against its recorded sha256, then extracted with path/size/count limits;
* base weights are either training/weights/yolo11m.pt or best.pt of an earlier job, both
  verified against the sha256 the Backend registered;
* train.py/evaluate.py/autolabel.py are started with an argument list built from re-validated
  integers and enums (no shell).
* auto-label jobs (config.kind = 'autolabel') download the target JPEGs of the job from the
  Backend, run autolabel.py on the GPU and post the detections back in batches; the Backend
  decides per image (row lock + revision) whether a draft may be written.

One job at a time. Before starting it waits until the shared GPU has the free memory the
Backend asked for (DeepStream keeps its share) and reports GPU samples every few seconds.
The Backend answers each heartbeat with a stop request (user stop or production protection);
the worker then asks train.py to stop between batches, and escalates to SIGTERM/SIGKILL after
a grace period. When the worker dies, the training child gets SIGTERM (PR_SET_PDEATHSIG) and
the Backend marks the job failed on the next worker start or when heartbeats stop.

Usage (see README "웹 학습 Worker"):
    RETRACE_TRAINING_WORKER_TOKEN=... .venv/bin/python worker.py --backend http://127.0.0.1:8000
"""
import argparse
import csv
import ctypes
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
BASE_WEIGHTS = ROOT / "weights" / "yolo11m.pt"
SPLITS = ("train", "val", "test")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
JOB_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Extraction limits for a dataset snapshot (exports hold at most 5000 images).
MAX_MEMBERS = 12_000
MAX_MEMBER_BYTES = 64 * 2**20
MAX_TOTAL_BYTES = 40 * 2**30
MAX_ZIP_BYTES = 40 * 2**30
MAX_WEIGHTS_BYTES = 1 * 2**30
# Auto labelling: dataset images are at most 20 MiB (Backend capture limit); results go up 50 per request.
MAX_IMAGE_BYTES = 20 * 2**20
AUTOLABEL_MAX_IMAGES = 500
AUTOLABEL_BATCH = 50
AUTOLABEL_CLASS_MODES = ("coco80", "vehicle3", "single")
# Uploaded to the Backend after a run (name -> local path relative to the job directory).
ARTIFACTS = {
    "best.pt": "train/weights/best.pt", "last.pt": "train/weights/last.pt",
    "results.csv": "train/results.csv", "results.png": "train/results.png", "args.yaml": "train/args.yaml",
    "run_info.json": "train/run_info.json", "train.log": "train.log", "check_report.json": "check_report.json",
    "eval_metrics.json": "eval/metrics.json", "eval_predictions.json": "eval/predictions.json",
    "eval_report.md": "eval/report.md",
}


class WorkerError(Exception):
    """A job failure with a stable code the web page explains."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class StopRequested(Exception):
    pass


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Backend:
    def __init__(self, base_url, token, timeout=15):
        self.base, self.token, self.timeout = base_url.rstrip("/"), token, timeout
        # Loopback/LAN Backend only: never route the worker token through a proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method, path, body=None, timeout=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as response:
                raw = response.read(8 * 2**20)
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read(65536) or b"{}").get("detail")
            except ValueError:
                detail = None
            return error.code, {"detail": detail}

    def download(self, path, target, max_bytes, expected_sha256=None):
        req = urllib.request.Request(self.base + path, method="GET")
        req.add_header("Authorization", f"Bearer {self.token}")
        partial = target.with_name(target.name + ".partial")
        digest, size = hashlib.sha256(), 0
        try:
            with self.opener.open(req, timeout=60) as response, open(partial, "wb") as out:
                for chunk in iter(lambda: response.read(1 << 20), b""):
                    size += len(chunk)
                    if size > max_bytes:
                        raise WorkerError("dataset_too_large", "Download exceeds the size limit.")
                    digest.update(chunk)
                    out.write(chunk)
        except urllib.error.HTTPError as error:
            partial.unlink(missing_ok=True)
            raise WorkerError("download_failed", f"Backend refused the download (HTTP {error.code}).") from None
        except (OSError, urllib.error.URLError):
            partial.unlink(missing_ok=True)
            raise WorkerError("download_failed", "Could not download the file from the Backend.") from None
        if expected_sha256 and digest.hexdigest() != expected_sha256:
            partial.unlink(missing_ok=True)
            raise WorkerError("snapshot_changed", "Downloaded file does not match its recorded sha256.")
        partial.replace(target)
        return digest.hexdigest()

    def upload(self, path, name, job_id):
        size = path.stat().st_size
        with open(path, "rb") as handle:
            req = urllib.request.Request(f"{self.base}/api/training/worker/jobs/{job_id}/artifacts/{name}",
                                         data=handle, method="PUT")
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("Content-Type", "application/octet-stream")
            req.add_header("Content-Length", str(size))
            try:
                with self.opener.open(req, timeout=300) as response:
                    return response.status, json.loads(response.read(65536) or b"{}")
            except urllib.error.HTTPError as error:
                try:
                    return error.code, json.loads(error.read(65536) or b"{}")
                except ValueError:
                    return error.code, {}


def gpu_sample(index, child_pids=()):
    """nvidia-smi on the host; DeepStream's usage is part of memory.used."""
    try:
        out = subprocess.run(["nvidia-smi", f"--id={index}",
                              "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10, check=True).stdout
        name, total, used, util, temp = [v.strip() for v in out.strip().split(",")]
        sample = {"available": True, "index": index, "name": name, "total_mib": int(total), "used_mib": int(used),
                  "free_mib": int(total) - int(used), "util": int(util),
                  "temperature": int(temp) if temp.isdigit() else None}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"available": False, "index": index, "error": "nvidia-smi failed or the NVIDIA driver is unavailable"}
    if child_pids:
        try:
            apps = subprocess.run(["nvidia-smi", f"--id={index}", "--query-compute-apps=pid,used_memory",
                                   "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout
            sample["training_mib"] = sum(int(mem) for pid, mem in (line.split(",") for line in apps.splitlines() if "," in line)
                                         if int(pid) in child_pids)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return sample


SAFE_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
DATA_FILE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}\.(?:jpg|jpeg|png|txt)", re.IGNORECASE)


def allowed_member(name):
    """Only the export layout (optionally inside one wrapping folder); no '..', absolute or odd names."""
    if "\\" in name or name.startswith("/") or "\0" in name:
        return False
    parts = [part for part in PurePosixPath(name).parts]
    if not parts or any(not SAFE_PART.fullmatch(part) for part in parts):
        return False
    if parts[0] not in ("images", "labels", "data.yaml", "manifest.json"):
        parts = parts[1:]  # dataset/ wrapping folder tolerated by check_dataset.find_dataset_root
    if not parts:
        return name.endswith("/")
    if parts in (["data.yaml"], ["manifest.json"]):
        return not name.endswith("/")
    if parts[0] not in ("images", "labels") or len(parts) > 3:
        return False
    if len(parts) == 1:
        return name.endswith("/")
    if parts[1] not in SPLITS:
        return False
    if len(parts) == 2:
        return name.endswith("/")
    suffix_ok = parts[2].lower().endswith(".txt") == (parts[0] == "labels")
    return DATA_FILE.fullmatch(parts[2]) is not None and suffix_ok and not name.endswith("/")


def safe_extract(zip_path, dest):
    """Extract a ReTrace export without trusting member names, sizes, types or count."""
    tmp = dest.with_name(dest.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            raise WorkerError("dataset_invalid", "Dataset archive has too many files.")
        total = 0
        for member in members:
            name = member.filename
            if not allowed_member(name):
                raise WorkerError("dataset_invalid", f"Unsafe or unexpected path in the dataset archive: {name[:120]!r}")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise WorkerError("dataset_invalid", "Dataset archive contains a symbolic link.")
            if member.file_size > MAX_MEMBER_BYTES:
                raise WorkerError("dataset_invalid", f"Dataset file too large: {name[:120]!r}")
            total += member.file_size
            if total > MAX_TOTAL_BYTES:
                raise WorkerError("dataset_invalid", "Dataset archive expands beyond the size limit.")
        for member in members:
            target = (tmp / member.filename).resolve()
            if not target.is_relative_to(tmp.resolve()):
                raise WorkerError("dataset_invalid", "Unsafe path in the dataset archive.")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with archive.open(member) as source, open(target, "wb") as out:
                for chunk in iter(lambda: source.read(1 << 20), b""):
                    written += len(chunk)
                    if written > member.file_size or written > MAX_MEMBER_BYTES:  # lying header
                        raise WorkerError("dataset_invalid", "Dataset archive member is larger than declared.")
                    out.write(chunk)
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    return dest


def read_results(path):
    """Ultralytics results.csv (one row per finished epoch) -> list of {column: float}."""
    if not path.is_file():
        return []
    rows = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            parsed = {}
            for key, value in row.items():
                try:
                    parsed[key.strip()] = float(value)
                except (TypeError, ValueError):
                    continue
            if "epoch" in parsed:
                rows.append(parsed)
    return rows


def classify_failure(log_text, returncode):
    text = log_text.lower()
    if "cuda is not available" in text:
        return "cuda_unavailable", "CUDA is not available on the training host; training never runs on CPU."
    if "out of memory" in text or "cublas_status_alloc_failed" in text:
        return "gpu_oom", "GPU memory ran out during training. Lower the batch size or image size and try again."
    if "mib free" in text and "not starting" in text:
        return "gpu_busy", "Not enough free GPU memory when training started; production inference keeps its share."
    if "no space left on device" in text:
        return "storage_full", "The training host ran out of disk space while saving results."
    if "dataset errors" in text or "val split is empty" in text:
        return "dataset_invalid", "The dataset failed validation inside train.py. See the log."
    if returncode is not None and returncode < 0:
        return "killed", f"The training process was terminated by signal {-returncode}."
    return "training_failed", f"Training exited with code {returncode}. See the log for the cause."


def read_lines(path, offset):
    """Complete new lines of a growing file since `offset` (untruncated, for JSON lines)."""
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read(64 * 2**20)
    except OSError:
        return [], offset
    cut = data.rfind(b"\n") + 1
    return [line for line in data[:cut].decode("utf-8", "replace").split("\n") if line], offset + cut


def set_parent_death_signal():
    # Linux: the training child receives SIGTERM if this worker dies, so no GPU job runs unmonitored.
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except OSError:
        pass


def validated_config(spec):
    """Re-validate the Backend's job config; only integers and enums reach the command line."""
    config = spec["config"]
    def integer(name, low, high):
        value = config[name]
        if type(value) is not int or not low <= value <= high:
            raise WorkerError("invalid_config", f"Invalid {name} in the job configuration.")
        return value
    if config.get("imgsz") not in (640, 960):
        raise WorkerError("invalid_config", "Invalid image size in the job configuration.")
    if not JOB_ID.fullmatch(str(spec.get("id", ""))):
        raise WorkerError("invalid_config", "Invalid job id.")
    if config.get("kind") == "deploy":
        if config.get("action") not in ("deploy", "rollback"):
            raise WorkerError("invalid_config", "Invalid deployment action.")
        if config["action"] == "deploy" and (config.get("class_mode") not in ("vehicle3", "single")
                                             or config["imgsz"] != 640):
            raise WorkerError("invalid_config", "Only 640px COCO80 or 1-class car models can be deployed.")
        return {"kind": "deploy", "action": config["action"], "class_mode": config.get("class_mode", "vehicle3")}
    if config.get("kind") == "autolabel":
        conf = config.get("conf")
        if type(conf) not in (int, float) or not 0.1 <= conf <= 0.9:
            raise WorkerError("invalid_config", "Invalid confidence threshold in the job configuration.")
        if config["imgsz"] != 640 or config.get("class_mode") not in AUTOLABEL_CLASS_MODES:
            raise WorkerError("invalid_config", "Invalid image size or class mode in the job configuration.")
        targets = config.get("targets")
        if (not isinstance(targets, list) or not 1 <= len(targets) <= AUTOLABEL_MAX_IMAGES
                or any(not isinstance(t, dict) or not isinstance(t.get("id"), str) or not JOB_ID.fullmatch(t["id"])
                       for t in targets)):
            raise WorkerError("invalid_config", "Invalid auto-label image list in the job configuration.")
        ids = [t["id"] for t in targets]
        if len(set(ids)) != len(ids):
            raise WorkerError("invalid_config", "Duplicate auto-label image in the job configuration.")
        return {"kind": "autolabel", "conf": float(conf), "imgsz": 640, "class_mode": config["class_mode"],
                "targets": ids}
    if config.get("kind", "train") == "evaluate":
        ids = config.get("eval_image_ids")
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 5000
                or any(not isinstance(i, str) or not JOB_ID.fullmatch(i) for i in ids)):
            raise WorkerError("invalid_config", "Invalid evaluation image list in the job configuration.")
        return {"kind": "evaluate", "imgsz": config["imgsz"], "eval_image_ids": ids}
    if config.get("kind", "train") != "train":
        raise WorkerError("invalid_config", "Invalid job kind in the job configuration.")
    if config.get("class_mode") not in ("vehicle3", "single"):
        raise WorkerError("invalid_config", "Invalid class mode in the job configuration.")
    train_mode = config.get("train_mode", "standard")
    if train_mode not in ("standard", "all") or (train_mode == "all" and config["class_mode"] != "vehicle3"):
        raise WorkerError("invalid_config", "Invalid training mode in the job configuration.")
    return {"kind": "train", "epochs": integer("epochs", 1, 300), "imgsz": config["imgsz"],
            "batch": integer("batch", 1, 32), "seed": integer("seed", 0, 2**31 - 1),
            "patience": integer("patience", 1, 300), "class_mode": config["class_mode"],
            "smoke": config.get("smoke") is True, "train_mode": train_mode}


class Worker:
    def __init__(self, args, backend):
        self.args, self.backend = args, backend
        self.work_dir = args.work_dir.resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.worker_id = f"{socket.gethostname()[:40]}-{os.getpid()}-{uuid4().hex[:8]}"
        self.shutdown = False
        self.base_cache = None

    # ----- reporting ---------------------------------------------------------------------
    def base_weights(self):
        if not BASE_WEIGHTS.is_file():
            return {"present": False}
        st = BASE_WEIGHTS.stat()
        key = (st.st_size, st.st_mtime_ns)
        if not self.base_cache or self.base_cache[0] != key:
            self.base_cache = (key, sha256_file(BASE_WEIGHTS))
        return {"present": True, "sha256": self.base_cache[1], "size": st.st_size}

    def heartbeat(self, job=None, started=False, child_pids=()):
        disk = shutil.disk_usage(self.work_dir)
        body = {"worker_id": self.worker_id, "started": started, "gpu": gpu_sample(self.args.gpu, child_pids),
                "base_weights": self.base_weights(), "disk_free_mib": disk.free // 2**20, "job": job}
        try:
            from deployment import public_status
            body["deployment"] = public_status()
        except Exception:  # noqa: BLE001 - deployment status must never stop training heartbeats
            body["deployment"] = {}
        try:
            status, data = self.backend.request("POST", "/api/training/worker/heartbeat", body)
        except (OSError, urllib.error.URLError, ValueError):
            return {}  # Backend restart: keep training, report again on the next beat
        if status == 401:
            raise SystemExit("Backend rejected the worker token (TRAINING_WORKER_TOKEN mismatch).")
        return (data or {}) if status == 200 else {}

    def finish(self, job_id, result):
        result = {**result, "worker_id": self.worker_id}
        pending = self.work_dir / job_id / "final.json"
        pending.write_text(json.dumps(result))
        self.submit_final(job_id, pending)

    def submit_final(self, job_id, pending):
        for attempt in range(30):
            try:
                status, _ = self.backend.request("POST", f"/api/training/worker/jobs/{job_id}/finish",
                                                 json.loads(pending.read_text()))
            except (OSError, urllib.error.URLError, ValueError):
                status = None
            if status in (200, 404, 409):  # recorded, or the job no longer accepts this result
                pending.replace(pending.with_name("final.sent.json"))
                return True
            time.sleep(min(60, 2 ** attempt))
        return False  # resubmitted on the next worker start

    def resubmit_pending(self):
        for pending in self.work_dir.glob("*/final.json"):
            if JOB_ID.fullmatch(pending.parent.name):
                self.submit_final(pending.parent.name, pending)

    # ----- job steps ---------------------------------------------------------------------
    def check_stop(self, job, phase, message):
        reply = self.heartbeat({**job, "phase": phase, "message": message})
        if reply.get("stop") or self.shutdown:
            raise StopRequested(reply.get("stop_reason") or "Worker is shutting down.")

    def prepare_dataset(self, spec, job_dir, job, require_val=True):
        self.check_stop(job, "preparing", "Downloading the frozen dataset snapshot")
        zip_path = job_dir / "dataset.zip"
        if not (zip_path.is_file() and sha256_file(zip_path) == spec["dataset_sha256"]):
            self.backend.download(f"/api/training/worker/jobs/{spec['id']}/dataset", zip_path,
                                  MAX_ZIP_BYTES, spec["dataset_sha256"])
        self.check_stop(job, "validating", "Extracting and validating the dataset")
        import check_dataset  # needs Pillow/PyYAML from the training venv
        root = check_dataset.find_dataset_root(safe_extract(zip_path, job_dir / "dataset"))
        report = check_dataset.check(root)
        (job_dir / "check_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        if report["errors"]:
            raise WorkerError("dataset_invalid", "Dataset validation failed: " + "; ".join(report["errors"][:5]))
        if require_val and report["info"]["images"]["val"] == 0:
            raise WorkerError("no_validation", "The dataset has no validation images from an independent session.")
        return root, report

    def resolve_weights(self, spec, job_dir):
        model = spec["base_model"]
        if model["kind"] == "base":
            if not BASE_WEIGHTS.is_file():
                raise WorkerError("base_model_missing", "training/weights/yolo11m.pt is missing on the training host.")
            if model.get("sha256") and self.base_weights()["sha256"] != model["sha256"]:
                raise WorkerError("base_model_changed", "training/weights/yolo11m.pt differs from the registered base model.")
            return BASE_WEIGHTS
        source_job = str(model.get("job_id", ""))
        if not JOB_ID.fullmatch(source_job) or not model.get("sha256"):
            raise WorkerError("base_model_missing", "The selected model has no verified weights.")
        local = self.work_dir / source_job / "train" / "weights" / "best.pt"
        if local.is_file() and sha256_file(local) == model["sha256"]:
            return local
        target = job_dir / "base_best.pt"
        self.backend.download(f"/api/training/worker/models/{model['id']}/weights", target, MAX_WEIGHTS_BYTES, model["sha256"])
        return target

    def wait_for_gpu(self, spec, job):
        required, waited = int(spec["required_free_mib"]), 0
        max_util = int(spec.get("max_gpu_util", 100))
        while True:
            sample = gpu_sample(self.args.gpu)
            if not sample.get("available"):
                raise WorkerError("cuda_unavailable", "The NVIDIA driver/GPU is not available on the training host.")
            if sample["free_mib"] >= required and sample["util"] <= max_util:
                return sample
            if waited >= int(spec.get("gpu_wait_seconds", 1800)):
                raise WorkerError("gpu_busy", f"GPU stayed busy: {sample['free_mib']} MiB free (needs {required}), "
                                              f"{sample['util']}% util (limit {max_util}%).")
            self.check_stop(job, "waiting_gpu", f"Waiting for GPU: {sample['free_mib']} MiB free, needs {required}; "
                                                f"util {sample['util']}% (limit {max_util}%)")
            time.sleep(self.args.poll)
            waited += self.args.poll

    def run_child(self, cmd, job_dir, job, phase, progress_file=None, stop_file=None, grace=None, on_tick=None,
                  message=None):
        """Run train.py/evaluate.py/autolabel.py, heartbeat with progress, honour stop requests.

        on_tick(progress) runs once per poll (before the heartbeat) and after the child exits; it may
        return a message for the heartbeat (auto labelling uploads finished batches there).
        """
        log_path = job_dir / "train.log"
        with open(log_path, "ab") as log:
            log.write(f"\n$ {' '.join(Path(c).name if i < 2 else str(c) for i, c in enumerate(cmd))}\n".encode())
            log.flush()
            proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    env={**os.environ, "PYTHONUNBUFFERED": "1"}, start_new_session=True,
                                    preexec_fn=set_parent_death_signal)
        offset = log_path.stat().st_size
        stop_reason, stop_at, term_at, started = None, None, None, time.time()
        sent_rows = 0
        while True:
            try:
                proc.wait(timeout=self.args.poll)
            except subprocess.TimeoutExpired:
                pass
            lines, offset = self.new_log_lines(log_path, offset)
            rows = read_results(job_dir / "train" / "results.csv") if phase == "training" else []
            progress = {}
            if progress_file and progress_file.is_file():
                try:
                    progress = json.loads(progress_file.read_text())
                except ValueError:
                    progress = {}
            note = on_tick(progress) if on_tick else None
            update = {**job, "phase": phase, "message": (stop_reason and f"Stopping: {stop_reason}") or note or message,
                      "progress": progress, "metrics_rows": rows[sent_rows:], "metrics_offset": sent_rows,
                      "log_lines": lines, "elapsed": round(time.time() - started)}
            reply = self.heartbeat(update, child_pids={proc.pid} if proc.poll() is None else ())
            sent_rows = len(rows) if reply else sent_rows
            if proc.poll() is not None:
                return proc.returncode, stop_reason
            if (reply.get("stop") or self.shutdown) and stop_reason is None:
                stop_reason = reply.get("stop_reason") or "Worker is shutting down."
                stop_at = time.time()
                if stop_file:
                    stop_file.touch()
                else:
                    os.killpg(proc.pid, signal.SIGTERM)
                    term_at = time.time()
            if stop_at and term_at is None and time.time() - stop_at > (grace or self.args.stop_grace):
                with open(log_path, "ab") as log:
                    log.write(b"\nGraceful stop timed out; sending SIGTERM.\n")
                os.killpg(proc.pid, signal.SIGTERM)
                term_at = time.time()
            if term_at and time.time() - term_at > 30:
                os.killpg(proc.pid, signal.SIGKILL)

    @staticmethod
    def new_log_lines(path, offset, limit=200):
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(2 * 2**20)
        except OSError:
            return [], offset
        cut = data.rfind(b"\n") + 1  # complete lines only
        text = data[:cut].decode("utf-8", "replace")
        lines = [ANSI.sub("", part).strip()[:500] for line in text.split("\n") for part in line.split("\r")[-1:]]
        return [line for line in lines if line][-limit:], offset + cut

    def upload_artifacts(self, job_id, job_dir):
        uploaded = []
        for name, relative in ARTIFACTS.items():
            path = job_dir / relative
            if not path.is_file():
                continue
            try:
                status, data = self.backend.upload(path, name, job_id)
            except (OSError, urllib.error.URLError):
                raise WorkerError("artifact_upload_failed", f"Could not reach the Backend to store {name}.") from None
            if status == 200:
                uploaded.append(data)
            elif status == 507:
                raise WorkerError("storage_full", "The Backend has no space left to store the training results.")
            else:
                raise WorkerError("artifact_upload_failed", f"Could not store {name} on the Backend (HTTP {status}).")
        return uploaded

    def run_job(self, spec):
        job_id = spec["id"]
        job_dir = self.work_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job = {"id": job_id}
        result = {"status": "failed"}
        try:
            config = validated_config(spec)
            if shutil.disk_usage(self.work_dir).free // 2**20 < self.args.min_disk_mib:
                raise WorkerError("storage_full", f"Less than {self.args.min_disk_mib} MiB free on the training host.")
            if config["kind"] == "deploy":
                from deployment import run_deployment
                result = run_deployment(self, spec, job_dir, job, WorkerError)
            elif config["kind"] == "evaluate":
                result = self.run_evaluation(spec, config, job_dir, job)
            elif config["kind"] == "autolabel":
                result = self.run_autolabel(spec, config, job_dir, job)
            else:
                result = self.run_training(spec, config, job_dir, job)
            self.heartbeat({**job, "phase": "saving", "message": "Uploading results"})
            result["artifacts"] = self.upload_artifacts(job_id, job_dir)
        except StopRequested as stop:
            result = {**result, "status": "stopped", "error": str(stop)}
            try:
                result["artifacts"] = self.upload_artifacts(job_id, job_dir)
            except WorkerError:
                pass
        except WorkerError as error:
            result = {**result, "status": "failed", "error_code": error.code, "error": error.message}
        except Exception as error:  # noqa: BLE001 - record any unexpected failure, keep the worker alive
            result = {**result, "status": "failed", "error_code": "worker_error",
                      "error": f"Unexpected worker error ({error.__class__.__name__}). See the worker log."}
            print(f"job {job_id}: {error!r}", file=sys.stderr, flush=True)
        self.finish(job_id, result)

    def run_training(self, spec, config, job_dir, job):
        train_all = config["train_mode"] == "all"
        root, report = self.prepare_dataset(spec, job_dir, job, require_val=not train_all)
        weights = self.resolve_weights(spec, job_dir)
        gpu_before = self.wait_for_gpu(spec, job)
        progress_file, stop_file = job_dir / "progress.json", job_dir / "stop.request"
        stop_file.unlink(missing_ok=True)
        cmd = [sys.executable, str(ROOT / "train.py"), "--data", str(root), "--model", str(weights),
               "--epochs", str(config["epochs"]), "--imgsz", str(config["imgsz"]), "--batch", str(config["batch"]),
               "--device", str(self.args.gpu), "--seed", str(config["seed"]), "--patience", str(config["patience"]),
               "--classes", "coco80" if config["class_mode"] == "vehicle3" else "single",
               "--project", str(job_dir), "--name", "train", "--min-free-mib", str(int(spec["required_free_mib"])),
               "--workers", str(self.args.dataloader_workers),
               "--progress-file", str(progress_file), "--stop-file", str(stop_file)]
        if config["smoke"]:
            cmd.append("--smoke")
        if train_all:
            cmd.append("--train-all")
        returncode, stop_reason = self.run_child(cmd, job_dir, job, "training", progress_file, stop_file)
        log_text = (job_dir / "train.log").read_text("utf-8", "replace")[-200_000:]
        best = job_dir / "train" / "weights" / "best.pt"
        result = {"gpu_before": gpu_before, "metrics_rows": read_results(job_dir / "train" / "results.csv"),
                  "dataset_report": {"info": report["info"], "warnings": report["warnings"][:50]}}
        if (job_dir / "train" / "run_info.json").is_file():
            result["run_info"] = json.loads((job_dir / "train" / "run_info.json").read_text())
        if stop_reason:
            result.update(status="stopped", error=stop_reason)
        elif returncode != 0:
            code, message = classify_failure(log_text, returncode)
            result.update(status="failed", error_code=code, error=message)
        elif not best.is_file():
            result.update(status="failed", error_code="model_missing", error="Training finished but best.pt was not created.")
        else:
            result["status"] = "completed"
            if train_all:
                result["evaluation"] = {"status": "skipped", "reason": "Train All: every image of the dataset "
                                        "was used for training, so no independent test exists. Evaluate this "
                                        "model on newly captured CCTV images."}
            elif report["info"]["images"]["test"] and not config["smoke"]:
                result["evaluation"] = self.evaluate(spec, config, job_dir, job, root, best, weights)
            else:
                result["evaluation"] = {"status": "skipped", "reason": "No independent test images; "
                                        "improvement cannot be measured." if not report["info"]["images"]["test"]
                                        else "Smoke run: evaluation skipped."}
        return result

    def run_evaluation(self, spec, config, job_dir, job):
        """Evaluation job: a registered model vs the COCO base on images it never trained on."""
        root, _ = self.prepare_dataset(spec, job_dir, job, require_val=False)
        import dataset_views
        view, missing = dataset_views.subset_view(root, config["eval_image_ids"])
        if missing:
            raise WorkerError("dataset_invalid", f"{len(missing)} selected evaluation image(s) are not in the dataset snapshot.")
        weights = self.resolve_weights(spec, job_dir)
        if not BASE_WEIGHTS.is_file():
            raise WorkerError("base_model_missing", "training/weights/yolo11m.pt is missing on the training host.")
        gpu_before = self.wait_for_gpu(spec, job)
        out = job_dir / "eval"
        out.mkdir(exist_ok=True)
        cmd = [sys.executable, str(ROOT / "evaluate.py"), "--data", str(view), "--split", "test", "--eval-only",
               "--baseline", str(BASE_WEIGHTS), "--model", f"new={weights}", "--imgsz", str(config["imgsz"]),
               "--device", str(self.args.gpu), "--out", str(out), "--predictions-json", str(out / "predictions.json"),
               "--max-images", "0"]
        returncode, stop_reason = self.run_child(cmd, job_dir, job, "evaluating", grace=30)
        result = {"gpu_before": gpu_before}
        if stop_reason:
            return {**result, "status": "stopped", "error": stop_reason}
        if returncode != 0 or not (out / "metrics.json").is_file():
            log_text = (job_dir / "train.log").read_text("utf-8", "replace")[-200_000:]
            code, message = classify_failure(log_text, returncode)
            return {**result, "status": "failed", "error_code": "evaluation_failed" if code == "training_failed" else code,
                    "error": message if code != "training_failed" else "evaluate.py failed; see the log."}
        return {**result, "status": "completed",
                "evaluation": {"status": "completed", "metrics": json.loads((out / "metrics.json").read_text())}}

    # ----- auto labelling ----------------------------------------------------------------
    def download_images(self, spec, config, job_dir, job):
        """Target JPEGs -> job_dir/images/<id>.jpg; images deleted meanwhile are skipped, not fatal."""
        images_dir = job_dir / "images"
        images_dir.mkdir(exist_ok=True)
        present, missing = [], []
        for number, image_id in enumerate(config["targets"], 1):
            target = images_dir / f"{image_id}.jpg"
            if not target.is_file():
                try:
                    self.backend.download(f"/api/training/worker/jobs/{spec['id']}/images/{image_id}", target,
                                          MAX_IMAGE_BYTES)
                except WorkerError as error:
                    if error.code not in ("download_failed", "dataset_too_large"):
                        raise
                    missing.append(image_id)
                    continue
            present.append(target)
            if number % 25 == 0:
                self.check_stop(job, "preparing", f"Downloading images {number}/{len(config['targets'])}")
        if not present:
            raise WorkerError("download_failed", "None of the target images could be downloaded from the Backend.")
        return present, missing

    def upload_autolabels(self, job_id, results):
        """POST detections in batches of AUTOLABEL_BATCH; returns the Backend's latest summary."""
        summary = None
        for start in range(0, len(results), AUTOLABEL_BATCH):
            batch = results[start:start + AUTOLABEL_BATCH]
            try:
                status, data = self.backend.request("POST", f"/api/training/worker/jobs/{job_id}/autolabels",
                                                    {"worker_id": self.worker_id, "results": batch}, timeout=60)
            except (OSError, urllib.error.URLError, ValueError):
                raise WorkerError("artifact_upload_failed", "Could not reach the Backend to store auto labels.") from None
            if status != 200:
                raise WorkerError("artifact_upload_failed", f"Backend refused the auto-label results (HTTP {status}).")
            summary = data
        return summary

    def run_autolabel(self, spec, config, job_dir, job):
        """Auto-label job: draft boxes on the job's target images with the selected model."""
        weights = self.resolve_weights(spec, job_dir)
        self.check_stop(job, "preparing", "Downloading dataset images")
        images, missing = self.download_images(spec, config, job_dir, job)
        gpu_before = self.wait_for_gpu(spec, job)
        list_file, out_file, progress_file = job_dir / "images.txt", job_dir / "autolabels.jsonl", job_dir / "progress.json"
        list_file.write_text("\n".join(str(path) for path in images) + "\n")
        out_file.unlink(missing_ok=True)
        cmd = [sys.executable, str(ROOT / "autolabel.py"), "--weights", str(weights), "--images", str(list_file),
               "--out", str(out_file), "--imgsz", str(config["imgsz"]), "--conf", f"{config['conf']:.3f}",
               "--device", str(self.args.gpu), "--class-mode", config["class_mode"], "--progress-file", str(progress_file)]
        state = {"offset": 0, "pending": [], "sent": 0, "summary": None, "total": len(images)}

        def flush(force=False):
            while state["pending"] and (force or len(state["pending"]) >= AUTOLABEL_BATCH):
                batch, state["pending"] = state["pending"][:AUTOLABEL_BATCH], state["pending"][AUTOLABEL_BATCH:]
                state["summary"] = self.upload_autolabels(spec["id"], batch)
                state["sent"] += len(batch)

        def on_tick(progress):
            lines, state["offset"] = read_lines(out_file, state["offset"])
            for line in lines:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and JOB_ID.fullmatch(str(item.get("image_id", ""))):
                    state["pending"].append({"image_id": item["image_id"], "boxes": list(item.get("boxes") or [])[:300]})
            flush()
            return f"Labeling images: {progress.get('done', state['sent'])}/{state['total']} inferred, {state['sent']} stored"

        returncode, stop_reason = self.run_child(cmd, job_dir, job, "labeling", progress_file, grace=30, on_tick=on_tick,
                                                 message=f"Labeling {len(images)} images")
        on_tick({})
        flush(force=True)
        result = {"gpu_before": gpu_before,
                  "run_info": {"kind": "autolabel", "images": len(images), "missing_images": len(missing),
                               "inferred": state["sent"], "conf": config["conf"], "class_mode": config["class_mode"],
                               "weights": weights.name, "summary": state["summary"]}}
        if stop_reason:
            return {**result, "status": "stopped", "error": stop_reason}
        if returncode != 0:
            log_text = (job_dir / "train.log").read_text("utf-8", "replace")[-200_000:]
            code, message = classify_failure(log_text, returncode)
            return {**result, "status": "failed", "error_code": "autolabel_failed" if code == "training_failed" else code,
                    "error": message if code != "training_failed" else "autolabel.py failed; see the log."}
        if state["sent"] < len(images):
            return {**result, "status": "failed", "error_code": "autolabel_failed",
                    "error": f"autolabel.py produced results for {state['sent']} of {len(images)} images."}
        return {**result, "status": "completed"}

    def evaluate(self, spec, config, job_dir, job, root, best, weights):
        if not BASE_WEIGHTS.is_file():
            return {"status": "skipped", "reason": "Base yolo11m.pt missing; comparison impossible."}
        out = job_dir / "eval"
        out.mkdir(exist_ok=True)
        cmd = [sys.executable, str(ROOT / "evaluate.py"), "--data", str(root), "--split", "test",
               "--baseline", str(BASE_WEIGHTS), "--model", f"new={best}", "--imgsz", str(config["imgsz"]),
               "--device", str(self.args.gpu), "--out", str(out), "--predictions-json", str(out / "predictions.json"),
               "--max-images", "0"]
        if weights != BASE_WEIGHTS:
            cmd += ["--model", f"previous={weights}"]
        returncode, stop_reason = self.run_child(cmd, job_dir, job, "evaluating", grace=30)
        if stop_reason:
            return {"status": "skipped", "reason": f"Evaluation stopped: {stop_reason}"}
        if returncode != 0 or not (out / "metrics.json").is_file():
            return {"status": "failed", "reason": "evaluate.py failed; see the training log."}
        return {"status": "completed", "metrics": json.loads((out / "metrics.json").read_text())}

    def loop(self):
        self.resubmit_pending()
        self.heartbeat(started=True)
        while not self.shutdown:
            try:
                status, spec = self.backend.request("POST", "/api/training/worker/claim", {"worker_id": self.worker_id})
            except (OSError, urllib.error.URLError, ValueError):
                status, spec = None, None
            if status == 401:
                raise SystemExit("Backend rejected the worker token (TRAINING_WORKER_TOKEN mismatch).")
            if status == 200 and spec:
                self.run_job(spec)
                continue
            time.sleep(self.args.poll)
            if not self.shutdown:
                self.heartbeat()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", default=os.environ.get("RETRACE_BACKEND_URL", "http://127.0.0.1:8000"))
    p.add_argument("--gpu", type=int, default=0, help="nvidia-smi / CUDA device index")
    p.add_argument("--poll", type=int, default=5, help="heartbeat interval in seconds")
    p.add_argument("--work-dir", type=Path, default=ROOT / "runs" / "web", help="per-job directories")
    p.add_argument("--min-disk-mib", type=int, default=10_000, help="refuse a job below this free disk space")
    p.add_argument("--stop-grace", type=int, default=180,
                   help="seconds to wait for a between-batch stop (validation + save) before SIGTERM")
    p.add_argument("--dataloader-workers", type=int, default=4, help="Preview/Backend share the host CPUs")
    args = p.parse_args(argv)
    token = os.environ.get("RETRACE_TRAINING_WORKER_TOKEN", "")
    if len(token) < 24:
        raise SystemExit("Set RETRACE_TRAINING_WORKER_TOKEN (same value as the Backend's TRAINING_WORKER_TOKEN, >= 24 chars).")
    os.environ.setdefault("YOLO_CONFIG_DIR", str(ROOT / ".ultralytics"))
    worker = Worker(args, Backend(args.backend, token))

    def request_shutdown(signum, frame):
        worker.shutdown = True
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    print(f"ReTrace training worker {worker.worker_id} -> {args.backend}", flush=True)
    worker.loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
