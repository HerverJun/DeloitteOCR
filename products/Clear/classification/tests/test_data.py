import copy
import json

import numpy as np
from PIL import Image
import pytest

from image_platform.datasets import grouped_split, load_dataset, publish_dataset, register_asset, validate_fixed_splits
from image_platform.images import PREPROCESS, decode, file_sha, preprocess, tensor_from_image


def test_transparency_padding_and_shape(tmp_path):
    path = tmp_path / "透明 中文.png"
    Image.new("RGBA", (20, 60), (255, 0, 0, 0)).save(path)
    image, digest = decode(path)
    assert image.getpixel((10, 10)) == (255, 255, 255)
    array = preprocess(path, digest, 224, 224)
    assert array.shape == (3, 224, 224)
    assert array.dtype == np.float32
    white = (1 - np.array(PREPROCESS["mean"])) / np.array(PREPROCESS["std"])
    np.testing.assert_allclose(array[:, 0, 0], white, rtol=1e-6)


def test_letterbox_preserves_entire_long_image():
    image = Image.new("RGB", (10, 30), "black")
    array = tensor_from_image(image, 6, 6)
    denormalized = array.transpose(1, 2, 0) * np.array(PREPROCESS["std"]) + np.array(PREPROCESS["mean"])
    assert np.allclose(denormalized[:, 2:4], 0, atol=1e-6)
    assert np.allclose(denormalized[:, :2], 1, atol=1e-6)
    assert np.allclose(denormalized[:, 4:], 1, atol=1e-6)


def test_exif_orientation_is_applied(tmp_path):
    path = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (40, 20), "red")
    exif = image.getexif()
    exif[274] = 6
    image.save(path, exif=exif)
    decoded, _ = decode(path)
    assert decoded.size == (20, 40)


def test_changed_input_and_animated_files_rejected(tmp_path):
    path = tmp_path / "image.png"
    Image.new("RGB", (20, 20), "red").save(path)
    digest = file_sha(path)
    Image.new("RGB", (20, 20), "blue").save(path)
    with pytest.raises(ValueError, match="input_changed"):
        preprocess(path, digest)
    animated = tmp_path / "animated.webp"
    Image.new("RGB", (20, 20), "red").save(animated, save_all=True, append_images=[Image.new("RGB", (20, 20), "blue")], duration=100)
    with pytest.raises(ValueError, match="单帧"):
        decode(animated)


def make_items():
    return [{"id": f"{label}-{n}", "label_id": label, "sha256": f"hash-{label}-{n}", "group_id": f"group-{label}-{n}"} for label in ("a", "b") for n in range(12)]


def test_split_is_reproducible_and_duplicate_groups_are_joined():
    items = make_items()
    items.append({**items[0], "id": "duplicate-a", "group_id": "separate-origin"})
    items.append({**items[1], "id": "linked-a", "group_id": "separate-origin"})
    result = grouped_split(items, ["a", "b"])
    assert result == grouped_split(copy.deepcopy(items), ["a", "b"])
    split_by_hash = {}
    for item in result:
        split_by_hash.setdefault(item["sha256"], set()).add(item["split"])
    assert all(len(splits) == 1 for splits in split_by_hash.values())
    assert split_by_hash[items[0]["sha256"]] == split_by_hash[items[1]["sha256"]]
    for split in ("train", "val", "test"):
        assert {item["label_id"] for item in result if item["split"] == split} == {"a", "b"}


def test_conflicting_labels_and_insufficient_groups_rejected():
    items = make_items()
    items[-1]["sha256"] = items[0]["sha256"]
    with pytest.raises(ValueError, match="冲突"):
        grouped_split(items, ["a", "b"])
    with pytest.raises(ValueError, match="独立来源"):
        grouped_split(make_items()[:2], ["a", "b"])


