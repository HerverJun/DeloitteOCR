from collections import Counter, defaultdict
import csv
import json
from pathlib import Path

import numpy as np

from .images import decode, file_sha
from .store import dump, now, uid, fingerprint
from .paths import require_path


def register_asset(store, project_id, path, label_id=None, source_id="upload", group_id=None,
                   job=None, on_accept=None):
    path = require_path(path, 'Classification asset', role='read').resolve(strict=True)
    project = store.project(project_id)
    if label_id and label_id not in {x["label_id"] for x in project["labels"]}:
        raise ValueError("类别不属于该项目")
    image, digest = decode(path)
    with store.connect(write=True) as con:
        if job:
            store.assert_owner(con, job["id"], job["lease_token"])
        prior = con.execute("SELECT * FROM assets WHERE project_id=? AND path=? AND sha256=?", (project_id, str(path), digest)).fetchone()
        if prior:
            return dict(prior), False
        con.execute("UPDATE assets SET archived=1 WHERE project_id=? AND path=? AND sha256<>?", (project_id, str(path), digest))
        asset_id = uid("asset")
        con.execute("INSERT INTO assets(id,project_id,path,name,sha256,width,height,label_id,group_id,source_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (asset_id, project_id, str(path), path.name, digest, image.width, image.height, label_id, group_id or digest, source_id, now()))
        if label_id:
            con.execute("INSERT INTO annotations VALUES(?,?,NULL,?,'import',?)", (uid("annotation"), asset_id, label_id, now()))
        if on_accept is not None:
            on_accept(con, asset_id)
    return store.one("SELECT * FROM assets WHERE id=?", (asset_id,)), True


def grouped_split(items, labels, seed=42, prior_items=None):
    """Keep connected source/content groups together; choose a balanced deterministic split."""
    parent = {}

    def find(key):
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a, b):
        parent[find(a)] = find(b)

    content_labels = defaultdict(set)
    for item in items:
        union("group:" + item["group_id"], "sha:" + item["sha256"])
        content_labels[item["sha256"]].add(item["label_id"])
    if any(len(values) > 1 for values in content_labels.values()):
        raise ValueError("相同图片内容存在冲突标签，请先修正")
    groups = defaultdict(list)
    for item in items:
        groups[find("group:" + item["group_id"])].append(item)
    group_list = sorted(groups)
    label_index = {label: index for index, label in enumerate(labels)}
    matrix = np.zeros((len(group_list), len(labels)), dtype=int)
    for g, key in enumerate(group_list):
        for item in groups[key]:
            matrix[g, label_index[item["label_id"]]] += 1
    if len(items) == 0 or np.any((matrix > 0).sum(axis=0) < 3):
        raise ValueError("每类至少需要 3 个独立来源组，才能保留训练、验证和测试数据")
    if prior_items:
        prior = {item["id"]: item for item in prior_items}
        current = {item["id"]: item for item in items}
        for item in prior_items:
            if item["split"] in {"val", "test"}:
                candidate = current.get(item["id"])
                if not candidate or candidate["label_id"] != item["label_id"] or candidate["sha256"] != item["sha256"]:
                    raise ValueError("锁定的验证或测试样本已被移除或改标签。请恢复原标注；重新定义评估基准时应另建项目。")
        output = []
        for key in group_list:
            prior_splits = {prior[item["id"]]["split"] for item in groups[key] if item["id"] in prior}
            if len(prior_splits) > 1:
                raise ValueError("新的来源分组连接了不同历史划分，会造成评估泄漏，请先检查分组")
            split = next(iter(prior_splits), "train")
            for item in groups[key]:
                output.append({**item, "split": split, "group_id": key})
        return output
    rng = np.random.default_rng(seed)
    best = None
    target = np.array([0.6, 0.2, 0.2])
    # Seed each label across all splits, then allocate the remaining groups greedily.
    for _ in range(120):
        assignment = np.full(len(group_list), -1)
        counts = np.zeros((3, len(labels)), dtype=float)
        for label in np.argsort((matrix > 0).sum(axis=0)):
            candidates = list(rng.permutation(np.flatnonzero(matrix[:, label] > 0)))
            for split in rng.permutation(3):
                if counts[split, label] > 0:
                    continue
                available = [g for g in candidates if assignment[g] < 0]
                if not available:
                    break
                g = available[0]
                assignment[g] = split
                counts[split] += matrix[g]
        totals = matrix.sum(axis=0)
        for g in rng.permutation(np.flatnonzero(assignment < 0)):
            scores = []
            for split in range(3):
                trial = counts.copy()
                trial[split] += matrix[g]
                scores.append(float(np.square(trial / totals - target[:, None]).sum()))
            split = int(np.argmin(scores))
            assignment[g] = split
            counts[split] += matrix[g]
        if np.any(counts == 0):
            continue
        score = float(np.square(counts / totals - target[:, None]).sum())
        if best is None or score < best[0]:
            best = (score, assignment.copy())
    if best is None:
        raise ValueError("来源分组无法形成每类均覆盖的划分，请增加独立来源或调整分组")
    output = []
    for g, key in enumerate(group_list):
        for item in groups[key]:
            output.append({**item, "split": ["train", "val", "test"][best[1][g]], "group_id": key})
    return output


