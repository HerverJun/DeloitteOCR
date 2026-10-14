import json
import os
from pathlib import Path

import jsonschema
import numpy as np

from .images import file_sha
from .settings import REPO


def probabilities(logits):
    logits = np.asarray(logits, dtype=np.float32)
    if logits.ndim != 2 or not np.isfinite(logits).all():
        raise ValueError("模型输出包含无效数值或形状")
    shifted = logits - logits.max(axis=1, keepdims=True)
    exponent = np.exp(shifted)
    return exponent / exponent.sum(axis=1, keepdims=True)


def decide(logits, metadata, auto_accept=False):
    scores = probabilities(logits)
    labels = sorted(metadata["labels"], key=lambda label: label["class_index"])
    if scores.shape[1] != len(labels):
        raise ValueError("模型类别维度不一致")
    output = []
    for row in scores:
        index = int(np.argmax(row))
        label = labels[index]
        score = float(row[index])
        threshold = metadata["decision_policy"]["thresholds_by_label_id"][label["label_id"]]
        output.append({"class_index": index, "label_id": label["label_id"], "display_name": label["display_name"],
                       "score": score, "status": "accepted" if auto_accept and threshold < 1.0 and score >= threshold else "needs_review",
                       "scores": [float(value) for value in row]})
    return output


def validate_package(directory):
    directory = Path(directory)
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    schema = json.loads((REPO / "schemas" / "model-metadata.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(metadata)
    labels = metadata["labels"]
    if sorted(label["class_index"] for label in labels) != list(range(len(labels))):
        raise ValueError("类别索引不连续或重复")
    label_ids = {label["label_id"] for label in labels}
    if len(label_ids) != len(labels) or len(labels) != metadata["output"]["num_classes"]:
        raise ValueError("类别数量或 ID 不一致")
    if set(metadata["decision_policy"]["thresholds_by_label_id"]) != label_ids:
        raise ValueError("阈值类别集合不一致")
    if file_sha(directory / "model.onnx") != metadata["provenance"]["model_onnx_sha256"]:
        raise ValueError("ONNX 模型文件哈希不匹配")
    checksums = json.loads((directory / "checksums.json").read_text(encoding="utf-8"))
    required = {"model.onnx", "metadata.json", "metrics.json", "training-config.json", "label-set.json", "dataset-manifest.json", "environment-lock.txt", "export-validation.json"}
    if not required.issubset(checksums):
        raise ValueError("模型包缺少必要校验条目")
    for relative, expected in checksums.items():
        target = (directory / relative).resolve()
        if not target.is_relative_to(directory.resolve()) or not target.is_file() or file_sha(target) != expected:
            raise ValueError(f"模型包文件校验失败: {relative}")
    return metadata


def session_for(model_path, require_gpu=True, profiling=False):
    import onnxruntime as ort

    # ORT locates CUDA/cuDNN DLLs supplied by the selected GPU runtime environment.
    if require_gpu and hasattr(ort, "preload_dlls"):
        ort.preload_dlls(directory=os.environ.get("ICP_CUDA_DLL_DIR"))
    if require_gpu and "CUDAExecutionProvider" not in ort.get_available_providers():
        raise RuntimeError("ONNX Runtime CUDA provider 不可用，请检查 GPU 环境")
    options = ort.SessionOptions()
    options.enable_profiling = profiling
    if profiling:
        options.profile_file_prefix = str(Path(model_path).parent / "ort-profile")
    options.intra_op_num_threads = 4
    providers = [("CUDAExecutionProvider", {"use_tf32": "0", "cudnn_conv_algo_search": "HEURISTIC"}), "CPUExecutionProvider"] if require_gpu else ["CPUExecutionProvider"]
    session = ort.InferenceSession(str(model_path), sess_options=options, providers=providers)
    session.disable_fallback()
    if require_gpu and session.get_providers()[0] != "CUDAExecutionProvider":
        raise RuntimeError("CUDA session 初始化失败，已拒绝静默切换 CPU")
    inputs, outputs = session.get_inputs(), session.get_outputs()
    if len(inputs) != 1 or inputs[0].name != "images" or inputs[0].type != "tensor(float)":
        raise ValueError("ONNX 输入契约不符")
    if len(outputs) != 1 or outputs[0].name != "logits":
        raise ValueError("ONNX 输出契约不符")
    return session
