"""CPU/process fault simulations; these do not qualify real GPU availability."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import subprocess
import sys
import time

import pytest

from workbench_platform.resources.gpu import (
    GpuCoordinator, Lease, Owner, ReleaseUnconfirmed, ResourceBusy,
    ResourceEvidence, StaleOwner,
)


@pytest.fixture
def processes():
    owned = []

    def start():
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        owned.append(process)
        return process

    yield start
    # Only Popen handles created by this fixture may be terminated.
    for process in owned:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.fixture
def world(tmp_path, processes):
    now = [1000.0]
    coordinator = GpuCoordinator(tmp_path / "gpu.sqlite", ttl_seconds=5, clock=lambda: now[0])
    registry = {}

    def launch(requester):
        process = processes()
        owner = Owner(requester, f"launch-{requester}", f"instance-{requester}",
                      process.pid, str(time.time_ns()))
        registry[owner] = process
        return owner, process

    def owned(owner):
        return owner in registry and registry[owner].poll() is None

    def witness(owner, *, children=(), gpu_released=True):
        return ResourceEvidence(owner in registry,
                                registry[owner].poll() is None if owner in registry else None,
                                any(child.poll() is None for child in children), gpu_released)

    return coordinator, now, launch, owned, witness


def test_fifo_duplicate_and_persistent_restart(world):
    gpu, now, launch, owned, witness = world
    alice, _ = launch("alice")
    bob, _ = launch("bob")
    assert gpu.request("first", "alice") == 1
    assert gpu.request("first", "alice") == 1
    assert gpu.request("second", "bob") == 2
    with pytest.raises(ValueError):
        gpu.request("first", "bob")
    with pytest.raises(ResourceBusy):
        gpu.grant("second", bob, owned=owned)
    with pytest.raises(StaleOwner):
        gpu.grant("first", replace(alice, creation_time="wrong-identity"), owned=owned)
    lease = gpu.grant("first", alice, owned=owned)
    assert gpu.request("first", "alice") == 0
    assert gpu.request("second", "bob") == 2
    restarted = GpuCoordinator(gpu.path, ttl_seconds=5, clock=lambda: now[0])
    assert restarted.snapshot().owner == alice
    assert restarted.grant("first", alice, owned=owned, resume_token=lease.token) == lease
    with pytest.raises(ResourceBusy):
        restarted.grant("first", alice, owned=owned, resume_token="wrong")
    with pytest.raises(ResourceBusy):
        restarted.grant("second", bob, owned=owned)
    refreshed = restarted.heartbeat(lease)
    assert refreshed.generation == lease.generation
    with pytest.raises(ReleaseUnconfirmed):
        restarted.release(refreshed, witness=lambda owner: witness(owner, gpu_released=False))
    assert restarted.release(refreshed, witness=witness) == lease.generation + 1
    with pytest.raises(StaleOwner):
        restarted.heartbeat(lease)
    with pytest.raises(ValueError):
        restarted.request("first", "alice")
    assert restarted.grant("second", bob, owned=owned).generation == lease.generation + 1


def test_expired_owner_is_not_replaced_and_reclaim_requires_exact_quiescence(world, processes):
    gpu, now, launch, owned, witness = world
    alice, parent = launch("alice")
    bob, _ = launch("bob")
    child = processes()
    gpu.request("first", "alice")
    gpu.request("second", "bob")
    lease = gpu.grant("first", alice, owned=owned)
    now[0] += 20
    with pytest.raises(ResourceBusy):
        gpu.grant("second", bob, owned=owned)
    with pytest.raises(ReleaseUnconfirmed):
        gpu.reclaim(lease.generation, witness=lambda owner: witness(owner, children=(child,)))
    parent.terminate()
    parent.wait(timeout=5)
    with pytest.raises(ReleaseUnconfirmed):
        gpu.reclaim(lease.generation, witness=lambda owner: witness(owner, children=(child,)))
    assert gpu.snapshot().owner == alice
    child.terminate()
    child.wait(timeout=5)
    with pytest.raises(ReleaseUnconfirmed):
        gpu.reclaim(lease.generation, witness=lambda owner: ResourceEvidence(False, False, False, True))
    with pytest.raises(ReleaseUnconfirmed):
        gpu.reclaim(lease.generation, witness=lambda owner: ResourceEvidence(True, False, False, None))
    assert gpu.reclaim(lease.generation, witness=witness) == lease.generation + 1
    new_lease = gpu.grant("second", bob, owned=owned)
    with pytest.raises(StaleOwner):
        gpu.release(lease, witness=witness)
    with pytest.raises(StaleOwner):
        gpu.reclaim(lease.generation, witness=witness)
    assert gpu.snapshot().owner == bob
    assert new_lease.generation == lease.generation + 1


def test_old_identity_token_and_forged_generation_cannot_release(world):
    gpu, _, launch, owned, witness = world
    alice, _ = launch("alice")
    gpu.request("first", "alice")
    lease = gpu.grant("first", alice, owned=owned)
    forged = (replace(lease, token="x" * 43), replace(lease, generation=True),
              replace(lease, owner=replace(alice, creation_time="different-time")),
              replace(lease, token="\ud800"), "not-a-lease")
    for wrong in forged:
        with pytest.raises(StaleOwner):
            gpu.release(wrong, witness=witness)
    assert gpu.snapshot().owner == alice
    with pytest.raises(ReleaseUnconfirmed):
        gpu.release(lease, witness=lambda owner: (_ for _ in ()).throw(RuntimeError("secret")))
    with pytest.raises(ReleaseUnconfirmed):
        gpu.release(lease, witness=lambda owner: None)
    assert gpu.snapshot().owner == alice
    assert gpu.release(lease, witness=witness) == lease.generation + 1


def test_two_coordinators_contend_on_same_ledger(world):
    first, now, launch, owned, witness = world
    second = GpuCoordinator(first.path, ttl_seconds=5, clock=lambda: now[0])
    alice, _ = launch("alice")
    bob, _ = launch("bob")
    first.request("first", "alice")
    second.request("second", "bob")

    def attempt(coordinator, request_id, owner):
        try:
            return coordinator.grant(request_id, owner, owned=owned)
        except ResourceBusy:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(attempt, first, "first", alice)
        b = pool.submit(attempt, second, "second", bob)
        lease, other = a.result(), b.result()
    assert isinstance(lease, Lease) and other is None
    assert first.snapshot() == second.snapshot()
    assert second.release(lease, witness=witness) == lease.generation + 1
    assert first.grant("second", bob, owned=owned).generation == lease.generation + 1


def test_separate_interpreter_cannot_grant_while_parent_owns(world):
    gpu, _, launch, owned, witness = world
    alice, _ = launch("alice")
    bob, _ = launch("bob")
    gpu.request("first", "alice")
    lease = gpu.grant("first", alice, owned=owned)
    script = (
        "import sys; sys.path.insert(0, 'src'); "
        "from workbench_platform.resources.gpu import GpuCoordinator,Owner,ResourceBusy; "
        "gpu=GpuCoordinator(sys.argv[1]); "
        "owner=Owner('bob','launch-bob','instance-bob',int(sys.argv[2]),sys.argv[3]); "
        "assert gpu.request('second','bob') == 2; "
        "\ntry: gpu.grant('second',owner,owned=lambda _: True)\n"
        "except ResourceBusy: print('busy')\n"
        "else: raise AssertionError('duplicate GPU owner')\n"
    )
    result = subprocess.run([sys.executable, "-c", script, str(gpu.path), str(bob.pid),
                             bob.creation_time], capture_output=True, text=True, timeout=10,
                            check=True)
    assert result.stdout.strip() == "busy"
    assert gpu.snapshot().pending == ("second",)
    gpu.release(lease, witness=witness)
    assert gpu.grant("second", bob, owned=owned).generation == lease.generation + 1


def test_cancel_queue_and_invalid_inputs(world):
    gpu, now, launch, owned, witness = world
    alice, _ = launch("alice")
    bob, _ = launch("bob")
    gpu.request("first", "alice")
    gpu.request("second", "bob")
    gpu.cancel_pending("first", "alice")
    with pytest.raises(StaleOwner):
        gpu.grant("first", alice, owned=owned)
    with pytest.raises(ValueError):
        gpu.request("first", "alice")
    with pytest.raises(ValueError):
        gpu.grant(None, alice, owned=owned)
    with pytest.raises(StaleOwner):
        gpu.cancel_pending("second", "alice")
    lease = gpu.grant("second", bob, owned=owned)
    with pytest.raises(StaleOwner):
        gpu.cancel_pending("second", "bob")
    with pytest.raises(ResourceBusy):
        gpu.reclaim(lease.generation, witness=witness)
    assert gpu.release(lease, witness=witness) == lease.generation + 1
    with pytest.raises(ValueError):
        GpuCoordinator(gpu.path, ttl_seconds=float("nan"), clock=lambda: now[0])
