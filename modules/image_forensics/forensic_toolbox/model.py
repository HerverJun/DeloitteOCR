from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from .core import Context, Evidence


def run_external_model(ctx: Context, config_path: Path, output: Path) -> Evidence:
    """Run a user-configured local model with a narrow, checked JSON contract."""
    name = "external_model"
    family = "localization"
    try:
        config_path = config_path.resolve()
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        name = str(cfg["name"])
        family = str(cfg.get("family", family))
        version = str(cfg["version"])
        command = cfg["command"]
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise ValueError("command must be a nonempty argv string list")
        timeout = float(cfg.get("timeout_seconds", 120))
        if not 0 < timeout <= 3600:
            raise ValueError("timeout_seconds must be in (0, 3600]")
        output.mkdir(parents=True, exist_ok=True)
        image_path = output / "input-exif-normalized.png"
        Image.fromarray(ctx.image).save(image_path)
        args = [entry.replace("{image}", str(image_path))
                     .replace("{source_image}", str(ctx.path))
                     .replace("{output_dir}", str(output)) for entry in command]
        proc = subprocess.run(args, cwd=config_path.parent, capture_output=True, text=True,
                              timeout=timeout, shell=False, check=False)
        if proc.returncode:
            raise ValueError(f"model exited {proc.returncode}: {proc.stderr[-500:]}")
        lines = proc.stdout.strip().splitlines()
        if not lines:
            raise ValueError("model produced no JSON on stdout")
        payload = json.loads(lines[-1])
        if not isinstance(payload, dict):
            raise ValueError("model JSON must be an object")
        score = payload.get("score")
        if score is not None and (not isinstance(score, (int, float)) or
                                  isinstance(score, bool) or not math.isfinite(score)):
            raise ValueError("score must be a finite number")
        w, h = ctx.size
        regions = []
        for box in payload.get("regions", []):
            if (not isinstance(box, list) or len(box) != 4 or
                    any(not isinstance(v, (int, float)) or isinstance(v, bool) or
                        not math.isfinite(v) for v in box)):
                raise ValueError("invalid region")
            x1, y1, x2, y2 = box
            if not (0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h):
                raise ValueError("region outside normalized image")
            regions.append([float(v) for v in box])
        heatmap = None
        map_assessment = "not_provided"
        map_diagnostics = {}
        if payload.get("mask_path"):
            mask_path = (output / str(payload["mask_path"])).resolve()
            if not mask_path.is_relative_to(output.resolve()):
                raise ValueError("mask_path outside model output directory")
            with Image.open(mask_path) as img:
                if img.mode != "L" or img.size != (w, h):
                    raise ValueError("mask must be an 8-bit grayscale full-size image")
                data = np.asarray(img, dtype=np.float32)
            if not np.isfinite(data).all():
                raise ValueError("mask contains nonfinite values")
            heatmap = np.clip(data / 255, 0, 1)
            p05, p50, p95 = (float(x) for x in np.percentile(heatmap, [5, 50, 95]))
            above_half = float(np.mean(heatmap > 0.5))
            map_diagnostics = {"map_p05": round(p05, 4), "map_median": round(p50, 4),
                               "map_p95": round(p95, 4),
                               "map_fraction_above_half": round(above_half, 4)}
            # This is a map-usability check, not a learned authenticity threshold.
            if above_half >= .75:
                map_assessment = "diffuse_nonlocalizing"
            elif p95 - p05 < .1:
                map_assessment = "low_contrast_nonlocalizing"
            else:
                map_assessment = "spatially_varied_unverified"
        limitations = ["The external model's score and map need calibration on business images."]
        if map_assessment.endswith("nonlocalizing"):
            limitations.append("The predicted map does not isolate a reviewable region; a high model score is not localization evidence.")
        return Evidence(name, family, metrics={"model_score": score,
                         "score_is_calibrated": False,
                         "map_assessment": map_assessment, **map_diagnostics},
                        regions=regions, map=heatmap, version=version,
                        limitations=limitations)
    except Exception as exc:
        return Evidence(name, family, "error", reason=f"{type(exc).__name__}: {exc}")
