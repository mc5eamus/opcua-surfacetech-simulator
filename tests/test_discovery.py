"""
OPC UA SurfaceTechnology Simulator -- Discovery & Tree Traversal Tests

These tests act as an OPC UA client: they start the simulator server,
connect to it, perform endpoint discovery, browse the address space tree,
and read telemetry values -- exactly what a real OPC UA client or Azure
IoT Operations connector would do.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from asyncua import Client, Server, ua
from asyncua.common.node import Node

# ---------------------------------------------------------------------------
# Ensure the simulator package is importable
# ---------------------------------------------------------------------------
SIMULATOR_DIR = Path(__file__).resolve().parent.parent / "simulator"
sys.path.insert(0, str(SIMULATOR_DIR))

import server as sim_server  # noqa: E402

# ---------------------------------------------------------------------------
# Use a single event loop for the entire module so that module-scoped
# fixtures (server + client) share the same loop as the test functions.
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.asyncio(loop_scope="module")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
ENDPOINT = "opc.tcp://127.0.0.1:48400/surfacetech-test"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def opcua_server():
    """Start the SurfaceTechnology OPC UA server on a test port."""
    srv = Server()
    await srv.init()
    srv.set_endpoint(ENDPOINT)
    srv.set_server_name("SurfaceTech Test Server")
    srv.set_security_policy([ua.SecurityPolicyType.NoSecurity])
    srv.set_security_IDs(["Anonymous"])

    await sim_server.build_address_space(srv)

    await srv.start()
    sim_task = asyncio.create_task(sim_server.simulation_loop())
    await asyncio.sleep(sim_server.INTERVAL * 1.5)
    yield srv
    sim_task.cancel()
    await asyncio.gather(sim_task, return_exceptions=True)
    await srv.stop()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def opcua_client(opcua_server):
    """Connect an OPC UA client to the test server."""
    client = Client(ENDPOINT)
    await client.connect()
    yield client
    await client.disconnect()


# ---------------------------------------------------------------------------
# Helper: recursive tree walker
# ---------------------------------------------------------------------------
async def walk_tree(node: Node, depth: int = 0, max_depth: int = 12,
                    collected: dict | None = None, path: tuple[str, ...] = ()) -> dict:
    """Recursively walk the OPC UA address space from *node* and collect
    browse names and node classes.  Returns a dict of {browse_path: node_class}."""
    if collected is None:
        collected = {}
    if depth > max_depth:
        return collected

    try:
        bn = await node.read_browse_name()
        nc = await node.read_node_class()
        current_path = path + (bn.Name,)
        key = "/".join(current_path)
        collected[key] = nc
    except Exception:
        return collected

    for child in await node.get_children():
        await walk_tree(child, depth + 1, max_depth, collected, current_path)

    return collected


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestEndpointDiscovery:
    """Verify that the server exposes the expected endpoints."""


    async def test_get_endpoints(self, opcua_client: Client):
        """A client should be able to discover endpoints."""
        endpoints = await opcua_client.get_endpoints()
        assert len(endpoints) >= 1
        urls = [ep.EndpointUrl for ep in endpoints]
        assert any("surfacetech" in u for u in urls), (
            f"No surfacetech endpoint found in {urls}")


    async def test_server_namespaces(self, opcua_client: Client):
        """The SurfaceTechnology and DI namespaces must be registered."""
        ns_array = await opcua_client.get_namespace_array()
        assert any("SurfaceTechnology" in ns for ns in ns_array), (
            f"SurfaceTechnology namespace missing: {ns_array}")
        assert any("DI" in ns for ns in ns_array), (
            f"DI namespace missing: {ns_array}")
        assert any("Machinery" in ns for ns in ns_array), (
            f"Machinery namespace missing: {ns_array}")

    async def test_default_nodeset_profile(self):
        """Default profile should skip ISA95 and Machinery.Jobs nodesets."""
        minimal_files = sim_server.nodeset_files_for_profile("minimal")
        assert "Opc.Ua.ISA95-JOBCONTROL.NodeSet2.xml" not in minimal_files
        assert "Opc.Ua.Machinery.Jobs.NodeSet2.xml" not in minimal_files
        assert "Opc.Ua.Di.NodeSet2.xml" in minimal_files
        assert "Opc.Ua.IA.NodeSet2.xml" in minimal_files
        assert "Opc.Ua.Machinery.NodeSet2.xml" in minimal_files
        assert "Opc.Ua.STGeneralTypes.NodeSet2.xml" in minimal_files


class TestAddressSpaceStructure:
    """Verify the structure of the address space matches the SurfaceTechnology model."""


    async def test_coating_system_exists(self, opcua_client: Client):
        """Objects/CoatingSystem must exist."""
        objects = opcua_client.nodes.objects
        children = await objects.get_children()
        names = []
        for child in children:
            bn = await child.read_browse_name()
            names.append(bn.Name)
        assert "CoatingSystem" in names, (
            f"CoatingSystem not found under Objects. Found: {names}")


    async def test_coating_system_has_identification(self, opcua_client: Client):
        """CoatingSystem must have an Identification node with nameplate properties."""
        objects = opcua_client.nodes.objects
        coating = None
        for child in await objects.get_children():
            bn = await child.read_browse_name()
            if bn.Name == "CoatingSystem":
                coating = child
                break

        assert coating is not None
        ident = None
        for child in await coating.get_children():
            bn = await child.read_browse_name()
            if bn.Name == "Identification":
                ident = child
                break

        assert ident is not None, "Identification node missing from CoatingSystem"

        # Check nameplate properties
        ident_children = {}
        for child in await ident.get_children():
            bn = await child.read_browse_name()
            ident_children[bn.Name] = child

        assert "Manufacturer" in ident_children, (
            f"Manufacturer missing. Found: {list(ident_children.keys())}")
        assert "SerialNumber" in ident_children, (
            f"SerialNumber missing. Found: {list(ident_children.keys())}")

        # Read manufacturer value
        manufacturer = await ident_children["Manufacturer"].read_value()
        assert "SurfaceTech" in str(manufacturer)


    async def test_components_folder_exists(self, opcua_client: Client):
        """CoatingSystem must have a Components folder with all 4 components."""
        objects = opcua_client.nodes.objects
        coating = None
        for child in await objects.get_children():
            bn = await child.read_browse_name()
            if bn.Name == "CoatingSystem":
                coating = child
                break

        assert coating is not None
        components = None
        for child in await coating.get_children():
            bn = await child.read_browse_name()
            if bn.Name == "Components":
                components = child
                break

        assert components is not None, "Components folder missing"

        comp_names = []
        for child in await components.get_children():
            bn = await child.read_browse_name()
            comp_names.append(bn.Name)

        expected = ["PretreatmentStation", "PowderBooth", "CuringOven", "ConveyorSystem"]
        for exp in expected:
            assert exp in comp_names, (
                f"{exp} missing from Components. Found: {comp_names}")


    async def test_component_monitoring_structure(self, opcua_client: Client):
        """Each component must have Monitoring with Process, Consumption, Health."""
        objects = opcua_client.nodes.objects
        # Navigate to CoatingSystem/Components/PowderBooth
        coating = None
        for child in await objects.get_children():
            bn = await child.read_browse_name()
            if bn.Name == "CoatingSystem":
                coating = child
                break
        assert coating is not None

        components = None
        for child in await coating.get_children():
            bn = await child.read_browse_name()
            if bn.Name == "Components":
                components = child
                break
        assert components is not None

        booth = None
        for child in await components.get_children():
            bn = await child.read_browse_name()
            if bn.Name == "PowderBooth":
                booth = child
                break
        assert booth is not None

        monitoring = None
        for child in await booth.get_children():
            bn = await child.read_browse_name()
            if bn.Name == "Monitoring":
                monitoring = child
                break
        assert monitoring is not None, "Monitoring missing from PowderBooth"

        mon_children = []
        for child in await monitoring.get_children():
            bn = await child.read_browse_name()
            mon_children.append(bn.Name)

        for folder in ["Process", "Consumption", "Health"]:
            assert folder in mon_children, (
                f"{folder} missing from PowderBooth/Monitoring. Found: {mon_children}")


    async def test_line_controller_exists(self, opcua_client: Client):
        """A LineController must exist under Objects."""
        objects = opcua_client.nodes.objects
        names = []
        for child in await objects.get_children():
            bn = await child.read_browse_name()
            names.append(bn.Name)
        assert "LineController" in names, (
            f"LineController not found under Objects. Found: {names}")


class TestTelemetryCollection:
    """Simulate an OPC UA client collecting telemetry values."""


    async def test_read_process_variables(self, opcua_client: Client):
        """Read process variables from PowderBooth and verify they have
        reasonable numeric values."""
        # Navigate to PowderBooth/Monitoring/Process
        objects = opcua_client.nodes.objects

        async def find_child(parent, name):
            for child in await parent.get_children():
                bn = await child.read_browse_name()
                if bn.Name == name:
                    return child
            return None

        coating = await find_child(objects, "CoatingSystem")
        assert coating is not None
        components = await find_child(coating, "Components")
        assert components is not None
        booth = await find_child(components, "PowderBooth")
        assert booth is not None
        monitoring = await find_child(booth, "Monitoring")
        assert monitoring is not None
        process = await find_child(monitoring, "Process")
        assert process is not None

        # Read all process variables
        var_values = {}
        for child in await process.get_children():
            bn = await child.read_browse_name()
            nc = await child.read_node_class()
            if nc == ua.NodeClass.Variable:
                val = await child.read_value()
                var_values[bn.Name] = val

        # Verify expected variables exist with non-None values
        expected_vars = [
            "PowderFlow_g_min", "GunVoltage_kV", "GunCurrent_uA",
            "BoothAirflow_m3_h", "FilmThickness_um", "RecoveryEfficiency_Pct",
        ]
        for var_name in expected_vars:
            assert var_name in var_values, (
                f"{var_name} missing. Found: {list(var_values.keys())}")
            assert var_values[var_name] is not None, (
                f"{var_name} value is None")

        # Verify numeric ranges are reasonable
        assert 0 < var_values["PowderFlow_g_min"] <= 500
        assert 0 < var_values["GunVoltage_kV"] <= 100
        assert 0 < var_values["FilmThickness_um"] <= 500


    async def test_read_oven_temperatures(self, opcua_client: Client):
        """Read CuringOven temperatures and verify they are in expected ranges."""
        objects = opcua_client.nodes.objects

        async def find_child(parent, name):
            for child in await parent.get_children():
                bn = await child.read_browse_name()
                if bn.Name == name:
                    return child
            return None

        coating = await find_child(objects, "CoatingSystem")
        components = await find_child(coating, "Components")
        oven = await find_child(components, "CuringOven")
        monitoring = await find_child(oven, "Monitoring")
        process = await find_child(monitoring, "Process")
        assert process is not None

        var_values = {}
        for child in await process.get_children():
            bn = await child.read_browse_name()
            nc = await child.read_node_class()
            if nc == ua.NodeClass.Variable:
                val = await child.read_value()
                var_values[bn.Name] = val

        assert "OvenTemperature_C" in var_values
        assert 100 < var_values["OvenTemperature_C"] < 300
        assert "OvenTempSetpoint_C" in var_values
        assert var_values["OvenTempSetpoint_C"] == 200.0


    async def test_read_system_monitoring(self, opcua_client: Client):
        """Read system-level monitoring variables."""
        objects = opcua_client.nodes.objects

        async def find_child(parent, name):
            for child in await parent.get_children():
                bn = await child.read_browse_name()
                if bn.Name == name:
                    return child
            return None

        coating = await find_child(objects, "CoatingSystem")
        monitoring = await find_child(coating, "Monitoring")
        assert monitoring is not None

        var_values = {}
        for child in await monitoring.get_children():
            bn = await child.read_browse_name()
            nc = await child.read_node_class()
            if nc == ua.NodeClass.Variable:
                val = await child.read_value()
                var_values[bn.Name] = val

        assert "SystemStatus" in var_values
        assert var_values["SystemStatus"] == 1  # Running
        assert "PartsProduced" in var_values
        assert var_values["PartsProduced"] > 0
        assert "OEE_Overall" in var_values
        assert 0 < var_values["OEE_Overall"] <= 100


    async def test_read_health_alarms(self, opcua_client: Client):
        """Read health alarm booleans from a component."""
        objects = opcua_client.nodes.objects

        async def find_child(parent, name):
            for child in await parent.get_children():
                bn = await child.read_browse_name()
                if bn.Name == name:
                    return child
            return None

        coating = await find_child(objects, "CoatingSystem")
        components = await find_child(coating, "Components")
        pretreatment = await find_child(components, "PretreatmentStation")
        monitoring = await find_child(pretreatment, "Monitoring")
        health = await find_child(monitoring, "Health")
        assert health is not None

        var_values = {}
        for child in await health.get_children():
            bn = await child.read_browse_name()
            nc = await child.read_node_class()
            if nc == ua.NodeClass.Variable:
                val = await child.read_value()
                var_values[bn.Name] = val

        assert "TankLevelLow" in var_values
        assert var_values["TankLevelLow"] is False
        assert "FilterClogged" in var_values
        assert var_values["FilterClogged"] is False


class TestTreeTraversal:
    """Full address space tree traversal, like a discovery client would do."""


    async def test_full_tree_traversal(self, opcua_client: Client):
        """Walk the entire CoatingSystem subtree and verify minimum node count."""
        objects = opcua_client.nodes.objects

        coating = None
        for child in await objects.get_children():
            bn = await child.read_browse_name()
            if bn.Name == "CoatingSystem":
                coating = child
                break
        assert coating is not None

        tree = await walk_tree(coating)

        # Should have a substantial number of nodes
        assert len(tree) > 30, (
            f"Expected > 30 nodes in CoatingSystem subtree, found {len(tree)}")

        # Verify key paths are present
        expected_paths = [
            "CoatingSystem",
            "CoatingSystem/Identification",
            "CoatingSystem/Components",
            "CoatingSystem/Monitoring",
            "CoatingSystem/Components/PretreatmentStation",
            "CoatingSystem/Components/PowderBooth",
            "CoatingSystem/Components/CuringOven",
            "CoatingSystem/Components/ConveyorSystem",
            "CoatingSystem/Components/PowderBooth/Monitoring/Process",
            "CoatingSystem/Components/PowderBooth/Monitoring/Consumption",
            "CoatingSystem/Components/PowderBooth/Monitoring/Health",
        ]
        for expected_path in expected_paths:
            if expected_path not in tree:
                sample_paths = sorted(tree.keys())[:10]
                pytest.fail(
                    f"{expected_path} not found in tree traversal. Sample paths: {sample_paths}"
                )


    async def test_variable_count(self, opcua_client: Client):
        """Count simulated telemetry Variable nodes in the CoatingSystem subtree."""
        objects = opcua_client.nodes.objects

        coating = None
        for child in await objects.get_children():
            bn = await child.read_browse_name()
            if bn.Name == "CoatingSystem":
                coating = child
                break
        assert coating is not None

        expected_vars = {name for name, *_ in sim_server.SYSTEM_MONITORING}
        for process_vars, consumption_vars, health_vars in sim_server.COMPONENTS.values():
            expected_vars.update(name for name, *_ in process_vars)
            expected_vars.update(name for name, *_ in consumption_vars)
            expected_vars.update(name for name, *_ in health_vars)

        async def count_variables(node: Node, depth: int = 0) -> dict[str, int]:
            """Count expected telemetry variables under *node*.

            Returns a mapping of browse name -> occurrence count, limited to
            variables in the expected simulator telemetry set.
            """
            if depth > 15:
                return {}
            counts: dict[str, int] = {}
            try:
                nc = await node.read_node_class()
                if nc == ua.NodeClass.Variable:
                    bn = await node.read_browse_name()
                    if bn.Name in expected_vars:
                        counts[bn.Name] = counts.get(bn.Name, 0) + 1
            except Exception:
                pass
            for child in await node.get_children():
                child_counts = await count_variables(child, depth + 1)
                for name, count in child_counts.items():
                    counts[name] = counts.get(name, 0) + count
            return counts

        telemetry_counts = await count_variables(coating)
        missing = sorted(expected_vars - set(telemetry_counts))
        assert not missing, f"Missing simulated telemetry variables: {missing}"
        total = sum(telemetry_counts.values())
        assert total == len(expected_vars), (
            f"Expected {len(expected_vars)} simulated telemetry variables, found {total}")


    async def test_engineering_units_present(self, opcua_client: Client):
        """Verify that numeric variables have EngineeringUnits properties."""
        objects = opcua_client.nodes.objects

        async def find_child(parent, name):
            for child in await parent.get_children():
                bn = await child.read_browse_name()
                if bn.Name == name:
                    return child
            return None

        coating = await find_child(objects, "CoatingSystem")
        components = await find_child(coating, "Components")
        oven = await find_child(components, "CuringOven")
        monitoring = await find_child(oven, "Monitoring")
        process = await find_child(monitoring, "Process")

        # Find OvenTemperature_C and check it has EngineeringUnits
        oven_temp = await find_child(process, "OvenTemperature_C")
        assert oven_temp is not None

        eu_node = await find_child(oven_temp, "EngineeringUnits")
        assert eu_node is not None, (
            "EngineeringUnits property missing from OvenTemperature_C")
        eu_val = await eu_node.read_value()
        assert eu_val is not None
