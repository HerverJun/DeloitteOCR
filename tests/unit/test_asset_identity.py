"""Source identity, content integrity and confined file access."""

import hashlib
from pathlib import Path

import pytest

from workbench_platform.assets import AssetChanged, AssetRegistry, AssetUnavailable


@pytest.fixture
def space(tmp_path):
    inputs, vault = tmp_path / "inputs", tmp_path / "vault"
    inputs.mkdir()
    vault.mkdir()
    return inputs, vault, AssetRegistry(tmp_path / "assets.sqlite", vault, {"inputs": inputs})


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_same_bytes_different_sources_remain_distinct_and_managed_blobs_deduplicate(space):
    inputs, vault, registry = space
    (inputs / "one.pdf").write_bytes(b"same-content")
    (inputs / "two.pdf").write_bytes(b"same-content")
    first = registry.register("workspaceA", "inputs", "one.pdf",
                              media_type="application/pdf", storage_mode="managed")
    second = registry.register("workspaceA", "inputs", "two.pdf",
                               media_type="application/pdf", storage_mode="managed")
    assert first.asset_id != second.asset_id
    assert first.source_id != second.source_id
    assert first.blob_hash == second.blob_hash == digest(b"same-content")
    assert first.storage_mode == second.storage_mode == "managed"
    assert first.revision == second.revision == 1
    assert registry.register("workspaceA", "inputs", "one.pdf", media_type="application/pdf",
                             storage_mode="managed").asset_id == first.asset_id
    assert len(list(vault.glob("[0-9a-f][0-9a-f]/[0-9a-f]*"))) == 1
    (inputs / "one.pdf").unlink()
    with registry.stage_verified("workspaceA", first.asset_id, revision=1,
                                 blob_hash=first.blob_hash) as path:
        assert path.read_bytes() == b"same-content"


def test_referenced_source_change_cannot_be_processed_as_old_hash(space):
    inputs, vault, registry = space
    source = inputs / "input.csv"
    source.write_bytes(b"old")
    first = registry.register("workspaceA", "inputs", "input.csv",
                              media_type="text/csv", storage_mode="referenced")
    with registry.stage_verified("workspaceA", first.asset_id, revision=first.revision,
                                 blob_hash=first.blob_hash) as staged:
        assert staged.read_bytes() == b"old"
        assert staged.parent == vault
    assert not staged.exists()
    source.write_bytes(b"new-bytes")
    with pytest.raises(AssetChanged):
        with registry.stage_verified("workspaceA", first.asset_id, revision=first.revision,
                                     blob_hash=first.blob_hash):
            pytest.fail("Changed bytes must never be yielded")
    updated = registry.register("workspaceA", "inputs", "input.csv",
                                media_type="text/csv", storage_mode="referenced")
    assert updated.asset_id == first.asset_id
    assert updated.source_id == first.source_id
    assert updated.revision == 2 and updated.blob_hash == digest(b"new-bytes")
    with pytest.raises(AssetChanged):
        with registry.stage_verified("workspaceA", first.asset_id, revision=1,
                                     blob_hash=first.blob_hash):
            pytest.fail("Old revision must never be yielded")
    with registry.stage_verified("workspaceA", updated.asset_id, revision=updated.revision,
                                 blob_hash=updated.blob_hash) as staged:
        assert staged.read_bytes() == b"new-bytes"


def test_missing_reference_reports_unavailable_and_workspace_is_scoped(space):
    inputs, _, registry = space
    source = inputs / "input.txt"
    source.write_bytes(b"important")
    asset = registry.register("workspaceA", "inputs", "input.txt",
                              media_type="text/plain", storage_mode="referenced")
    with pytest.raises(AssetUnavailable):
        registry.get("workspaceB", asset.asset_id)
    source.unlink()
    with pytest.raises(AssetUnavailable):
        with registry.stage_verified("workspaceA", asset.asset_id,
                                     revision=asset.revision, blob_hash=asset.blob_hash):
            pytest.fail("Missing reference must never be yielded")


@pytest.mark.parametrize("name", ["../outside.txt", "/absolute.txt", "C:/drive.txt",
                                        "nested//file.txt", "nested/./file.txt",
                                        "nested/../file.txt", "nested\\file.txt"])
def test_noncanonical_or_escaping_paths_rejected(space, name):
    _, _, registry = space
    with pytest.raises(AssetUnavailable):
        registry.register("workspaceA", "inputs", name, media_type="text/plain",
                          storage_mode="referenced")


def test_symlink_or_junction_escape_is_rejected_on_register_and_read(space, tmp_path, monkeypatch):
    inputs, _, registry = space
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    link = inputs / "link.txt"
    simulated_links = set()
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink",
                        lambda path: path in simulated_links or real_is_symlink(path))
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        # A restricted Windows account still exercises the explicit link guard.
        simulated_links.add(link)
    with pytest.raises(AssetUnavailable):
        registry.register("workspaceA", "inputs", "link.txt",
                          media_type="text/plain", storage_mode="referenced")
    if link.exists() or link.is_symlink():
        if link not in simulated_links:
            link.unlink()
    simulated_links.clear()
    source = inputs / "source.txt"
    source.write_bytes(b"allowed")
    asset = registry.register("workspaceA", "inputs", "source.txt",
                              media_type="text/plain", storage_mode="referenced")
    source.unlink()
    try:
        source.symlink_to(outside)
    except (OSError, NotImplementedError):
        simulated_links.add(source)
    with pytest.raises(AssetUnavailable):
        with registry.stage_verified("workspaceA", asset.asset_id, revision=asset.revision,
                                     blob_hash=asset.blob_hash):
            pytest.fail("Retargeted source must never be yielded")


def test_managed_blob_corruption_is_not_served(space):
    inputs, vault, registry = space
    (inputs / "input.txt").write_bytes(b"known")
    asset = registry.register("workspaceA", "inputs", "input.txt",
                              media_type="text/plain", storage_mode="managed")
    blob = vault / asset.blob_hash[:2] / asset.blob_hash
    blob.write_bytes(b"wrong")
    with pytest.raises(AssetChanged):
        with registry.stage_verified("workspaceA", asset.asset_id, revision=1,
                                     blob_hash=asset.blob_hash):
            pytest.fail("Corrupt blob must never be yielded")
    with pytest.raises(AssetChanged):
        registry.register("workspaceA", "inputs", "input.txt",
                          media_type="text/plain", storage_mode="managed")
