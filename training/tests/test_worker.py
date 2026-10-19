"""Worker <-> real Backend protocol on CPU: fake nvidia-smi, fake train.py/evaluate.py, real HTTP.

Run from the repository root with an environment that has the backend requirements and Pillow:
    python -m pytest training/tests/test_worker.py
"""
import io
import json
import os
import socket
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import unittest.mock
import zipfile
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

TRAINING = Path(__file__).resolve().parents[1]
REPO = TRAINING.parent
sys.path.insert(0, str(TRAINING))
sys.path.insert(0, str(REPO))
import worker  # noqa: E402

FAKE_SMI = """#!/bin/sh
case "$*" in
  *query-compute-apps*) exit 0 ;;
  *) echo "Tesla V100-PCIE-16GB, 16384, 4900, 55, 41" ;;
esac
"""
FAKE_TRAIN = """
import argparse, json, sys, time
from pathlib import Path
p = argparse.ArgumentParser()
for name in ("--data", "--model", "--epochs", "--imgsz", "--batch", "--device", "--seed", "--patience", "--classes",
             "--project", "--name", "--min-free-mib", "--workers", "--progress-file", "--stop-file"):
    p.add_argument(name)
p.add_argument("--smoke", action="store_true")
p.add_argument("--train-all", action="store_true")
a = p.parse_args()
mode = Path(a.project, "mode").read_text() if Path(a.project, "mode").exists() else "ok"
out = Path(a.project) / a.name
(out / "weights").mkdir(parents=True)
Path(a.project, "argv.json").write_text(json.dumps(sys.argv[1:]))
if mode == "oom":
    print("torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB")
    sys.exit(1)
epochs = int(a.epochs)
header = "epoch,time,train/box_loss,train/cls_loss,train/dfl_loss,metrics/precision(B),metrics/recall(B),metrics/mAP50(B),metrics/mAP50-95(B),val/box_loss,val/cls_loss,val/dfl_loss"
lines = [header]
for epoch in range(1, epochs + 1):
    for batch in range(1, 4):
        Path(a.progress_file).write_text(json.dumps({"epoch": epoch, "epochs": epochs, "batch": batch, "batches": 3}))
        print(f"{epoch}/{epochs} batch {batch}", flush=True)
        time.sleep(0.15 if mode != "slow" else 0.6)
        if Path(a.stop_file).exists():
            break
    lines.append(f"{epoch},{epoch},1.0,0.5,0.5,0.6,0.5,0.5,{0.2 + epoch / 100},1.1,0.6,0.6")
    (out / "results.csv").write_text("\\n".join(lines) + "\\n")
    if Path(a.stop_file).exists():
        print("Stop requested", flush=True)
        break
(out / "weights" / "best.pt").write_bytes(b"best-weights")
(out / "weights" / "last.pt").write_bytes(b"last-weights")
(out / "run_info.json").write_text(json.dumps({"names": {"0": "person", "2": "car"}, "final_batch": int(a.batch)}))
"""
FAKE_EVALUATE = """
import argparse, json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument("--model", action="append")
p.add_argument("--eval-only", action="store_true")
for name in ("--data", "--split", "--baseline", "--imgsz", "--device", "--out", "--predictions-json", "--max-images"):
    p.add_argument(name)
a = p.parse_args()
Path(a.out).mkdir(parents=True, exist_ok=True)
Path(a.out, "argv.json").write_text(json.dumps({"eval_only": a.eval_only, "data": a.data, "model": a.model,
    "images": sorted(p.name for p in Path(a.data, "images", "test").iterdir())}))
Path(a.out, "metrics.json").write_text(json.dumps({"images": 1, "dataset_warnings": [], "models": {
    "baseline": {"vehicle": {"mAP50": 0.4}, "operating_point": {"precision": 0.5, "recall": 0.4}},
    "new": {"vehicle": {"mAP50": 0.6}, "operating_point": {"precision": 0.7, "recall": 0.6}}}}))
Path(a.predictions_json).write_text(json.dumps({"split": "test", "images": []}))
Path(a.out, "report.md").write_text("# report")
"""


def zip_bytes(entries, symlink=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "/etc/passwd")
    return buffer.getvalue()