def test_feedback_versions_preserve_holdouts_and_reject_leaking_group_changes():
    original = make_items()
    baseline = grouped_split(original, ["a", "b"])
    expanded = original + [{"id": "new", "label_id": "a", "sha256": "new", "group_id": "new-source"}]
    updated = grouped_split(expanded, ["a", "b"], seed=999, prior_items=baseline)
    original_splits = {item["id"]: item["split"] for item in baseline}
    assert all(original_splits.get(item["id"], "train") == item["split"] for item in updated)
    heldout = next(item for item in baseline if item["split"] == "test")
    changed = copy.deepcopy(expanded)
    next(item for item in changed if item["id"] == heldout["id"])["label_id"] = "b" if heldout["label_id"] == "a" else "a"
    with pytest.raises(ValueError, match="锁定"):
        grouped_split(changed, ["a", "b"], prior_items=baseline)
    training = next(item for item in baseline if item["split"] == "train")
    linked = copy.deepcopy(expanded)
    next(item for item in linked if item["id"] == heldout["id"])["group_id"] = next(item for item in linked if item["id"] == training["id"])["group_id"]
    with pytest.raises(ValueError, match="历史划分"):
        grouped_split(linked, ["a", "b"], prior_items=baseline)


def test_snapshot_survives_label_changes_and_detects_manifest_corruption(store, tmp_path):
    for label in ("a", "b"):
        for index in range(5):
            path = tmp_path / f"{label}-{index}.png"
            Image.new("RGB", (20 + index, 20), (index * 30, 20 if label == "a" else 150, 0)).save(path)
            register_asset(store, "p", path, label)
    version = publish_dataset(store, "p", "v1")
    record, snapshot = load_dataset(store, version["id"])
    with store.connect(write=True) as con:
        con.execute("UPDATE assets SET label_id='b' WHERE label_id='a'")
    _, after = load_dataset(store, version["id"])
    assert snapshot == after
    with open(record["manifest_path"], "w", encoding="utf-8") as stream:
        json.dump({}, stream)
    with pytest.raises(ValueError, match="哈希"):
        load_dataset(store, version["id"])


def test_official_splits_cannot_leak_content_or_source_groups():
    items = [{**item, "split": ["train", "val", "test"][i % 3]} for i, item in enumerate(make_items())]
    validate_fixed_splits(items, {"a", "b"})
    duplicate = {**items[0], "id": "new", "split": "test"}
    with pytest.raises(ValueError, match="泄漏"):
        validate_fixed_splits(items + [duplicate], {"a", "b"})
    linked = {**items[0], "id": "new", "sha256": "new-content", "split": "val"}
    with pytest.raises(ValueError, match="泄漏"):
        validate_fixed_splits(items + [linked], {"a", "b"})
    with pytest.raises(ValueError, match="未覆盖"):
        validate_fixed_splits([item for item in items if not (item["split"] == "test" and item["label_id"] == "b")], {"a", "b"})


def test_optimized_preprocessing_is_bit_exact_to_v01(tmp_path):
    import importlib.util
    from image_platform.settings import REPO
    spec = importlib.util.spec_from_file_location("baseline_images", REPO / "benchmarks/baseline-v0.1/images.py")
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    rng = np.random.default_rng(123)
    pixels = rng.integers(0, 256, (237, 151, 4), dtype=np.uint8)
    for mode, extension in [("RGBA", "png"), ("RGB", "jpg"), ("L", "png")]:
        path = tmp_path / f"test-{mode}.{extension}"
        image = Image.fromarray(pixels).convert(mode)
        exif = image.getexif()
        exif[274] = 6
        image.save(path, exif=exif)
        for size in [224, 336]:
            np.testing.assert_array_equal(preprocess(path, height=size, width=size), baseline.preprocess(path, height=size, width=size))


def test_valid_image_with_oversized_trailing_data_is_rejected(tmp_path):
    path = tmp_path / 'oversized.png'
    Image.new('RGB', (2, 2)).save(path)
    with path.open('r+b') as stream:
        stream.truncate(100 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match='100 MiB'):
        decode(path)
