"""Fine-tune YOLO11m on a checked ReTrace dataset (GPU only, isolated from production).

Before training it (1) re-runs check_dataset.py and refuses on errors or an empty val split,
(2) refuses without CUDA (no CPU fallback), (3) refuses when the shared GPU has less free
memory than --min-free-mib, so the running DeepStream Preview keeps its VRAM.

Class strategy (--classes, see README for the trade-offs):
  coco80  default. Labels remapped to the base model's COCO IDs in a derived view
          (car 2 / truck 7 / bus 5); keeps the pretrained 80-class head and the production
          80-label ONNX/labels.txt layout. Original export untouched.
  single  every box is one class (Ultralytics single_cls, names {0: item}); new 1-class head
  multi   0 car / 1 truck / 2 bus; new 3-class head

Train All (--train-all, coco80 only): every train/val/test image of the export becomes a training
image in a separate copy (dataset_views.train_all_view); the export and its splits stay as they are.
Nothing independent is left to select or stop on, so validation-driven behaviour is switched off:
no per-epoch validation and no early stopping (a fixed --epochs schedule), and the final epoch's
weights are the result (best.pt is overwritten with last.pt). Ultralytics still validates once
after the last epoch on the same training images; that is training fit, not a test score.
Measure the model on newly captured CCTV images (evaluate.py --eval-only) instead.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("YOLO_CONFIG_DIR", str(ROOT / ".ultralytics"))  # keep settings out of ~/.config
(Path(os.environ["YOLO_CONFIG_DIR"]) / "Ultralytics").mkdir(parents=True, exist_ok=True)  # else falls back to /tmp

import check_dataset  # noqa: E402
import dataset_views  # noqa: E402


def gpu_memory(index=0):
    out = subprocess.run(["nvidia-smi", f"--id={index}", "--query-gpu=memory.total,memory.used,utilization.gpu",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True, check=True).stdout
    total, used, util = (int(v) for v in out.strip().split(","))
    return total, used, util


def configure_ultralytics():
    from ultralytics import settings
    # No analytics/sync uploads; weights (incl. the AMP-check model) and runs stay under training/.
    settings.update({"sync": False, "wandb": False, "mlflow": False, "comet": False, "clearml": False,
                     "dvc": False, "raytune": False, "tensorboard": False,
                     "weights_dir": str(ROOT / "weights"), "datasets_dir": str(ROOT / "datasets"),
                     "runs_dir": str(ROOT / "runs")})


def add_worker_callbacks(model, progress_file, stop_file, index):
    """Progress for training/worker.py and a cooperative stop.

    Ultralytics checks trainer.stop after every batch (8.4.x): the run then validates, saves
    last.pt/best.pt and exits normally, so a stopped run still keeps its checkpoints.
    """
    import torch
    state = {"batch": 0, "written": 0.0}

    def write(trainer, final=False):
        if not progress_file:
            return
        now = time.time()
        if not final and now - state["written"] < 1.0:
            return
        state["written"] = now
        loss = trainer.label_loss_items(trainer.tloss, prefix="train") if trainer.tloss is not None else {}
        data = {"epoch": trainer.epoch + 1, "epochs": trainer.epochs, "batch": state["batch"],
                "batches": len(trainer.train_loader) if getattr(trainer, "train_loader", None) else None,
                "batch_size": trainer.batch_size, "train_loss": {k: float(v) for k, v in loss.items()},
                "torch_reserved_mib": torch.cuda.memory_reserved(index) // 2**20, "time": now}
        tmp = progress_file.with_name(progress_file.name + ".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(progress_file)

    def epoch_start(trainer):
        state["batch"] = 0

    def batch_end(trainer):
        state["batch"] += 1
        write(trainer)
        if stop_file and stop_file.exists() and not trainer.stop:
            print("Stop requested: finishing this batch, validating and saving checkpoints.", flush=True)
            trainer.stop = True

    model.add_callback("on_train_epoch_start", epoch_start)
    model.add_callback("on_train_batch_end", batch_end)
    model.add_callback("on_fit_epoch_end", lambda trainer: write(trainer, final=True))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, required=True, help="extracted dataset dir or its data.yaml")
    p.add_argument("--model", default=str(ROOT / "weights" / "yolo11m.pt"))
    p.add_argument("--epochs", type=int, default=50)
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--batch", type=int, default=4)
    p.add_argument("--device", default="0")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--workers", type=int, default=4, help="dataloader workers (Preview/Backend share the 8 CPUs)")
    p.add_argument("--patience", type=int, default=15)
    p.add_argument("--classes", choices=("coco80", "single", "multi"), default="coco80")
    p.add_argument("--name", help="run name under training/runs/<project>")
    p.add_argument("--project", default=str(ROOT / "runs" / "train"))
    p.add_argument("--min-free-mib", type=int, default=6000, help="refuse to start below this free VRAM")
    p.add_argument("--smoke", action="store_true", help="1 epoch pipeline/VRAM check; result is not a model")
    p.add_argument("--allow-no-val", action="store_true",
                   help="smoke only: permit a dataset without val images (never for a real run)")
    p.add_argument("--skip-check", action="store_true", help="do not re-run check_dataset.py (e.g. public smoke data)")
    p.add_argument("--train-all", action="store_true",
                   help="train on train+val+test (coco80 only): no validation/early stopping, final-epoch weights")
    p.add_argument("--progress-file", type=Path, help="JSON progress written for worker.py (epoch/batch/losses)")
    p.add_argument("--stop-file", type=Path, help="graceful stop between batches once this file exists")
    args = p.parse_args(argv)
    if args.train_all and (args.classes != "coco80" or args.skip_check):
        print("--train-all keeps the pretrained COCO head: use --classes coco80 on a checked ReTrace export.")
        return 1

    data_yaml = args.data if args.data.suffix in (".yaml", ".yml") else args.data / "data.yaml"
    data_dir = data_yaml.parent
    report = None
    if not args.skip_check:
        report = check_dataset.check(check_dataset.find_dataset_root(data_dir.resolve()))
        if report["errors"]:
            print("\n".join(["Dataset errors (run check_dataset.py for the full report):"] + report["errors"][:20]))
            return 1
        if report["info"]["images"]["val"] == 0 and not (args.smoke and args.allow_no_val) and not args.train_all:
            print("val split is empty: collect another independent capture session. Refusing to train.")
            return 1
    classes = args.classes if report else "as_given"  # unchecked (public smoke) data: train labels as given

    import torch
    if not torch.cuda.is_available():
        print("CUDA is not available; refusing to train on CPU.")
        return 1
    index = int(str(args.device).split(",")[0])
    total, used, util = gpu_memory(index)
    print(f"GPU {torch.cuda.get_device_name(index)}: {used}/{total} MiB used, {util}% util before training")
    if total - used < args.min_free_mib:
        print(f"Only {total - used} MiB free (< {args.min_free_mib}); production Preview needs its share. Not starting.")
        return 1

    configure_ultralytics()
    from ultralytics import YOLO
    import ultralytics

    epochs = 1 if args.smoke else args.epochs
    kind = "smoke" if args.smoke else "all" if args.train_all else "ft"
    name = args.name or f"{kind}_{classes}_{args.imgsz}_{time.strftime('%Y%m%d_%H%M%S')}"
    model = YOLO(args.model)
    if args.progress_file or args.stop_file:
        add_worker_callbacks(model, args.progress_file, args.stop_file, index)
    train_yaml = data_yaml
    if args.train_all:
        train_yaml = dataset_views.train_all_view(data_dir, model.names) / "data.yaml"
        print(f"Train All copy (train+val+test as training images, original export untouched): {train_yaml}")
    elif classes == "coco80":
        train_yaml = dataset_views.coco80_view(data_dir, model.names) / "data.yaml"
        print(f"coco80 view: {train_yaml}")
    started = time.time()
    model.train(
        data=str(train_yaml), epochs=epochs, imgsz=args.imgsz, batch=args.batch, device=args.device,
        seed=args.seed, deterministic=True, workers=args.workers,
        # Train All has no independent validation: patience 0 disables early stopping (Ultralytics
        # treats it as infinite) and val=False skips per-epoch validation of training images.
        patience=0 if args.train_all else args.patience,
        project=args.project, name=name, exist_ok=False, single_cls=classes == "single",
        pretrained=True, optimizer="auto", amp=True, cos_lr=False, close_mosaic=min(10, epochs),
        # Fixed CCTV viewpoint: no rotation/shear/perspective; keep scale (small far cars),
        # HSV (day/night, shadows), horizontal flip and mosaic (crowding/occlusion).
        degrees=0.0, shear=0.0, perspective=0.0, flipud=0.0, fliplr=0.5, scale=0.5,
        translate=0.1, hsv_h=0.015, hsv_s=0.7, hsv_v=0.4, mosaic=1.0, mixup=0.0,
        plots=True, val=not args.train_all, cache=False, verbose=True,
    )
    save_dir = Path(model.trainer.save_dir)
    weights_dir = save_dir / "weights"
    if args.train_all and (weights_dir / "last.pt").is_file():
        # No validation picks a "best" epoch: the result is the final epoch (last.pt, already
        # stripped of optimizer state by Ultralytics). Keep the best.pt name for the registry.
        shutil.copyfile(weights_dir / "last.pt", weights_dir / "best.pt")
    peak = torch.cuda.max_memory_reserved(index) // 2**20
    info = {
        "smoke": args.smoke, "classes": classes, "epochs": epochs, "imgsz": args.imgsz, "batch": args.batch,
        "seed": args.seed, "model": args.model, "data": str(data_yaml.resolve() if data_yaml.exists() else data_yaml), "train_yaml": str(train_yaml),
        "best": str(save_dir / "weights" / "best.pt"), "seconds": round(time.time() - started),
        "torch": torch.__version__, "cuda": torch.version.cuda, "ultralytics": ultralytics.__version__,
        "python": sys.version.split()[0], "gpu": torch.cuda.get_device_name(index),
        "gpu_mib_before": used, "torch_peak_reserved_mib": peak,
        "dataset_counts": report["info"]["images"] if report else None,
        "dataset_warnings": report["warnings"] if report else None,
        "stopped_early": bool(args.stop_file and args.stop_file.exists()),
        "final_batch": model.trainer.batch_size,  # Ultralytics halves it on a first-epoch CUDA OOM
        "names": {int(k): str(v) for k, v in model.names.items()},
        "train_mode": "all" if args.train_all else "standard",
        "weights_selection": "final_epoch" if args.train_all else "best_val_fitness",
        "early_stopping": "disabled" if args.train_all else {"patience": args.patience},
        "train_images": (json.loads((train_yaml.parent / "train_all.json").read_text())["counts"] if args.train_all
                         else report["info"]["images"]["train"] if report else None),
    }
    (save_dir / "run_info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))
    print(json.dumps(info, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