class SafeExtractTests(unittest.TestCase):
    def test_member_allowlist(self):
        ok = ["data.yaml", "manifest.json", "images/", "images/train/", "images/train/a-1.jpg",
              "labels/val/a.txt", "dataset/", "dataset/data.yaml", "dataset/labels/test/b.txt"]
        bad = ["../x", "/etc/passwd", "images/../../x.jpg", "images/train/a.py", "labels/train/a.jpg",
               "images/other/a.jpg", "a\\b.txt", "run.sh", "dataset/inner/data.yaml", ".hidden/data.yaml",
               "images/train/a.jpg/"]
        self.assertEqual([n for n in ok if not worker.allowed_member(n)], [])
        self.assertEqual([n for n in bad if worker.allowed_member(n)], [])

    def test_safe_extract_rejects_traversal_symlinks_and_lying_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for data in (zip_bytes({"data.yaml": "x", "../escape.txt": "x"}),
                         zip_bytes({"data.yaml": "x"}, symlink="labels/train/a.txt")):
                (tmp / "a.zip").write_bytes(data)
                with self.assertRaises(worker.WorkerError):
                    worker.safe_extract(tmp / "a.zip", tmp / "out")
                self.assertFalse((tmp / "escape.txt").exists())
            (tmp / "a.zip").write_bytes(zip_bytes({"dataset/data.yaml": "names: {0: car}",
                                                   "dataset/images/train/a.jpg": "x"}))
            out = worker.safe_extract(tmp / "a.zip", tmp / "out")
            self.assertTrue((out / "dataset" / "images" / "train" / "a.jpg").is_file())

    def test_validated_config_only_accepts_integers_and_enums(self):
        spec = {"id": str(uuid4()), "config": {"epochs": 50, "imgsz": 640, "batch": 4, "seed": 42, "patience": 15,
                                               "class_mode": "vehicle3", "smoke": False}}
        self.assertEqual(worker.validated_config(spec)["epochs"], 50)
        for change in ({"epochs": "50; rm -rf /"}, {"batch": 0}, {"imgsz": 800}, {"class_mode": "multi"},
                       {"seed": True}):
            with self.assertRaises(worker.WorkerError):
                worker.validated_config({**spec, "config": {**spec["config"], **change}})
        with self.assertRaises(worker.WorkerError):
            worker.validated_config({**spec, "id": "../../etc"})
        self.assertEqual(worker.validated_config(spec)["train_mode"], "standard")
        train_all = {**spec, "config": {**spec["config"], "train_mode": "all"}}
        self.assertEqual(worker.validated_config(train_all)["train_mode"], "all")
        for change in ({"train_mode": "all", "class_mode": "single"}, {"train_mode": "everything"}, {"kind": "x"}):
            with self.assertRaises(worker.WorkerError):
                worker.validated_config({**spec, "config": {**spec["config"], **change}})
        evaluate = {"id": spec["id"], "config": {"kind": "evaluate", "imgsz": 640, "eval_image_ids": [str(uuid4())]}}
        self.assertEqual(worker.validated_config(evaluate)["kind"], "evaluate")
        for ids in ([], ["../a"], [1], "x"):
            with self.assertRaises(worker.WorkerError):
                worker.validated_config({**evaluate, "config": {**evaluate["config"], "eval_image_ids": ids}})

    def test_failure_classification(self):
        self.assertEqual(worker.classify_failure("CUDA out of memory", 1)[0], "gpu_oom")
        self.assertEqual(worker.classify_failure("CUDA is not available; refusing", 1)[0], "cuda_unavailable")
        self.assertEqual(worker.classify_failure("OSError: [Errno 28] No space left on device", 1)[0], "storage_full")
        self.assertEqual(worker.classify_failure("", -9)[0], "killed")


