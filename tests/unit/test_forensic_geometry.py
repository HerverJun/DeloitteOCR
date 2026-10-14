"""Pixel-level geometry checks; these are not tampering-accuracy benchmarks."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
import pytest

from forensic_toolbox import __version__
from forensic_toolbox.algorithms import jpeg, jpeg_grid
from forensic_toolbox.core import load_image, run, tile_map
from forensic_toolbox.geometry import describe_geometry, transpose_pixels
from forensic_toolbox.review import render


@pytest.mark.parametrize("width,height", [(100, 77), (65, 65), (96, 64), (33, 1)])
def test_tile_map_keeps_actual_partial_block_boundaries(width, height):
    rows, columns = np.indices((height, width))
    values = ((rows // 32) * 10 + columns // 32).astype(np.float32)
    result = tile_map(values, 32, (height, width), lambda block: float(block.mean()))
    np.testing.assert_array_equal(result, values)
    assert result.shape == (height, width)
    assert np.isfinite(result).all()


def test_tile_map_single_pixel_last_row_and_column_stay_single_pixel():
    values = np.zeros((65, 65), np.float32)
    values[64, :] = 7
    values[:, 64] = 9
    values[64, 64] = 11
    result = tile_map(values, 32, values.shape, lambda block: float(block.mean()))
    np.testing.assert_array_equal(result, values)
    assert np.count_nonzero(result == 11) == 1
    assert np.count_nonzero(result == 7) == 64
    assert np.count_nonzero(result == 9) == 64


@pytest.mark.parametrize("block,shape", [(0, (65, 65)), (-1, (65, 65)), (32, (64, 65))])
def test_tile_map_rejects_an_implicit_coordinate_resize(block, shape):
    with pytest.raises(ValueError, match="original pixel shape"):
        tile_map(np.zeros((65, 65), np.float32), block, shape, lambda a: 0)


@pytest.mark.parametrize("orientation", range(1, 9))
def test_exif_pixel_permutation_matches_pillow_and_exact_pixel_centres(orientation):
    width, height = 101, 77
    pixels = np.arange(width * height, dtype=np.int32).reshape(height, width)
    image = Image.fromarray(pixels)
    image.getexif()[274] = orientation
    expected = np.asarray(ImageOps.exif_transpose(image))
    transformed = transpose_pixels(pixels, orientation)
    np.testing.assert_array_equal(transformed, expected)
    assert transformed.dtype == pixels.dtype
    assert transformed.flags.c_contiguous

    geometry = describe_geometry((width, height), orientation)
    forward = np.asarray(geometry["stored_to_display"])
    inverse = np.asarray(geometry["display_to_stored"])
    np.testing.assert_array_equal(inverse @ forward, np.eye(3, dtype=int))
    np.testing.assert_array_equal(forward @ inverse, np.eye(3, dtype=int))
    rows, columns = np.indices(pixels.shape)
    centres = np.stack([columns.ravel() + .5, rows.ravel() + .5,
                        np.ones(pixels.size)])
    mapped = forward @ centres
    np.testing.assert_array_equal(
        transformed[np.floor(mapped[1]).astype(int), np.floor(mapped[0]).astype(int)],
        pixels.ravel())
    np.testing.assert_array_equal(inverse @ mapped, centres)
    assert geometry["display_size"] == [transformed.shape[1], transformed.shape[0]]
    assert geometry["coordinate_convention"] == "pixel_edges"
    # Outer image edges map to the outer display rectangle, including W/H.
    corners = forward @ np.array([[0, width, 0, width], [0, 0, height, height], [1, 1, 1, 1]])
    assert set(zip(corners[0], corners[1])) == {
        (0, 0), (transformed.shape[1], 0), (0, transformed.shape[0]),
        (transformed.shape[1], transformed.shape[0])}


@pytest.mark.parametrize("raw,status", [(None, "absent"), (0, "invalid"), (9, "invalid"),
                                        ("6", "invalid"), (True, "invalid")])
def test_absent_or_invalid_exif_uses_identity_without_claiming_valid_metadata(raw, status):
    pixels = np.arange(12).reshape(3, 4)
    geometry = describe_geometry((4, 3), raw)
    assert geometry["exif_orientation"] == 1
    assert geometry["orientation_status"] == status
    assert geometry["source_exif_orientation"] == (raw if type(raw) is int else None)
    np.testing.assert_array_equal(transpose_pixels(pixels, raw), pixels)


@pytest.fixture
def encoded_jpegs(tmp_path):
    width, height = 101, 77  # Neither dimension aligns with the JPEG block grid.
    rows, columns = np.indices((height, width))
    pixels = np.stack([(columns * 7 + rows * 3) % 256,
                       ((columns // 8) * 31 + rows * 5) % 256,
                       ((rows // 8) * 49 + columns * 2) % 256], axis=2).astype(np.uint8)
    pixels[3:19, 69:94] = (230, 11, 70)
    paths = {}
    for orientation in range(1, 9):
        path = tmp_path / f"orientation-{orientation}.jpg"
        exif = Image.Exif()
        exif[274] = orientation
        Image.fromarray(pixels).save(path, "JPEG", quality=82, exif=exif)
        paths[orientation] = path
    return paths


@pytest.mark.parametrize("orientation", range(1, 9))
@pytest.mark.parametrize("algorithm", [jpeg, jpeg_grid], ids=["recompression", "jpeg-grid"])
def test_jpeg_analysis_stays_in_encoded_coordinates_then_maps_exactly(encoded_jpegs, orientation, algorithm):
    path = encoded_jpegs[orientation]
    before = path.read_bytes()
    baseline = load_image(encoded_jpegs[1])
    context = load_image(path)
    np.testing.assert_array_equal(context.encoded_image, baseline.encoded_image)
    with Image.open(path) as image:
        np.testing.assert_array_equal(context.image, np.asarray(ImageOps.exif_transpose(image).convert("RGB")))
    original_result = algorithm(baseline)
    result = algorithm(context)
    expected_image = Image.fromarray(original_result.map)
    expected_image.getexif()[274] = orientation
    expected = np.asarray(ImageOps.exif_transpose(expected_image))
    assert result.status == "completed"
    assert result.map.shape == context.image.shape[:2]
    np.testing.assert_array_equal(result.map, expected)
    assert result.metrics == original_result.metrics
    assert result.metrics["analysis_coordinate_system"] == "stored_pixels"
    # Context construction by older callers retains exact behavior as well.
    np.testing.assert_array_equal(algorithm(replace(context, encoded_image=None)).map, expected)
    assert path.read_bytes() == before
    assert context.sha256 == hashlib.sha256(before).hexdigest()


@pytest.mark.parametrize("orientation", [1, 6, 7, 8])
def test_report_maps_preview_and_geometry_use_one_display_coordinate_system(encoded_jpegs, tmp_path, orientation):
    source = encoded_jpegs[orientation]
    before = source.read_bytes()
    output = tmp_path / f"report-{orientation}"
    report = run(source, output, tools=["noise", "frequency", "jpeg", "jpeg_grid"])
    geometry = report["input"]["geometry"]
    assert report["schema_version"] == "0.1"
    assert report["verdict"] is None
    assert geometry["coordinate_version"] == "exif-pixel-edges-v1"
    assert geometry["stored_size"] == [101, 77]
    assert geometry["display_size"] == [report["input"]["width"], report["input"]["height"]]
    assert geometry["persisted_region_coordinate_system"] == "exif_transposed_pixels"
    with Image.open(output / report["input"]["preview_path"]) as preview:
        assert list(preview.size) == geometry["display_size"]
    for item in report["evidence"]:
        assert item["status"] == "completed", item
        assert item["version"] == __version__ == "0.1.1"
        assert item["metrics"]["map_coordinate_system"] == "exif_transposed_pixels"
        with Image.open(output / item["map_path"]) as heatmap:
            assert list(heatmap.size) == geometry["display_size"]
            assert heatmap.mode == "L"
    assert source.read_bytes() == before
    assert report["input"]["sha256"] == hashlib.sha256(before).hexdigest()


def test_non_jpeg_keeps_compression_tools_skipped(tmp_path):
    source = tmp_path / "original.png"
    Image.new("RGB", (100, 77), "white").save(source)
    before = source.read_bytes()
    report = run(source, tmp_path / "output", tools=["jpeg", "jpeg_grid"])
    assert {item["status"] for item in report["evidence"]} == {"skipped"}
    assert all(item["map_path"] is None for item in report["evidence"])
    assert source.read_bytes() == before


def test_legacy_offline_report_without_geometry_renders_without_rewriting_evidence(tmp_path):
    legacy = {"schema_version": "0.1", "input": {"preview_path": "preview-exif-normalized.png",
              "sha256": "a" * 64, "source_format": "PNG", "width": 100, "height": 77},
              "verdict": None, "evidence": [{"tool": "noise", "status": "completed", "family": "noise",
              "version": "0.1.0", "elapsed_ms": 1, "reason": None, "map_path": "noise.png",
              "regions": [], "metrics": {}, "limitations": ["Legacy diagnostic map"]}]}
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(legacy), encoding="utf-8")
    evidence_path = tmp_path / "noise.png"
    Image.new("L", (100, 77), 7).save(evidence_path)
    original_bytes = report_path.read_bytes(), evidence_path.read_bytes()
    render(json.loads(report_path.read_text(encoding="utf-8")), tmp_path / "review.html")
    assert (report_path.read_bytes(), evidence_path.read_bytes()) == original_bytes
    assert "0.1.0" in (tmp_path / "review.html").read_text(encoding="utf-8")


def test_derived_manifest_retains_upstream_hashes_and_matches_current_code():
    module = Path(__file__).resolve().parents[2] / "modules/image_forensics"
    manifest = json.loads((module / "source-manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"] == r"F:\CodexBackups\图像篡改识别\20261003_111113\project"
    upstream = {item["name"]: item["sha256"] for item in manifest["files"]}
    assert upstream["core.py"] == "1d836afaaa75e6713f2470738bbbadc3a97fffc07992ab7d79d0c2f2511e6a5b"
    assert upstream["algorithms.py"] == "f7eb97afde8a1d182f9c01384048077e360c69b488c90d3240947c1b99ef0fe0"
    assert manifest["derived"]["version"] == __version__
    assert hashlib.sha256((module / "worker.py").read_bytes()).hexdigest() == manifest["derived"]["worker_sha256"]
    assert {item["name"] for item in manifest["derived"]["files"]} == set(upstream) | {"geometry.py"}
    for item in manifest["derived"]["files"]:
        assert hashlib.sha256((module / "forensic_toolbox" / item["name"]).read_bytes()).hexdigest() == item["sha256"]


@pytest.mark.parametrize("orientation", range(1, 9))
@pytest.mark.parametrize("width,height", [(4, 3), (101, 77)])
def test_tiff_decoder_orientation_is_applied_exactly_once(tmp_path, orientation, width, height):
    pixels = (np.arange(width * height * 3).reshape(height, width, 3) % 256).astype(np.uint8)
    source = tmp_path / f"tiff-{width}-{orientation}.tiff"
    exif = Image.Exif()
    exif[274] = orientation
    Image.fromarray(pixels).save(source, "TIFF", exif=exif)
    before = source.read_bytes()
    with Image.open(source) as image:
        assert (image.tag_v2[256], image.tag_v2[257]) == (width, height)
        expected = np.asarray(ImageOps.exif_transpose(image).convert("RGB")).copy()
    context = load_image(source)
    assert context.original_size == (width, height)
    assert context.encoded_image is None
    np.testing.assert_array_equal(context.image, expected)
    report = run(source, tmp_path / f"report-{width}-{orientation}", tools=["metadata", "jpeg", "jpeg_grid"])
    geometry = report["input"]["geometry"]
    assert geometry["stored_size"] == [width, height]
    assert geometry["stored_size_source"] == "tiff_image_width_length_tags"
    assert geometry["display_pixels_source"] == "pillow_imageops_exif_transpose"
    assert geometry["display_size"] == [expected.shape[1], expected.shape[0]]
    assert report["input"]["source_exif_orientation"] == orientation
    assert [item["status"] for item in report["evidence"]] == ["completed", "skipped", "skipped"]
    assert source.read_bytes() == before
    assert context.sha256 == hashlib.sha256(before).hexdigest()


def test_isolated_embedded_worker_imports_the_toolbox_next_to_its_script(tmp_path):
    module = Path(__file__).resolve().parents[2] / "modules/image_forensics"
    source, output = tmp_path / "source.png", tmp_path / "worker-output"
    Image.new("RGB", (100, 77), "white").save(source)
    before = source.read_bytes()
    result = subprocess.run([sys.executable, "-I", "-B", str(module / "worker.py"),
                             str(source), str(output), json.dumps(["metadata", "noise", "jpeg"]),
                             "isolated-source.png"], cwd=tmp_path, capture_output=True,
                            text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert {item["version"] for item in report["evidence"]} == {"0.1.1"}
    assert report["runtime"]["toolbox"]["version"] == "0.1.1"
    assert Path(report["runtime"]["toolbox"]["path"]).resolve() == (module / "forensic_toolbox").resolve()
    assert source.read_bytes() == before
