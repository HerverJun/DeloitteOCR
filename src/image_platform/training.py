from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from importlib import metadata as package_metadata
import json
from pathlib import Path
import shutil
import time

import numpy as np

from .datasets import load_dataset
from .evaluation import decision_metrics
from .images import PREPROCESS, file_sha, preprocess
from .runtime import decide, probabilities, session_for, validate_package
from .settings import REPO
from .store import dump, fingerprint, now

ENCODERS = {
    "resnet18": {"name": "resnet18.a1_in1k", "pooling": "global_average", "dimension": 512,
                 "revision": "491b427b45c94c7fb0e78b5474cc919aff584bbf", "sha256": "80c49dee3da4822c009c5a7fe591e9223c5a2cfcf95a4067ca4dfb5a7b89c612"},
    "dinov2": {"name": "vit_small_patch14_dinov2.lvd142m", "pooling": "cls_last", "dimension": 384,
               "revision": "4610ca143709d58a633b6397a74412c2c3842454", "sha256": "04d27f3400d059fc0cfd7d17dd1909a75bf3ea8fb3eeb48b97cb99e57ee20081"},
    "mobilenetv4": {"name": "mobilenetv4_conv_small.e2400_r224_in1k", "pooling": "global_average", "dimension": 1280,
                    "revision": "331fb803779522b685cf942e15f914fb6741c1eb", "sha256": "7a7102ec18f62bbfb555b6fe829bbb5af749516b84174926c29ffdfdfc03aec4"},
    "repvit": {"name": "repvit_m0_9.dist_450e_in1k", "pooling": "global_average", "dimension": 384,
               "revision": "c073717dd84b5af671dd51b187b99ac55b2af4f0", "sha256": "c2dd32622df856ea7a68d6b241c1623f0e6fbec8acde381a3519cb5a5463e9cf"},
}


def metrics_for(logits, target, labels):
    from sklearn.metrics import classification_report, confusion_matrix

    pred = np.argmax(logits, axis=1)
    counts = confusion_matrix(target, pred, labels=list(range(len(labels))))
    report = classification_report(target, pred, labels=list(range(len(labels))), target_names=labels, output_dict=True, zero_division=0)
    return {"sample_count": len(target), "confusion_matrix": counts.tolist(), "matrix_rows": "true", "matrix_columns": "predicted",
            "label_order": labels,
            "classification_report": report, "macro_f1": report["macro avg"]["f1-score"],
            "accuracy": float(np.mean(pred == target)),
            "class_counts": dict(Counter(labels[int(index)] for index in target))}


def fit_thresholds(logits, target, label_ids, minimum_precision):
    scores = probabilities(logits)
    predicted = np.argmax(scores, axis=1)
    thresholds = {}
    for index, label in enumerate(label_ids):
        candidates = np.sort(np.unique(scores[predicted == index, index]))
        threshold = 1.0
        for position, candidate in enumerate(candidates):
            # Keep a margin from observed scores so export rounding cannot flip an observed decision.
            lower = float(candidates[position - 1]) if position else max(0.0, float(candidate) - 0.002)
            boundary = (lower + float(candidate)) / 2
            chosen = (predicted == index) & (scores[:, index] >= boundary)
            if chosen.sum() >= 3 and float(np.mean(target[chosen] == index)) >= minimum_precision:
                threshold = boundary
                break
        thresholds[label] = threshold
    return thresholds


