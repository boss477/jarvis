import pytest

from friday.services.agent_registry import AgentRegistry
from friday.services.connectors import ConnectorRegistry


def test_agent_registry_registers_and_requires_named_agents():
    registry = AgentRegistry()
    agent = object()
    assert registry.register("planner", agent) is agent
    assert registry.get("planner") is agent
    assert registry.require("planner") is agent
    assert registry.names() == ("planner",)
    assert registry.unregister("planner") is agent
    assert registry.get("planner") is None
    with pytest.raises(LookupError, match="agent not registered"):
        registry.require("planner")


def test_connector_registry_allows_generic_use_and_checks_specific_capabilities():
    registry = ConnectorRegistry()
    client = object()
    connector = registry.register("mail", {"send"}, client=client)
    assert registry.require("mail") is connector
    assert registry.require("mail", "send").client is client
    with pytest.raises(PermissionError, match="does not grant capability 'delete'"):
        registry.require("mail", "delete")


def test_connector_registry_rejects_missing_or_disabled_connectors():
    registry = ConnectorRegistry()
    with pytest.raises(LookupError, match="connector not registered"):
        registry.require("mail")
    registry.register("mail", set()).enabled = False
    with pytest.raises(LookupError, match="connector is disabled"):
        registry.require("mail")
