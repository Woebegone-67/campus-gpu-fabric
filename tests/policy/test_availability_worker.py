"""B host opt-in wiring, using actual isolated authority for commits."""
import pytest

from campus_fabric.workflows.devhost import WorkflowWorker, availability_input_producer
from tests.policy.test_kernel_integration import read
from tests.policy.test_learned import artifact
from tests.policy.test_learned_kernel import learned_snapshot
from tests.policy.test_reliability_kernel import setup_kernel

INPUT_ENV = ("FABRIC_AVAILABILITY_INPUTS_PATH", "FABRIC_AVAILABILITY_INPUTS_SHA256",
             "FABRIC_AVAILABILITY_NAMESPACE")


def test_worker_enriches_current_reads_then_commits_without_a_second_ledger(tmp_path, monkeypatch):
    kernel = setup_kernel(tmp_path)
    path, sha = artifact(tmp_path)
    monkeypatch.setenv("FABRIC_AVAILABILITY_MODEL_PATH", str(path))
    monkeypatch.setenv("FABRIC_AVAILABILITY_MODEL_SHA256", sha)

    class ObservedFixture:
        last_diagnostics = ("test fixture only; not field accuracy",)
        calls = []

        def enrich(self, decision, observed):
            assert decision.version == observed.version and decision.now == observed.now
            assert observed.global_scope and observed.viewer.role == "administrator"
            assert {intent.intent_id for intent in observed.intents} == {"work-a", "work-b"}
            self.calls.append(decision.version)
            return learned_snapshot(decision)

    producer = ObservedFixture()
    worker = WorkflowWorker(kernel, policy_name="P", input_producer=producer)
    worker.tick()
    allocations = read(kernel).allocations
    assert len(allocations) == 2 and all(item.state == "Allocated" for item in allocations)
    assert {item.allocation_id: item.placement.slices[0].node_id for item in allocations} == {
        "e0-a1": "node-b", "e0-a2": "node-a"}
    assert len(producer.calls) == 2
    assert worker.availability_input_status == producer.last_diagnostics
    worker.tick()
    assert all(item.state == "Running" for item in read(kernel).allocations)
    assert len(read(kernel).jobs) == 2


def test_no_input_configuration_leaves_host_unmodified(monkeypatch):
    for name in INPUT_ENV:
        monkeypatch.delenv(name, raising=False)
    assert availability_input_producer(policy_name=None) is None
    assert availability_input_producer(policy_name="P") is None


@pytest.mark.parametrize("present", INPUT_ENV)
def test_partial_registry_configuration_is_not_silently_ignored(monkeypatch, present):
    for name in INPUT_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(present, "configured")
    with pytest.raises(ValueError, match="together"):
        availability_input_producer(policy_name="P")


@pytest.mark.parametrize("name", [None, "B0", "B2"])
def test_registry_requires_the_selected_external_learned_policy(monkeypatch, name):
    for key in INPUT_ENV:
        monkeypatch.setenv(key, "configured")
    monkeypatch.setenv("FABRIC_AVAILABILITY_MODEL_PATH", "configured")
    monkeypatch.setenv("FABRIC_AVAILABILITY_MODEL_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="external P"):
        availability_input_producer(policy_name=name)
