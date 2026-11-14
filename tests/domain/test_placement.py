import pytest

from campus_fabric.contracts.models import (
    GPUDevice,
    GPURequest,
    KeyValue,
    NodeInventory,
    Placement,
    PlacementSlice,
    ResourceAmounts,
    ResourceRequest,
)
from campus_fabric.domain.placement import (
    choose_placement,
    inventory,
    validate_node,
    validate_placement,
)


def node(name="n1", gpu="g1"):
    capacity = ResourceAmounts(
        cpu_millicores=4000, memory_mib=8192, ephemeral_storage_mib=1024,
        gpu_count=1, gpu_memory_mib=16384,
    )
    return NodeInventory(
        node_id=name, contributor_owner_id="supply", state="Ready",
        capacity=capacity, available=capacity, observed_at=0.0,
        labels=(KeyValue(key="zone", value="east"),),
        devices=(
            GPUDevice(device_id=gpu, physical_gpu_id=gpu, model="A100",
                      memory_mib=16384, state="available"),
            GPUDevice(device_id=f"{gpu}-s0", physical_gpu_id=gpu, model="A100",
                      memory_mib=8192, profile="half", state="available"),
            GPUDevice(device_id=f"{gpu}-s1", physical_gpu_id=gpu, model="A100",
                      memory_mib=8192, profile="half", state="available"),
        ),
    )


def request(**updates):
    values = dict(cpu_millicores=1000, memory_mib=2048, ephemeral_storage_mib=100,
                  gpu=GPURequest(count=1, model="A100", memory_mib=8192))
    values.update(updates)
    return ResourceRequest(**values)


def hold(placement, state="Allocated"):
    return {"a1": {"state": state, "placement": placement.model_dump(mode="json")}}


def test_physical_and_slice_counts_are_not_added():
    supplied = node()
    validate_node(supplied)
    initial = inventory({"n1": supplied.model_dump(mode="json")}, {}, 3.0)
    assert initial[0].available.gpu_count == 1
    assert initial[0].available.gpu_memory_mib == 16384
    sliced_request = request(gpu=GPURequest(count=1, profile="half"))
    first = choose_placement(sliced_request, (), initial)
    occupied = inventory({"n1": supplied}, hold(first), 4.0)
    assert occupied[0].available.gpu_count == 0
    assert occupied[0].available.gpu_memory_mib == 8192
    assert choose_placement(request(), (), occupied) is None
    second = choose_placement(sliced_request, (), occupied)
    assert second.slices[0].device_ids == ("g1-s1",)
    assert occupied[0].available.cpu_millicores == 3000
    assert occupied[0].available.memory_mib == 6144
    assert occupied[0].available.ephemeral_storage_mib == 924


def test_whole_gpu_excludes_every_slice_and_release_restores_once():
    supplied = node()
    first = choose_placement(request(), (), (supplied,))
    live = inventory({"n1": supplied}, hold(first, "Running"), 0.0)
    assert choose_placement(request(gpu=GPURequest(count=1, profile="half")), (), live) is None
    assert live[0].available.gpu_memory_mib == 0
    released = inventory({"n1": supplied}, hold(first, "Released"), 0.0)
    assert released[0].available == supplied.capacity


@pytest.mark.parametrize("candidate", [
    request(cpu_millicores=4001), request(memory_mib=8193),
    request(ephemeral_storage_mib=1025), request(gpu=GPURequest(count=2)),
    request(gpu=GPURequest(count=1, model="H800")),
    request(gpu=GPURequest(count=1, memory_mib=16385)),
    request(gpu=GPURequest(count=1, profile="quarter")),
    request(gpu=GPURequest(count=1, profile="half", memory_mib=8193)),
])
def test_every_resource_dimension_and_shape_is_binding(candidate):
    assert choose_placement(candidate, (), (node(),)) is None


def test_labels_and_readiness_bind_but_zero_gpu_cpu_jobs_work():
    assert choose_placement(request(), (KeyValue(key="zone", value="west"),), (node(),)) is None
    assert choose_placement(request(), (), (node().model_copy(update={"state": "Draining"}),)) is None
    zero = request(gpu=GPURequest(count=0))
    placed = choose_placement(zero, (), (node(),))
    assert placed.slices[0].device_ids == ()
    assert placed.slices[0].resources.gpu_count == 0


def test_revalidation_rejects_forged_resource_claim_and_cross_node():
    req = request()
    real = choose_placement(req, (), (node(),))
    forged = Placement(slices=(real.slices[0].model_copy(update={
        "resources": ResourceAmounts(gpu_count=1, gpu_memory_mib=16384),
    }),))
    with pytest.raises(ValueError, match="resources"):
        validate_placement(req, (), forged, (node(),))
    split = Placement(slices=(real.slices[0], PlacementSlice(
        node_id="n2", resources=ResourceAmounts(),
    )))
    with pytest.raises(ValueError, match="one node"):
        validate_placement(req, (), split, (node(), node("n2", "g2")))


def test_corrupt_claims_detected_instead_of_clamped():
    req = request()
    placed = choose_placement(req, (), (node(),))
    claims = hold(placed)
    claims["a2"] = claims["a1"].copy()
    with pytest.raises(ValueError, match="multiply claimed"):
        inventory({"n1": node()}, claims, 0.0)
    sliced = choose_placement(request(gpu=GPURequest(count=1, profile="half")), (), (node(),))
    claims["a2"] = hold(sliced)["a1"]
    with pytest.raises(ValueError, match="overlap"):
        inventory({"n1": node()}, claims, 0.0)
    with pytest.raises(ValueError, match="unknown node"):
        inventory({}, hold(placed), 0.0)


def test_cpu_only_overcommit_is_detected_even_without_gpu_claims():
    req = request(cpu_millicores=3000, gpu=GPURequest(count=0))
    placed = choose_placement(req, (), (node(),))
    claims = hold(placed)
    claims["a2"] = claims["a1"].copy()
    with pytest.raises(ValueError, match="resources overcommitted"):
        inventory({"n1": node()}, claims, 0.0)


def test_registration_rejects_unbounded_profile_capacity_and_duplicate_physical_ids():
    good = node()
    bad = good.model_copy(update={"devices": good.devices + (GPUDevice(
        device_id="g1-s2", physical_gpu_id="g1", model="A100", profile="half",
        memory_mib=8192, state="available",
    ),)})
    with pytest.raises(ValueError, match="exceed"):
        validate_node(bad)
    with pytest.raises(ValueError, match="globally unique"):
        validate_node(node("n2"), (good,))
    with pytest.raises(ValueError, match="physical parents"):
        validate_node(good.model_copy(update={
            "capacity": good.capacity.model_copy(update={"gpu_count": 3}),
            "available": good.capacity.model_copy(update={"gpu_count": 3}),
        }))