class DeploymentMailboxTests(unittest.TestCase):
    """Worker half of a deployment: leased request, cancellation, runtime result mapping (no GPU)."""
    def setUp(self):
        import deployment
        self.deployment = deployment
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        patcher = unittest.mock.patch.object(deployment, "DIRECTORY", self.directory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.status(phase="idle")
        self.job_id = str(uuid4())
        self.spec = {"id": self.job_id, "config": {"kind": "deploy", "action": "rollback", "imgsz": 640},
                     "base_model": {"id": "yolo11m-coco", "sha256": "b" * 64}}

    def status(self, **values):
        deployment = self.deployment
        deployment.write_json(self.directory / "status.json", {"enabled": True, "session": "s1", "contract": "c",
                                                               "updated_at": time.time(), **values})

    def fake_worker(self, on_beat):
        beats = []
        def heartbeat(job):
            beats.append(job)
            return on_beat(len(beats)) or {}
        return Namespace(heartbeat=heartbeat, wait_for_gpu=lambda spec, job: None, shutdown=False,
                         args=Namespace(poll=0)), beats

    def test_rollback_request_reports_runtime_result(self):
        request = self.directory / self.job_id / "request.json"
        def runtime(count):
            if count == 2:
                body = json.loads(request.read_text())
                self.assertEqual((body["action"], body["session"], body["contract"]), ("rollback", "s1", "c"))
                self.deployment.write_json(request.parent / "result.json", {"phase": "completed", "active": "x"})
        fake, beats = self.fake_worker(runtime)
        result = self.deployment.run_deployment(fake, self.spec, self.directory, {"id": self.job_id}, worker.WorkerError)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["run_info"]["deployment"]["active"], "x")
        self.assertFalse(json.loads(request.read_text()).get("cancel"))

    def test_stop_request_cancels_and_failed_result_maps_to_error(self):
        request = self.directory / self.job_id / "request.json"
        def runtime(count):
            if count == 1:
                return {"stop": True}
            if json.loads(request.read_text()).get("cancel"):
                self.deployment.write_json(request.parent / "result.json",
                                           {"phase": "rolled_back", "error": "deployment_cancelled", "active": "o"})
        fake, _ = self.fake_worker(runtime)
        result = self.deployment.run_deployment(fake, self.spec, self.directory, {"id": self.job_id}, worker.WorkerError)
        self.assertEqual((result["status"], result["error_code"]), ("failed", "deployment_rolled_back"))
        self.assertIn("deployment_cancelled", result["error"])

    def test_offline_runtime_or_lost_runtime_is_a_worker_error(self):
        self.status(phase="idle", updated_at=0)
        fake, _ = self.fake_worker(lambda count: None)
        with self.assertRaises(worker.WorkerError) as caught:
            self.deployment.run_deployment(fake, self.spec, self.directory, {"id": self.job_id}, worker.WorkerError)
        self.assertEqual(caught.exception.code, "deployment_unavailable")
        self.status(phase="idle")
        fake, _ = self.fake_worker(lambda count: self.status(session="restarted"))
        with self.assertRaises(worker.WorkerError) as caught:
            self.deployment.run_deployment(fake, self.spec, self.directory, {"id": self.job_id}, worker.WorkerError)
        self.assertEqual(caught.exception.code, "runtime_lost")
        # The runtime sees an expired/cancelled lease and recovers on its own.
        self.assertTrue(json.loads((self.directory / self.job_id / "request.json").read_text())["cancel"])

    def test_deploy_config_is_validated(self):
        self.assertEqual(worker.validated_config(self.spec)["action"], "rollback")
        deploy = {**self.spec, "config": {**self.spec["config"], "action": "deploy", "class_mode": "single"}}
        self.assertEqual(worker.validated_config(deploy)["class_mode"], "single")
        for change in ({"action": "delete"}, {"action": "deploy", "class_mode": "multi"},
                       {"action": "deploy", "class_mode": "single", "imgsz": 960}):
            with self.assertRaises(worker.WorkerError):
                worker.validated_config({**self.spec, "config": {**self.spec["config"], **change}})


try:
    import uvicorn  # noqa: F401  (backend requirements)
    import cv2  # noqa: F401
    HAVE_BACKEND = True
except ImportError:  # the training venv has no Backend dependencies
    HAVE_BACKEND = False


