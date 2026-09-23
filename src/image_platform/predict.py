from pathlib import Path
import sys

import numpy as np

from .images import file_sha, preprocess
from .runtime import decide, session_for, validate_package


def predict_images(package, paths, device="cuda", auto_accept=False):
    metadata = validate_package(package)
    session = session_for(Path(package) / "model.onnx", require_gpu=device == "cuda")
    results = []
    for start in range(0, len(paths), 16):
        batch = paths[start:start + 16]
        tensors, good = [], []
        for path in batch:
            try:
                digest = file_sha(path)
                tensors.append(preprocess(path, digest, metadata["input"]["height"], metadata["input"]["width"], metadata["preprocessing"]))
                good.append((path, digest))
            except (OSError, ValueError) as exc:
                results.append({"path": str(path), "status": "failed", "error": str(exc)})
        if tensors:
            logits = session.run(["logits"], {"images": np.stack(tensors)})[0]
            for (path, digest), decision in zip(good, decide(logits, metadata, auto_accept), strict=True):
                results.append({"path": str(path), "content_sha256": digest, **decision})
    return {"model_version_id": metadata["model_version_id"], "providers": session.get_providers(),
            "torch_imported": "torch" in sys.modules, "results": results}
