"""Account capability compatibility and trusted command boundaries."""

import json

import pytest
from pydantic import ValidationError

from campus_fabric import contracts as c
from campus_fabric.auth.http import HTTPIdentity


@pytest.mark.parametrize(
    ("role", "capabilities"),
    [
        ("member", ("use_compute", "contribute_resources")),
        ("researcher", ("use_compute",)),
        ("contributor", ("contribute_resources",)),
        ("administrator", ("use_compute", "contribute_resources")),
        ("test_driver", ()),
        ("system", ()),
    ],
)
def test_roles_keep_legacy_serialization_and_capability_limits(role, capabilities):
    serialized = {"actor_id": "actor", "role": role, "owner_id": "owner"}
    actor = c.AuthenticatedActor.model_validate_json(json.dumps(serialized))
    assert actor.capabilities == capabilities
    assert actor.can_use_compute == ("use_compute" in capabilities)
    assert actor.can_contribute_resources == ("contribute_resources" in capabilities)
    assert actor.model_dump(mode="json") == serialized
    assert set(actor.model_json_schema()["properties"]) == set(serialized)
    with pytest.raises(ValidationError, match="frozen"):
        actor.capabilities = ("use_compute", "contribute_resources")


@pytest.mark.parametrize("role", ["member", "researcher", "contributor"])
def test_user_roles_require_an_authenticated_owner(role):
    with pytest.raises(ValidationError, match="authenticated owner_id"):
        c.AuthenticatedActor(actor_id="actor", role=role)


def test_capabilities_cannot_be_supplied_to_escalate_a_legacy_actor():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        c.AuthenticatedActor.model_validate_json(json.dumps({
            "actor_id": "alice", "role": "researcher", "owner_id": "alice",
            "capabilities": ["use_compute", "contribute_resources"],
        }))


@pytest.mark.parametrize("role", ["member", "contributor", "administrator"])
def test_contribution_commands_accept_contribution_capability(role):
    actor = c.AuthenticatedActor(actor_id="actor", role=role, owner_id="owner")
    command = c.CommandEnvelope(
        request_id="recall", actor=actor, operation="contribution.recall",
        parameters=c.RecallParameters(
            kind="contribution.recall", device_ids=("gpu",),
            notice_seconds=1, duration_seconds=10,
        ),
    )
    assert command.actor == actor


@pytest.mark.parametrize("role", ["researcher", "test_driver", "system"])
def test_contribution_commands_reject_missing_contribution_capability(role):
    with pytest.raises(ValidationError, match="resource contribution authority"):
        c.CommandEnvelope(
            request_id="recall",
            actor=c.AuthenticatedActor(actor_id="actor", role=role, owner_id="owner"),
            operation="contribution.recall",
            parameters=c.RecallParameters(
                kind="contribution.recall", device_ids=("gpu",),
                notice_seconds=1, duration_seconds=10,
            ),
        )


@pytest.mark.parametrize("owner_id", ["someone-else", None])
def test_contribution_capability_does_not_override_registered_resource_owner(owner_id):
    actor = c.AuthenticatedActor(
        actor_id="actor", role="administrator" if owner_id is None else "member",
        owner_id=owner_id,
    )
    resources = c.ResourceAmounts(cpu_millicores=1000, memory_mib=1024)
    node = c.NodeInventory(
        node_id="node", contributor_owner_id="owner", state="Ready",
        capacity=resources, available=resources, observed_at=0.0,
    )
    with pytest.raises(ValidationError, match="resource owner must match"):
        c.CommandEnvelope(
            request_id="register", actor=actor, operation="contribution.register",
            parameters=c.ContributionRegisterParameters(kind="contribution.register", node=node),
        )


@pytest.mark.parametrize(
    ("operation", "parameters", "error"),
    [
        ("test.reset", c.ResetParameters(kind="test.reset"), "isolated test driver"),
        (
            "clock.advance",
            c.ClockAdvanceParameters(kind="clock.advance", advance_seconds=1.0, source="manual"),
            "clock-control authority",
        ),
    ],
)
def test_member_capabilities_do_not_grant_privileged_control(operation, parameters, error):
    with pytest.raises(ValidationError, match=error):
        c.CommandEnvelope(
            request_id="control", actor=c.AuthenticatedActor(
                actor_id="alice", role="member", owner_id="alice",
            ), operation=operation, parameters=parameters,
        )


@pytest.mark.parametrize("role", ["test_driver", "system"])
def test_internal_roles_still_cannot_be_loaded_as_public_tokens(role):
    with pytest.raises(ValueError, match="Internal roles"):
        HTTPIdentity({"public-token": {"actor_id": "actor", "role": role}})


def test_session_projection_is_separate_from_actor_storage():
    actor = c.AuthenticatedActor(actor_id="alice", role="member", owner_id="alice")
    session = c.AccountSession(
        actor_id=actor.actor_id, owner_id=actor.owner_id, capabilities=actor.capabilities,
        is_administrator=False,
    )
    assert json.loads(session.model_dump_json()) == {
        "actor_id": "alice", "owner_id": "alice",
        "capabilities": ["use_compute", "contribute_resources"], "is_administrator": False,
    }
    assert "account_session" not in c.ContractCatalog.model_fields
    for extra in ({"role": "administrator"}, {"token": "secret"}):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            c.AccountSession.model_validate({**session.model_dump(), **extra})


def test_global_administrator_session_does_not_invent_an_owner():
    actor = c.AuthenticatedActor(actor_id="admin", role="administrator")
    session = c.AccountSession(
        actor_id=actor.actor_id, owner_id=actor.owner_id, capabilities=actor.capabilities,
        is_administrator=True,
    )
    assert session.owner_id is None