@unittest.skipUnless(HAVE_BACKEND, "needs backend/requirements-test.txt")
class WorkerBackendTests(unittest.TestCase):
    def setUp(self):
        import cv2
        import httpx
        import numpy as np
        import uvicorn
        from backend.app.db import CameraStatus, make_engine
        from backend.app.main import create_app
        from backend.app.settings import Settings

        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "cameras.yaml").write_text("cameras: [{id: first, floor: B1, name: B1 주차장, rtsp_env: PRIVATE_FIRST}]")
        settings = Settings(f"sqlite:///{tmp / 'db.sqlite'}", tmp / "cameras.yaml", dataset_dir=tmp / "storage",
                            training_admin_token="a" * 32, training_worker_token="w" * 32)
        state = self.state = {"n": 0, "t": datetime(2026, 10, 1, tzinfo=timezone.utc)}

        def preview(request):
            state["n"] += 1
            image = np.random.default_rng(state["n"]).integers(0, 255, (60, 80, 3), dtype=np.uint8)
            return httpx.Response(200, content=cv2.imencode(".jpg", image)[1].tobytes(), headers={
                "X-Captured-At": state["t"].isoformat(), "Content-Type": "image/jpeg", "X-Capture-Clean": "pre-osd-v1"})

        self.engine = make_engine(settings.database_url)
        self.app = create_app(settings, self.engine, httpx.MockTransport(preview), start_poller=False)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(.02)
        self.base = f"http://127.0.0.1:{port}"
        self.app.state.poller.preview_state = "reachable"
        self.client = httpx.Client(base_url=self.base, timeout=10, trust_env=False)
        with self.app.state.sessions.begin() as session:
            row = session.get(CameraStatus, "first")
            row.state, row.fps, row.observed_at = "online", 10.0, datetime.now(timezone.utc)
        for hour in range(4):  # 4 sessions -> train / val / test
            state["t"] = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(hours=hour)
            image = self.client.post("/api/datasets/capture", json={"camera_id": "first"}).json()
            self.client.put(f"/api/datasets/images/{image['id']}/annotations", json={
                "revision": 1, "status": "reviewed",
                "boxes": [{"id": str(uuid4()), "class_id": 0, "x": 5, "y": 5, "width": 30, "height": 20}]})
        export = self.client.post("/api/datasets/exports", json={"seed": 42}).json()
        for _ in range(200):
            if self.client.get("/api/datasets/exports").json()[0]["status"] == "ready":
                break
            time.sleep(.02)
        self.export_id = export["id"]

        # Fake GPU host: nvidia-smi on PATH, fake train/evaluate scripts, base weights.
        self.root = tmp / "training"
        (self.root / "weights").mkdir(parents=True)
        (self.root / "weights" / "yolo11m.pt").write_bytes(b"coco-weights")
        (self.root / "train.py").write_text(FAKE_TRAIN)
        (self.root / "evaluate.py").write_text(FAKE_EVALUATE)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "nvidia-smi").write_text(FAKE_SMI)
        (bin_dir / "nvidia-smi").chmod(0o755)
        self.old = (worker.ROOT, worker.BASE_WEIGHTS, os.environ["PATH"])
        worker.ROOT, worker.BASE_WEIGHTS = self.root, self.root / "weights" / "yolo11m.pt"
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        args = Namespace(gpu=0, poll=1, work_dir=self.root / "runs" / "web", min_disk_mib=1, stop_grace=20,
                         dataloader_workers=1)
        self.worker = worker.Worker(args, worker.Backend(self.base, "w" * 32))
        self.worker.heartbeat(started=True)

    def tearDown(self):
        worker.ROOT, worker.BASE_WEIGHTS, os.environ["PATH"] = self.old
        self.client.close()
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.engine.dispose()
        self.tmp.cleanup()

    def start_job(self, **changes):
        response = self.client.post("/api/training/jobs", headers={"Authorization": "Bearer " + "a" * 32},
                                    json={"export_id": self.export_id, "acknowledge_limits": True, "epochs": 3, **changes})
        self.assertEqual(response.status_code, 201, response.text)
        status, spec = self.worker.backend.request("POST", "/api/training/worker/claim", {"worker_id": self.worker.worker_id})
        self.assertEqual(status, 200)
        return spec

    def job(self, job_id):
        return self.client.get(f"/api/training/jobs/{job_id}").json()

    def test_full_run_uploads_results_and_registers_model(self):
        spec = self.start_job()
        self.worker.run_job(spec)
        job = self.job(spec["id"])
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["epoch"], 3)
        self.assertEqual([p["epoch"] for p in job["metrics"]], [1, 2, 3])
        self.assertEqual(job["evaluation"]["status"], "completed")
        self.assertFalse(job["evaluation"]["conclusive"])
        self.assertTrue({"best.pt", "last.pt", "results.csv", "train.log", "check_report.json",
                         "eval_metrics.json", "eval_predictions.json"} <= set(job["artifacts"]))
        self.assertTrue(any("3/3 batch" in line for line in job["log_tail"]))
        argv = json.loads((self.root / "runs" / "web" / spec["id"] / "argv.json").read_text())
        self.assertEqual(argv[argv.index("--classes") + 1], "coco80")
        self.assertEqual(argv[argv.index("--batch") + 1], "4")
        model = next(m for m in self.client.get("/api/training/models").json() if m["kind"] == "finetuned")
        self.assertEqual(model["job_id"], spec["id"])
        self.assertEqual(model["deployment"], "not_deployed")
        self.assertEqual(model["metrics"]["test"]["vehicle"]["mAP50"], 0.6)
        # Retraining from that model: the worker reuses its local best.pt after verifying the sha256.
        spec = self.start_job(base_model_id=model["id"], epochs=1)
        self.assertEqual(self.worker.resolve_weights(spec, self.root), self.root / "runs" / "web" / model["job_id"] /
                         "train" / "weights" / "best.pt")

    def capture(self, hours, export=True):
        self.state["t"] = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(hours=hours)
        image = self.client.post("/api/datasets/capture", json={"camera_id": "first"}).json()
        self.client.put(f"/api/datasets/images/{image['id']}/annotations", json={
            "revision": 1, "status": "reviewed",
            "boxes": [{"id": str(uuid4()), "class_id": 0, "x": 5, "y": 5, "width": 30, "height": 20}]})
        return image["id"]

    def test_train_all_then_evaluate_on_new_cctv_images(self):
        spec = self.start_job(train_mode="all")
        self.worker.run_job(spec)
        job = self.job(spec["id"])
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["evaluation"]["status"], "skipped")
        self.assertIn("Train All", job["evaluation"]["reason"])
        self.assertNotIn("eval_metrics.json", job["artifacts"])
        self.assertTrue(all(p["map50"] is None and p["val_loss"] is None for p in job["metrics"]))
        argv = json.loads((self.root / "runs" / "web" / spec["id"] / "argv.json").read_text())
        self.assertIn("--train-all", argv)
        self.assertEqual(argv[argv.index("--classes") + 1], "coco80")
        model = next(m for m in self.client.get("/api/training/models").json() if m["kind"] == "finetuned")
        self.assertEqual(model["config"]["train_mode"], "all")
        new_ids = {self.capture(10), self.capture(13)}
        export = self.client.post("/api/datasets/exports", json={"seed": 1}).json()
        for _ in range(200):
            if next(e for e in self.client.get("/api/datasets/exports").json() if e["id"] == export["id"])["status"] == "ready":
                break
            time.sleep(.02)
        response = self.client.post("/api/training/evaluations", headers={"Authorization": "Bearer " + "a" * 32},
                                    json={"model_id": model["id"], "export_id": export["id"], "acknowledge_limits": True})
        self.assertEqual(response.status_code, 201, response.text)
        status, spec = self.worker.backend.request("POST", "/api/training/worker/claim", {"worker_id": self.worker.worker_id})
        self.assertEqual(status, 200)
        self.worker.run_job(spec)
        job = self.job(spec["id"])
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["evaluation"]["status"], "completed")
        self.assertIn("eval_predictions.json", job["artifacts"])
        argv = json.loads((self.root / "runs" / "web" / spec["id"] / "eval" / "argv.json").read_text())
        self.assertTrue(argv["eval_only"])
        self.assertEqual(argv["images"], sorted(f"{i}.jpg" for i in new_ids))  # only never-trained images
        self.assertTrue(argv["model"][0].endswith(f"{model['job_id']}/train/weights/best.pt"))
        tuned = next(m for m in self.client.get("/api/training/models").json() if m["kind"] == "finetuned")
        self.assertEqual(tuned["metrics"]["new_images"][-1]["job_id"], spec["id"])

    def test_stop_request_is_graceful_and_keeps_checkpoints(self):
        spec = self.start_job(epochs=20)
        dataset_dir = self.root / "runs" / "web" / spec["id"]
        dataset_dir.mkdir(parents=True, exist_ok=True)
        (dataset_dir / "mode").write_text("slow")
        runner = threading.Thread(target=self.worker.run_job, args=(spec,))
        runner.start()
        for _ in range(100):
            if self.job(spec["id"])["phase"] == "training":
                break
            time.sleep(.1)
        response = self.client.post(f"/api/training/jobs/{spec['id']}/stop", headers={"Authorization": "Bearer " + "a" * 32})
        self.assertEqual(response.json()["status"], "stopping")
        runner.join(timeout=60)
        job = self.job(spec["id"])
        self.assertEqual(job["status"], "stopped", job)
        self.assertLess(job["epoch"], 20)
        self.assertIn("best.pt", job["artifacts"])
        self.assertIsNone(job["model_id"])

    def test_gpu_oom_is_reported_with_a_readable_cause(self):
        spec = self.start_job()
        job_dir = self.root / "runs" / "web" / spec["id"]
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "mode").write_text("oom")
        self.worker.run_job(spec)
        job = self.job(spec["id"])
        self.assertEqual((job["status"], job["error_code"]), ("failed", "gpu_oom"))
        self.assertIn("Lower the batch size", job["error"])

    def test_base_weights_change_is_refused(self):
        spec = self.start_job()
        (self.root / "weights" / "yolo11m.pt").write_bytes(b"tampered")
        self.worker.run_job(spec)
        job = self.job(spec["id"])
        self.assertEqual(job["error_code"], "base_model_changed")


if __name__ == "__main__":
    unittest.main()