def publish_dataset(store, project_id, name, seed=42, *, asset_ids=None, request_id=None):
    project = store.project(project_id)
    binding = {'name': name, 'seed': seed, 'asset_ids': sorted(asset_ids) if asset_ids is not None else None}
    if request_id:
        previous = store.one('SELECT * FROM idempotency WHERE scope=? AND key=?', (project_id+':dataset', request_id))
        if previous:
            if previous['request_hash'] != fingerprint(binding):
                raise ValueError('数据版本请求身份与原参数不同')
            saved, _ = load_dataset(store, previous['job_id'])
            return {'id': saved['id'], 'name': saved['name'], 'stats': json.loads(saved['stats']), 'created_at': saved['created_at']}
    assets = store.rows("SELECT * FROM assets WHERE project_id=? AND archived=0 AND label_id IS NOT NULL ORDER BY id", (project_id,))
    if asset_ids is not None:
        if not isinstance(asset_ids, list) or not 1 <= len(asset_ids) <= 5000 or len(set(asset_ids)) != len(asset_ids):
            raise ValueError('数据版本资料范围无效')
        assets = [item for item in assets if item['id'] in set(asset_ids)]
        if {item['id'] for item in assets} != set(asset_ids):
            raise ValueError('资料未标注、不存在或超出当前项目')
    prior = store.one("SELECT id FROM datasets WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (project_id,))
    prior_items = load_dataset(store, prior["id"])[1]["items"] if prior else None
    items = grouped_split(assets, [x["label_id"] for x in project["labels"]], seed, prior_items)
    return save_snapshot(store, project, name, items, seed, {
        "prior_dataset_id": prior["id"] if prior else None,
        "holdout_policy": "preserve_existing_splits_new_groups_to_train" if prior else "initial_group_split",
    }, request_id=request_id, binding=binding)


def validate_fixed_splits(items, label_ids):
    """Validate externally specified holdouts without silently reshuffling them."""
    ids, content_labels, content_splits, group_splits = set(), defaultdict(set), defaultdict(set), defaultdict(set)
    for item in items:
        if item["id"] in ids or item["split"] not in {"train", "val", "test"} or item["label_id"] not in label_ids:
            raise ValueError("外部划分包含重复图片 ID、未知类别或无效 split")
        ids.add(item["id"])
        content_labels[item["sha256"]].add(item["label_id"])
        content_splits[item["sha256"]].add(item["split"])
        group_splits[item["group_id"]].add(item["split"])
    if any(len(values) > 1 for values in content_labels.values()):
        raise ValueError("相同图片内容存在冲突标签")
    if any(len(values) > 1 for values in [*content_splits.values(), *group_splits.values()]):
        raise ValueError("外部划分存在相同内容或来源组跨集合泄漏")
    for split in ("train", "val", "test"):
        if {item["label_id"] for item in items if item["split"] == split} != set(label_ids):
            raise ValueError(f"{split} 未覆盖全部类别")


def publish_fixed_dataset(store, project_id, name, splits_by_asset, provenance):
    project = store.project(project_id)
    if store.one("SELECT id FROM datasets WHERE project_id=?", (project_id,)):
        raise ValueError("外部固定划分仅可作为新项目的首个数据版本")
    assets = store.rows("SELECT * FROM assets WHERE project_id=? AND archived=0 AND label_id IS NOT NULL ORDER BY id", (project_id,))
    if set(splits_by_asset) != {item["id"] for item in assets}:
        raise ValueError("外部划分必须完整覆盖项目内已标注图片")
    items = [{**item, "split": splits_by_asset[item["id"]]} for item in assets]
    validate_fixed_splits(items, {label["label_id"] for label in project["labels"]})
    return save_snapshot(store, project, name, items, 42, {
        "prior_dataset_id": None, "holdout_policy": "external_fixed_split", "provenance": provenance,
    })


def save_snapshot(store, project, name, items, seed, extra_stats, *, request_id=None, binding=None):
    project_id = project["id"]
    dataset_id = uid("dataset")
    destination = store.settings.data_dir / "datasets" / f"{dataset_id}.json"
    # Store an immutable snapshot: paths, hashes, labels and source groups at publication time.
    snapshot = {"id": dataset_id, "project_id": project_id, "labels": project["labels"], "seed": seed, "items": items, "provenance": extra_stats.get("provenance")}
    destination.write_text(dump(snapshot), encoding="utf-8")
    # Complete both files before committing the replay receipt. A failed write
    # must never publish a dataset whose CSV is missing.
    csv_path = destination.with_suffix(".csv")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["item_id", "path", "content_sha256", "label_id", "group_id", "split", "source_id"])
        writer.writeheader()
        for item in items:
            writer.writerow({"item_id": item["id"], "path": item["path"], "content_sha256": item["sha256"], "label_id": item["label_id"], "group_id": item["group_id"], "split": item["split"], "source_id": item["source_id"]})
    stats = {"total": len(items), "splits": dict(Counter(x["split"] for x in items)),
             "classes": {label["label_id"]: dict(Counter(x["split"] for x in items if x["label_id"] == label["label_id"])) for label in project["labels"]},
             "seed": seed, **extra_stats,
             "warning": "测试集仅用于最终报告；样本少时不能证明低错误率。"}
    with store.connect(write=True) as con:
        if request_id:
            previous = con.execute('SELECT * FROM idempotency WHERE scope=? AND key=?', (project_id+':dataset', request_id)).fetchone()
            if previous:
                destination.unlink(missing_ok=True)
                csv_path.unlink(missing_ok=True)
                if previous['request_hash'] != fingerprint(binding):
                    raise ValueError('数据版本请求身份与原参数不同')
                saved = con.execute('SELECT * FROM datasets WHERE id=?', (previous['job_id'],)).fetchone()
                return {'id': saved['id'], 'name': saved['name'], 'stats': json.loads(saved['stats']), 'created_at': saved['created_at']}
        con.execute("INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?)", (dataset_id, project_id, name, dump(project["labels"]), str(destination), file_sha(destination), dump(stats), now()))
        if request_id:
            con.execute('INSERT INTO idempotency VALUES(?,?,?,?)', (project_id+':dataset', request_id, fingerprint(binding), dataset_id))
    return {"id": dataset_id, "name": name, "stats": stats, "created_at": now()}


def load_dataset(store, dataset_id):
    dataset = store.one("SELECT * FROM datasets WHERE id=?", (dataset_id,))
    if not dataset:
        raise LookupError("数据版本不存在")
    if file_sha(dataset["manifest_path"]) != dataset["manifest_sha256"]:
        raise ValueError("数据清单哈希不匹配，已拒绝读取")
    manifest = require_path(dataset['manifest_path'], 'Frozen classification dataset', role='read')
    return dataset, json.loads(manifest.read_text(encoding='utf-8'))