def train(store, job, control, log):
    if job["params"].get("device", "cuda") == "cuda":
        from .platform_resources import require_gpu_context
        require_gpu_context()
    import onnx
    import timm
    import torch
    from huggingface_hub import hf_hub_download
    from torch import nn
    from torch.nn import functional as F

    params = job["params"]
    dataset, snapshot = load_dataset(store, params["dataset_id"])
    if dataset["project_id"] != job["project_id"]:
        raise ValueError("数据版本与项目不一致")
    device = params.get("device", "cuda")
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("PyTorch CUDA 不可用；请选择已配置的 GPU 环境或显式 CPU 模式")
    torch.set_num_threads(4)
    torch.set_float32_matmul_precision("highest")
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.manual_seed(params["seed"])
    np.random.seed(params["seed"])
    encoder_info = ENCODERS[params["encoder"]]
    size = params["input_size"]
    batch_size = params["batch_size"]
    labels = [{**label, "class_index": i} for i, label in enumerate(snapshot["labels"])]
    label_ids = [label["label_id"] for label in labels]
    label_index = {label: i for i, label in enumerate(label_ids)}
    items = snapshot["items"]
    control()
    store.progress(job["id"], job["lease_token"], "下载并加载预训练编码器", 0, len(items))
    cfg = timm.get_pretrained_cfg(encoder_info["name"])
    cache_root = store.settings.data_dir / "cache" / "huggingface" / "hub"
    cached_path = cache_root / ("models--" + cfg.hf_hub_id.replace("/", "--")) / "snapshots" / encoder_info["revision"] / "model.safetensors"
    if cached_path.exists():
        weight_path = cached_path
    else:
        try:
            # Respect an existing user-configured HF_HOME without a network HEAD.
            weight_path = Path(hf_hub_download(cfg.hf_hub_id, "model.safetensors", revision=encoder_info["revision"], token=False, local_files_only=True))
        except FileNotFoundError:
            weight_path = Path(hf_hub_download(cfg.hf_hub_id, "model.safetensors", revision=encoder_info["revision"], token=False, cache_dir=cache_root))
    weight_sha = file_sha(weight_path)
    if weight_sha != encoder_info["sha256"]:
        raise ValueError("预训练权重与已核验的官方 SHA256 不一致")
    model_options = {"img_size": size} if params["encoder"] == "dinov2" else {}
    encoder = timm.create_model(encoder_info["name"], pretrained=True, num_classes=0,
                                pretrained_cfg_overlay={"file": str(weight_path)}, **model_options).eval().to(device)
    encoder.requires_grad_(False)
    # Explicit attention avoids exporter-specific fused attention graphs.
    for module in encoder.modules():
        if hasattr(module, "fused_attn"):
            module.fused_attn = False
    contract = {"weights_sha256": weight_sha, "model": encoder_info["name"], "size": size,
                "preprocessing": PREPROCESS, "pooling": encoder_info["pooling"],
                "feature_normalization": "l2_eps_1e-12", "dtype": "float32", "timm": timm.__version__,
                "torch": torch.__version__, "numpy": np.__version__, "pillow": package_metadata.version("Pillow"),
                "precision_policy": "fp32_no_tf32"}
    cache = store.settings.data_dir / "features" / fingerprint(contract)
    cache.mkdir(exist_ok=True)
    cache_hits = 0
    features = []
    feature_started = time.perf_counter()
    for start in range(0, len(items), batch_size):
        control()
        batch = items[start:start + batch_size]
        values = [None] * len(batch)
        missing, tensors = [], []
        for i, item in enumerate(batch):
            destination = cache / f"{item['sha256']}.npy"
            if destination.exists():
                # Cache hits still check input identity. Misses are checked by decode.
                if file_sha(item["path"]) != item["sha256"]:
                    raise ValueError(f"input_changed: {item['name']}")
                try:
                    cached = np.load(destination, allow_pickle=False)
                except (ValueError, OSError):
                    cached = None
                if cached is not None and cached.shape == (encoder_info["dimension"],) and np.isfinite(cached).all():
                    values[i] = cached
                    cache_hits += 1
                    continue
            missing.append(i)
        if missing:
            with ThreadPoolExecutor(max_workers=4) as decoders:
                tensors = list(decoders.map(lambda i: preprocess(batch[i]["path"], batch[i]["sha256"], size, size), missing))
        if tensors:
            with torch.inference_mode():
                encoded = F.normalize(encoder(torch.from_numpy(np.stack(tensors)).to(device)), dim=1, eps=1e-12).cpu().numpy()
            if encoded.shape[1] != encoder_info["dimension"]:
                raise ValueError("编码器特征维度与注册契约不一致")
            for i, vector in zip(missing, encoded, strict=True):
                values[i] = vector
                destination = cache / f"{batch[i]['sha256']}.npy"
                temp = destination.with_suffix(".tmp")
                with temp.open("wb") as stream:
                    np.save(stream, vector, allow_pickle=False)
                temp.replace(destination)
        features.extend(values)
        store.progress(job["id"], job["lease_token"], "提取冻结特征", min(start + batch_size, len(items)), len(items))
    feature_seconds = time.perf_counter() - feature_started
    x = torch.from_numpy(np.stack(features)).to(device)
    y = torch.tensor([label_index[item["label_id"]] for item in items], device=device)
    masks = {split: torch.tensor([item["split"] == split for item in items], device=device) for split in ("train", "val", "test")}
    for split, mask in masks.items():
        if set(y[mask].tolist()) != set(range(len(labels))):
            raise ValueError(f"{split} 没有覆盖所有类别")
    head = nn.Linear(encoder_info["dimension"], len(labels)).to(device)
    train_counts = torch.bincount(y[masks["train"]], minlength=len(labels)).float()
    class_weights = train_counts.sum() / (len(labels) * train_counts)
    optimizer = torch.optim.AdamW(head.parameters(), lr=0.03, weight_decay=0.01)
    best_loss, best_state, best_epoch = float("inf"), None, 0
    history = []
    for epoch in range(params["epochs"]):
        if params.get("head_method") == "logistic_cv":
            break
        control()
        head.train()
        optimizer.zero_grad()
        loss = F.cross_entropy(head(x[masks["train"]]), y[masks["train"]], weight=class_weights)
        loss.backward()
        optimizer.step()
        head.eval()
        with torch.inference_mode():
            val_loss = float(F.cross_entropy(head(x[masks["val"]]), y[masks["val"]]).item())
        history.append({"epoch": epoch + 1, "train_loss": float(loss.item()), "val_loss": val_loss})
        if val_loss < best_loss:
            best_loss, best_epoch = val_loss, epoch + 1
            best_state = {key: value.detach().clone() for key, value in head.state_dict().items()}
        if epoch % 10 == 0 or epoch == params["epochs"] - 1:
            log(f"epoch {epoch + 1}: train={loss.item():.5f}, val={val_loss:.5f}")
            store.progress(job["id"], job["lease_token"], "训练线性分类头", epoch + 1, params["epochs"])
    selected_c = None
    if params.get("head_method") == "logistic_cv":
        from .learning import fit_logistic_head
        fitted = fit_logistic_head(x.cpu().numpy(), y.cpu().numpy(), masks["train"].cpu().numpy(), masks["val"].cpu().numpy(), len(labels), control)
        selected_c, history = fitted["selected_c"], fitted["history"]
        with torch.no_grad():
            head.weight.copy_(torch.from_numpy(fitted["weight"]).to(device))
            head.bias.copy_(torch.from_numpy(fitted["bias"]).to(device))
        log(f"Validation selected regularization C={selected_c}")
    else:
        head.load_state_dict(best_state)
    head.eval()
    with torch.inference_mode():
        all_logits = head(x).cpu().numpy()
    target = y.cpu().numpy()
    val_mask = masks["val"].cpu().numpy()
    test_mask = masks["test"].cpu().numpy()
    thresholds = fit_thresholds(all_logits[val_mask], target[val_mask], label_ids, params["minimum_precision"])
    metrics = {"validation": metrics_for(all_logits[val_mask], target[val_mask], label_ids),
               "test": metrics_for(all_logits[test_mask], target[test_mask], label_ids),
               "history": history, "best_epoch": best_epoch, "cache_hits": cache_hits,
               "feature_seconds": feature_seconds, "business_validated": False,
               "head_method": params.get("head_method", "adamw"), "selected_c": selected_c,
               "warning": "开发实验结果。阈值来自有限验证样本，尚未完成独立业务验收。", "device": device}
    for split, mask in [("validation", val_mask), ("test", test_mask)]:
        metrics[split].update(decision_metrics(all_logits[mask], target[mask], label_ids, thresholds))
    control()
    store.progress(job["id"], job["lease_token"], "导出并验证 ONNX", 0, 1)
    model_id = "model_" + job["id"].removeprefix("job_")
    package = store.settings.data_dir / "models" / model_id
    package.mkdir(exist_ok=True)

    class Classifier(nn.Module):
        def __init__(self, encoder, head):
            super().__init__()
            self.encoder = encoder
            self.head = head

        def forward(self, images):
            return self.head(F.normalize(self.encoder(images), dim=1, eps=1e-12))

    model = Classifier(encoder, head).eval()
    golden_items = [item for item in items if item["split"] == "val"][:8]
    golden = np.stack([preprocess(item["path"], item["sha256"], size, size) for item in golden_items])
    onnx_path = package / "model.onnx"
    with torch.inference_mode():
        torch.onnx.export(model, torch.from_numpy(golden[:1]).to(device), str(onnx_path),
                          input_names=["images"], output_names=["logits"], dynamic_axes={"images": {0: "batch"}, "logits": {0: "batch"}},
                          opset_version=17, dynamo=False)
    onnx.checker.check_model(str(onnx_path))
    session = session_for(onnx_path, require_gpu=device == "cuda", profiling=True)
    validation = []
    max_abs = 0.0
    for n in [1, 3, 16, 32]:
        control()
        batch = np.stack([golden[i % len(golden)] for i in range(n)])
        with torch.inference_mode():
            reference = model(torch.from_numpy(batch).to(device)).cpu().numpy()
        output = session.run(["logits"], {"images": batch})[0]
        deviation = float(np.max(np.abs(reference - output)))
        max_abs = max(max_abs, deviation)
        if not np.allclose(reference, output, atol=1e-4, rtol=1e-3) or not np.array_equal(reference.argmax(1), output.argmax(1)):
            raise RuntimeError(f"ONNX 一致性失败，batch={n}, max_abs={deviation}")
        # Actual decisions must agree as well, including threshold boundary cases.
        policy_stub = {"labels": labels, "decision_policy": {"thresholds_by_label_id": thresholds}}
        if [(r["label_id"], r["status"]) for r in decide(reference, policy_stub, True)] != [(r["label_id"], r["status"]) for r in decide(output, policy_stub, True)]:
            raise RuntimeError("ONNX 接受状态与 PyTorch 不一致，需重新校准阈值")
        validation.append({"batch_size": n, "max_abs_error": deviation, "class_match": True, "decision_match": True})
    profile_path = Path(session.end_profiling())
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    node_providers = Counter(event.get("args", {}).get("provider") for event in profile if event.get("args", {}).get("provider"))
    if device == "cuda" and not node_providers.get("CUDAExecutionProvider"):
        raise RuntimeError("运行剖析未发现 CUDA 节点，GPU 验收失败")
    export_report = {"passed": True, "atol": 1e-4, "rtol": 1e-3, "max_abs_error": max_abs,
                     "batches": validation, "node_provider_events": dict(node_providers), "input_sha256": [item["sha256"] for item in golden_items]}
    metrics["export"] = export_report
    (package / "export-validation.json").write_text(dump(export_report), encoding="utf-8")
    (package / "metrics.json").write_text(dump(metrics), encoding="utf-8")
    (package / "training-config.json").write_text(dump(params), encoding="utf-8")
    (package / "label-set.json").write_text(dump(labels), encoding="utf-8")
    shutil.copyfile(dataset["manifest_path"], package / "dataset-manifest.json")
    versions = sorted(f"{dist.metadata['Name']}=={dist.version}" for dist in package_metadata.distributions() if dist.metadata.get("Name"))
    (package / "environment-lock.txt").write_text("\n".join(versions) + "\n", encoding="utf-8")
    torch.save({"state_dict": {key: value.cpu() for key, value in head.state_dict().items()}, "labels": labels}, package / "head.pt")
    metadata = {
        "schema_version": "1.0", "model_version_id": model_id, "project_id": job["project_id"], "task_type": "single_label", "created_at": now(),
        "encoder": {"adapter": "timm", "model_name": encoder_info["name"], "weights_source": f"https://huggingface.co/{cfg.hf_hub_id}/blob/{weight_path.parent.name}/model.safetensors", "weights_sha256": weight_sha, "weights_license": cfg.license or "see model card"},
        "input": {"name": "images", "dtype": "float32", "layout": "NCHW", "channels": 3, "height": size, "width": size, "dynamic_batch": True},
        "output": {"name": "logits", "dtype": "float32", "kind": "logits", "num_classes": len(labels)},
        "preprocessing": PREPROCESS, "feature": {"pooling": encoder_info["pooling"], "dimension": encoder_info["dimension"], "normalization": "l2", "epsilon": 1e-12, "included_in_onnx": True},
        "labels": labels, "decision_policy": {"version": "validation-v1", "score_transform": "stable_softmax", "thresholds_by_label_id": thresholds,
                                               "comparison": "greater_or_equal", "tie_break": "smallest_class_index", "validation_dataset_version": dataset["id"], "status": "validated"},
        "provenance": {"dataset_version_id": dataset["id"], "manifest_sha256": dataset["manifest_sha256"], "config_sha256": fingerprint(params),
                       "environment_lock_sha256": file_sha(package / "environment-lock.txt"), "model_onnx_sha256": file_sha(onnx_path), "opset": 17,
                       "export_validation_report_sha256": file_sha(package / "export-validation.json")},
    }
    (package / "metadata.json").write_text(dump(metadata), encoding="utf-8")
    license_dir = package / "licenses"
    license_dir.mkdir(exist_ok=True)
    shutil.copyfile(REPO / "resources/third-party/APACHE-2.0.txt", license_dir / "APACHE-2.0.txt")
    shutil.copyfile(REPO / f"resources/third-party/{params['encoder']}-model-card.md", license_dir / "MODEL_CARD.md")
    (license_dir / "weight-provenance.json").write_text(dump({"license": cfg.license, "source": metadata["encoder"]["weights_source"], "sha256": weight_sha}), encoding="utf-8")
    checksums = {str(path.relative_to(package)).replace("\\", "/"): file_sha(path) for path in sorted(package.rglob("*")) if path.is_file() and path.name != "checksums.json"}
    (package / "checksums.json").write_text(dump(checksums), encoding="utf-8")
    validate_package(package)
    control()
    with store.connect(write=True) as con:
        store.assert_owner(con, job["id"], job["lease_token"])
        model_name = f"{params['encoder']} · {params.get('head_method', 'adamw')} · seed {params['seed']} · {dataset['name']}"
        con.execute("INSERT INTO models VALUES(?,?,?,?,?,?,?,?)", (model_id, job["project_id"], job["id"], model_name, str(package), dump(metadata), dump(metrics), now()))
        con.execute("UPDATE projects SET active_model_id=COALESCE(active_model_id,?) WHERE id=?", (model_id, job["project_id"]))
    store.progress(job["id"], job["lease_token"], "训练与导出完成", 1, 1)
    log(f"Model {model_id} ready; validation accuracy={metrics['validation']['accuracy']:.3f}; ONNX max_abs={max_abs:.8f}")
    del session, model, encoder, head, x, y
    if device == "cuda":
        torch.cuda.empty_cache()
    return {"model_id": model_id, "metrics": metrics}
