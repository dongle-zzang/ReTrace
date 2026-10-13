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
"""
import argparse
import json
import os
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
    args = p.parse_args(argv)

    data_yaml = args.data if args.data.suffix in (".yaml", ".yml") else args.data / "data.yaml"
    data_dir = data_yaml.parent
    report = None
    if not args.skip_check:
        report = check_dataset.check(check_dataset.find_dataset_root(data_dir.resolve()))
        if report["errors"]:
            print("\n".join(["Dataset errors (run check_dataset.py for the full report):"] + report["errors"][:20]))
            return 1
        if report["info"]["images"]["val"] == 0 and not (args.smoke and args.allow_no_val):
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
    name = args.name or f"{'smoke' if args.smoke else 'ft'}_{classes}_{args.imgsz}_{time.strftime('%Y%m%d_%H%M%S')}"
    model = YOLO(args.model)
    train_yaml = data_yaml
    if classes == "coco80":
        train_yaml = dataset_views.coco80_view(data_dir, model.names) / "data.yaml"
        print(f"coco80 view: {train_yaml}")
    started = time.time()
    model.train(
        data=str(train_yaml), epochs=epochs, imgsz=args.imgsz, batch=args.batch, device=args.device,
        seed=args.seed, deterministic=True, workers=args.workers, patience=args.patience,
        project=args.project, name=name, exist_ok=False, single_cls=classes == "single",
        pretrained=True, optimizer="auto", amp=True, cos_lr=False, close_mosaic=min(10, epochs),
        # Fixed CCTV viewpoint: no rotation/shear/perspective; keep scale (small far cars),
        # HSV (day/night, shadows), horizontal flip and mosaic (crowding/occlusion).
        degrees=0.0, shear=0.0, perspective=0.0, flipud=0.0, fliplr=0.5, scale=0.5,
        translate=0.1, hsv_h=0.015, hsv_s=0.7, hsv_v=0.4, mosaic=1.0, mixup=0.0,
        plots=True, val=True, cache=False, verbose=True,
    )
    save_dir = Path(model.trainer.save_dir)
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
    }
    (save_dir / "run_info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))
    print(json.dumps(info, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
