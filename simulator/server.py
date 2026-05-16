"""
OPC UA SurfaceTechnology Simulator -- Industrial Powder Coating Line

Purpose
-------
An OPC UA simulator modelling a powder coating line using the
SurfaceTechnology/GeneralTypes companion specification (OPC 40563).
The address space is built by loading the standard companion nodesets
(DI, IA, Machinery, ISA95-JOBCONTROL, Machinery Jobs, STGeneralTypes)
and instantiating a system, components, and controller using the types
defined in the STGeneralTypes namespace.

Address space
-------------
- All companion nodesets are loaded so STSysType, STCompType, and
  STBaseControllerType are available.
- A vendor subtype of STSysType is created for the coating line.
- Components (PretreatmentStation, PowderBooth, CuringOven,
  ConveyorSystem) are created as STCompType instances under the
  system's Components folder.
- A controller (STBaseControllerType) is instantiated.
- Process variables are organised under Monitoring/Process,
  Monitoring/Consumption, and Monitoring/Health.

Environment variables
---------------------
OPCUA_PORT          OPC UA listening port (default 4840)
WEB_PORT            Web UI port (default 8080)
PUBLISH_INTERVAL_MS Telemetry interval in ms (default 1000)
ENDPOINT_PATH       URL path of the OPC UA endpoint (default surfacetech-demo)
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from asyncua import Server, ua
from asyncua.common.node import Node
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT = int(os.getenv("OPCUA_PORT", "4840"))
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
PUBLISH_MS = int(os.getenv("PUBLISH_INTERVAL_MS", "1000"))
ENDPOINT_PATH = os.getenv("ENDPOINT_PATH", "surfacetech-demo")
INTERVAL = PUBLISH_MS / 1000.0

DI_NAMESPACE = "http://opcfoundation.org/UA/DI/"
IA_NAMESPACE = "http://opcfoundation.org/UA/IA/"
MACHINERY_NAMESPACE = "http://opcfoundation.org/UA/Machinery/"
ISA95_NAMESPACE = "http://opcfoundation.org/UA/ISA95-JOBCONTROL_V2/"
MACHINERY_JOBS_NAMESPACE = "http://opcfoundation.org/UA/Machinery/Jobs/"
ST_NAMESPACE = "http://opcfoundation.org/UA/SurfaceTechnology/GeneralTypes/"
VENDOR_NAMESPACE = "urn:surfacetech-demo:coating-system"

NODESETS_DIR = Path(__file__).with_name("nodesets")
NODESET_FILES = [
    "Opc.Ua.Di.NodeSet2.xml",
    "Opc.Ua.IA.NodeSet2.xml",
    "Opc.Ua.Machinery.NodeSet2.xml",
    "Opc.Ua.ISA95-JOBCONTROL.NodeSet2.xml",
    "Opc.Ua.Machinery.Jobs.NodeSet2.xml",
    "Opc.Ua.STGeneralTypes.NodeSet2.xml",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("surfacetech-simulator")


# ---------------------------------------------------------------------------
# Variable catalogue
# ---------------------------------------------------------------------------
# Each entry: (browse_name, datatype, eu_unit, eu_low, eu_high, initial)
VarDef = tuple[str, ua.VariantType, str | None, float | None, float | None, Any]

# -- PretreatmentStation ---------------------------------------------------
PRETREATMENT_PROCESS: list[VarDef] = [
    ("TankTemperature_C",       ua.VariantType.Double,  "Cel",   20,   80,    55.0),
    ("PhLevel",                 ua.VariantType.Double,  None,    0,    14,    7.2),
    ("ChemicalConcentration_Pct", ua.VariantType.Double, "%",    0,    100,   12.5),
    ("SprayPressure_bar",       ua.VariantType.Double,  "bar",   0,    10,    3.2),
    ("RinseWaterFlow_L_min",    ua.VariantType.Double,  "L/min", 0,    50,    18.0),
]
PRETREATMENT_CONSUMPTION: list[VarDef] = [
    ("ChemicalUsage_L_h",      ua.VariantType.Double,  "L/h",   0,    100,   4.5),
    ("WaterUsage_L_h",          ua.VariantType.Double,  "L/h",   0,    1000,  120.0),
]
PRETREATMENT_HEALTH: list[VarDef] = [
    ("TankLevelLow",            ua.VariantType.Boolean, None, None, None, False),
    ("FilterClogged",           ua.VariantType.Boolean, None, None, None, False),
]

# -- PowderBooth -----------------------------------------------------------
POWDERBOOTH_PROCESS: list[VarDef] = [
    ("PowderFlow_g_min",        ua.VariantType.Double,  "g/min", 0,    500,   180.0),
    ("GunVoltage_kV",           ua.VariantType.Double,  "kV",    0,    100,   65.0),
    ("GunCurrent_uA",           ua.VariantType.Double,  "µA",    0,    200,   45.0),
    ("BoothAirflow_m3_h",       ua.VariantType.Double,  "m³/h",  0,    5000,  2800.0),
    ("FilmThickness_um",        ua.VariantType.Double,  "µm",    0,    500,   75.0),
    ("RecoveryEfficiency_Pct",  ua.VariantType.Double,  "%",     0,    100,   95.0),
]
POWDERBOOTH_CONSUMPTION: list[VarDef] = [
    ("PowderUsage_kg_h",        ua.VariantType.Double,  "kg/h",  0,    50,    8.0),
    ("CompressedAir_m3_h",      ua.VariantType.Double,  "m³/h",  0,    100,   15.0),
]
POWDERBOOTH_HEALTH: list[VarDef] = [
    ("GunClogAlarm",            ua.VariantType.Boolean, None, None, None, False),
    ("FilterDiffPressureHigh",  ua.VariantType.Boolean, None, None, None, False),
]

# -- CuringOven ------------------------------------------------------------
CURINGOVEN_PROCESS: list[VarDef] = [
    ("OvenTemperature_C",       ua.VariantType.Double,  "Cel",   0,    300,   195.0),
    ("OvenTempSetpoint_C",      ua.VariantType.Double,  "Cel",   0,    300,   200.0),
    ("HeatingPower_Pct",        ua.VariantType.Double,  "%",     0,    100,   65.0),
    ("ZoneTemp_Entry_C",        ua.VariantType.Double,  "Cel",   0,    300,   180.0),
    ("ZoneTemp_Center_C",       ua.VariantType.Double,  "Cel",   0,    300,   198.0),
    ("ZoneTemp_Exit_C",         ua.VariantType.Double,  "Cel",   0,    300,   190.0),
    ("ExhaustTemp_C",           ua.VariantType.Double,  "Cel",   0,    400,   145.0),
]
CURINGOVEN_CONSUMPTION: list[VarDef] = [
    ("GasConsumption_m3_h",     ua.VariantType.Double,  "m³/h",  0,    50,    12.0),
    ("ElectricalPower_kW",      ua.VariantType.Double,  "kW",    0,    200,   85.0),
]
CURINGOVEN_HEALTH: list[VarDef] = [
    ("OverTempAlarm",           ua.VariantType.Boolean, None, None, None, False),
    ("BurnerFault",             ua.VariantType.Boolean, None, None, None, False),
]

# -- ConveyorSystem --------------------------------------------------------
CONVEYOR_PROCESS: list[VarDef] = [
    ("ChainSpeed_m_min",        ua.VariantType.Double,  "m/min", 0,    20,    3.5),
    ("MotorCurrent_A",          ua.VariantType.Double,  "A",     0,    60,    22.0),
    ("MotorTemp_C",             ua.VariantType.Double,  "Cel",   -20,  150,   48.0),
    ("ChainTension_N",          ua.VariantType.Double,  "N",     0,    5000,  1800.0),
    ("PartCount",               ua.VariantType.UInt64,  "pcs",   0,    1e12,  0),
]
CONVEYOR_CONSUMPTION: list[VarDef] = [
    ("EnergyConsumption_kWh",   ua.VariantType.Double,  "kWh",   0,    1e9,   45600.0),
]
CONVEYOR_HEALTH: list[VarDef] = [
    ("ChainOverloadAlarm",      ua.VariantType.Boolean, None, None, None, False),
    ("LubricationLow",          ua.VariantType.Boolean, None, None, None, False),
]

# -- System-level ----------------------------------------------------------
SYSTEM_MONITORING: list[VarDef] = [
    ("SystemStatus",            ua.VariantType.Int32,   None,    0,    4,     1),
    ("PartsProduced",           ua.VariantType.UInt64,  "pcs",   0,    1e12,  245300),
    ("PartsRejected",           ua.VariantType.UInt64,  "pcs",   0,    1e12,  1240),
    ("LineSpeed_parts_h",       ua.VariantType.Double,  "1/h",   0,    500,   120.0),
    ("OEE_Overall",             ua.VariantType.Double,  "%",     0,    100,   82.5),
]

# Component definitions: name -> (process_vars, consumption_vars, health_vars)
COMPONENTS: dict[str, tuple[list[VarDef], list[VarDef], list[VarDef]]] = {
    "PretreatmentStation": (PRETREATMENT_PROCESS, PRETREATMENT_CONSUMPTION, PRETREATMENT_HEALTH),
    "PowderBooth":         (POWDERBOOTH_PROCESS, POWDERBOOTH_CONSUMPTION, POWDERBOOTH_HEALTH),
    "CuringOven":          (CURINGOVEN_PROCESS, CURINGOVEN_CONSUMPTION, CURINGOVEN_HEALTH),
    "ConveyorSystem":      (CONVEYOR_PROCESS, CONVEYOR_CONSUMPTION, CONVEYOR_HEALTH),
}


# ---------------------------------------------------------------------------
# Simulation state
# ---------------------------------------------------------------------------
@dataclass
class SimState:
    system_status: int = 1  # 0=Idle, 1=Running, 2=Paused, 3=Error, 4=Maintenance
    parts_produced: int = 245_300
    parts_rejected: int = 1_240
    line_speed: float = 120.0
    oee: float = 82.5
    energy_kwh: float = 45_600.0
    started_at: float = field(default_factory=time.monotonic)
    cycles: int = 0


SIM = SimState()
NODE_REGISTRY: dict[str, Node] = {}
NODE_TYPES: dict[str, ua.VariantType] = {}

DI_NS_IDX: int = 0
IA_NS_IDX: int = 0
MACH_NS_IDX: int = 0
ISA95_NS_IDX: int = 0
MACH_JOBS_NS_IDX: int = 0
ST_NS_IDX: int = 0
VENDOR_NS_IDX: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def vendor_sid(suffix: str) -> ua.NodeId:
    """Stable string NodeId in the vendor namespace."""
    return ua.NodeId(suffix, VENDOR_NS_IDX)


def variant_for(dtype: ua.VariantType, value: Any) -> ua.Variant:
    if dtype == ua.VariantType.Boolean:
        return ua.Variant(bool(value), dtype)
    if dtype in (ua.VariantType.Int32, ua.VariantType.UInt32,
                 ua.VariantType.Int64, ua.VariantType.UInt64):
        return ua.Variant(int(value), dtype)
    if dtype == ua.VariantType.Double:
        return ua.Variant(float(value), dtype)
    if dtype == ua.VariantType.String:
        return ua.Variant(str(value), dtype)
    return ua.Variant(value, dtype)


async def find_child_by_name(parent: Node, name: str) -> Node | None:
    """Find a direct child of *parent* by browse name (any namespace)."""
    for child in await parent.get_children():
        try:
            bn = await child.read_browse_name()
            if bn.Name == name:
                return child
        except Exception:
            continue
    return None


async def get_or_add_object(parent: Node, name: str, nodeid: ua.NodeId,
                            objecttype: ua.NodeId | None = None) -> Node:
    existing = await find_child_by_name(parent, name)
    if existing is not None:
        return existing
    if objecttype is not None:
        return await parent.add_object(nodeid, name, objecttype=objecttype)
    return await parent.add_object(nodeid, name)


async def set_or_add_property(parent: Node, name: str, nodeid: ua.NodeId,
                              value: ua.Variant,
                              bname_ns: int | None = None) -> Node:
    """Add or update a property.  When *bname_ns* is given the BrowseName
    is qualified into that namespace (needed for DI nameplate properties)."""
    existing = await find_child_by_name(parent, name)
    if existing is not None:
        try:
            await existing.write_value(value)
        except Exception as exc:
            log.debug("write %s skipped: %s", name, exc)
        return existing
    bname: Any = ua.QualifiedName(name, bname_ns) if bname_ns is not None else name
    return await parent.add_property(nodeid, bname, value)


async def add_eu_info(var: Node, unit: str | None,
                      lo: float | None, hi: float | None) -> None:
    """Attach EngineeringUnits + EURange properties."""
    if unit is not None:
        eu = ua.EUInformation()
        eu.NamespaceUri = "http://www.opcfoundation.org/UA/units/un/cefact"
        eu.UnitId = -1
        eu.DisplayName = ua.LocalizedText(unit)
        eu.Description = ua.LocalizedText(unit)
        await var.add_property(0, "EngineeringUnits",
                               ua.Variant(eu, ua.VariantType.ExtensionObject))
    if lo is not None and hi is not None:
        rng = ua.Range(Low=float(lo), High=float(hi))
        await var.add_property(0, "EURange",
                               ua.Variant(rng, ua.VariantType.ExtensionObject))


ANALOG_NUMERIC_TYPES = {
    ua.VariantType.Double, ua.VariantType.Float,
    ua.VariantType.Int16, ua.VariantType.UInt16,
    ua.VariantType.Int32, ua.VariantType.UInt32,
    ua.VariantType.Int64, ua.VariantType.UInt64,
}


async def retype_as_analog_unit(var: Node) -> None:
    """Switch HasTypeDefinition from BaseDataVariableType (i=63) to
    AnalogUnitType (i=2368)."""
    has_typedef = ua.NodeId(40)
    base_data_var = ua.NodeId(63)
    analog_unit = ua.NodeId(2368)
    try:
        await var.delete_reference(base_data_var, has_typedef, forward=True)
    except Exception as exc:
        log.debug("delete BaseDataVariableType ref failed: %s", exc)
    try:
        await var.add_reference(analog_unit, has_typedef,
                                forward=True, bidirectional=False)
    except Exception as exc:
        log.debug("add AnalogUnitType ref failed: %s", exc)


async def add_variable(parent: Node, reg_prefix: str,
                       vdef: VarDef) -> Node:
    """Create (or re-bind) a process variable under *parent* and register
    it in the global NODE_REGISTRY."""
    name, dtype, unit, lo, hi, initial = vdef
    key = f"{reg_prefix}.{name}" if reg_prefix else name
    nodeid = vendor_sid(key)

    existing = await find_child_by_name(parent, name)
    if existing is not None:
        try:
            await existing.write_value(variant_for(dtype, initial))
        except Exception:
            pass
        NODE_REGISTRY[key] = existing
        NODE_TYPES[key] = dtype
        return existing

    var = await parent.add_variable(
        nodeid, name, variant_for(dtype, initial), varianttype=dtype,
    )
    await var.set_writable(False)
    await add_eu_info(var, unit, lo, hi)
    if dtype in ANALOG_NUMERIC_TYPES and unit is not None and lo is not None:
        await retype_as_analog_unit(var)
    NODE_REGISTRY[key] = var
    NODE_TYPES[key] = dtype
    return var


async def find_type_by_name(name: str, root: Node) -> Node | None:
    """Recursively search the ObjectType hierarchy for a type node."""
    for child in await root.get_children():
        try:
            bn = await child.read_browse_name()
            if bn.Name == name:
                return child
            deeper = await find_type_by_name(name, child)
            if deeper:
                return deeper
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Address space construction
# ---------------------------------------------------------------------------
async def build_address_space(server: Server) -> Node:
    global DI_NS_IDX, IA_NS_IDX, MACH_NS_IDX, ISA95_NS_IDX
    global MACH_JOBS_NS_IDX, ST_NS_IDX, VENDOR_NS_IDX

    # Load all companion nodesets in dependency order.
    for ns_file in NODESET_FILES:
        ns_path = NODESETS_DIR / ns_file
        if not ns_path.exists():
            log.error("Nodeset not found: %s", ns_path)
            sys.exit(1)
        log.info("Loading nodeset: %s", ns_file)
        await server.import_xml(str(ns_path), strict_mode=False)
        log.info("Loaded %s", ns_file)

    # Register all namespaces.
    DI_NS_IDX = await server.register_namespace(DI_NAMESPACE)
    IA_NS_IDX = await server.register_namespace(IA_NAMESPACE)
    MACH_NS_IDX = await server.register_namespace(MACHINERY_NAMESPACE)
    ISA95_NS_IDX = await server.register_namespace(ISA95_NAMESPACE)
    MACH_JOBS_NS_IDX = await server.register_namespace(MACHINERY_JOBS_NAMESPACE)
    ST_NS_IDX = await server.register_namespace(ST_NAMESPACE)
    VENDOR_NS_IDX = await server.register_namespace(VENDOR_NAMESPACE)
    log.info("Namespaces -- DI=%d IA=%d Machinery=%d ISA95=%d MachJobs=%d ST=%d Vendor=%d",
             DI_NS_IDX, IA_NS_IDX, MACH_NS_IDX, ISA95_NS_IDX,
             MACH_JOBS_NS_IDX, ST_NS_IDX, VENDOR_NS_IDX)

    objects = server.nodes.objects
    obj_types = server.nodes.base_object_type

    # Locate ST types in the type hierarchy.
    st_sys_type = await find_type_by_name("STSysType", obj_types)
    st_comp_type = await find_type_by_name("STCompType", obj_types)
    st_base_ctrl_type = await find_type_by_name("STBaseControllerType", obj_types)
    fg_type = await find_type_by_name("FunctionalGroupType", obj_types)

    if st_sys_type is None:
        log.error("STSysType not found -- nodeset import may have failed")
        sys.exit(1)
    log.info("Found STSysType: %s", st_sys_type.nodeid)
    if st_comp_type:
        log.info("Found STCompType: %s", st_comp_type.nodeid)
    if st_base_ctrl_type:
        log.info("Found STBaseControllerType: %s", st_base_ctrl_type.nodeid)

    # Create a concrete vendor subtype of STSysType (which is abstract).
    coating_sys_type = await st_sys_type.add_object_type(
        ua.NodeId("CoatingSystemType", VENDOR_NS_IDX),
        "CoatingSystemType",
    )

    # Instantiate the coating system under Objects.
    coating_system = await objects.add_object(
        vendor_sid("CoatingSystem"),
        "CoatingSystem",
        objecttype=coating_sys_type.nodeid,
    )
    log.info("CoatingSystem instance created")

    # --- Identification (nameplate properties) ---
    identification = await get_or_add_object(
        coating_system, "Identification",
        vendor_sid("CoatingSystem.Identification"),
        objecttype=fg_type.nodeid if fg_type else None,
    )
    nameplate = {
        "Manufacturer":      ("SurfaceTech Industries", ua.VariantType.LocalizedText),
        "ManufacturerUri":   ("urn:surfacetech-industries", ua.VariantType.String),
        "Model":             ("PowderLine-X500", ua.VariantType.LocalizedText),
        "ProductCode":       ("PLX500-2026", ua.VariantType.String),
        "HardwareRevision":  ("HW-3.1.0", ua.VariantType.String),
        "SoftwareRevision":  ("FW-2.8.5", ua.VariantType.String),
        "DeviceRevision":    ("Rev-2", ua.VariantType.String),
        "SerialNumber":      ("PLX-500-001847", ua.VariantType.String),
        "RevisionCounter":   (8, ua.VariantType.Int32),
        "AssetId":           ("COATING-LINE-1-PLX500", ua.VariantType.String),
        "ComponentName":     ("Powder Coating Line 1", ua.VariantType.LocalizedText),
    }
    for prop_name, (val, vt) in nameplate.items():
        if vt == ua.VariantType.LocalizedText:
            v = ua.Variant(ua.LocalizedText(str(val)), vt)
        else:
            v = variant_for(vt, val)
        await set_or_add_property(
            identification, prop_name,
            vendor_sid(f"CoatingSystem.Identification.{prop_name}"), v,
            bname_ns=DI_NS_IDX,
        )

    # --- System-level Monitoring ---
    monitoring = await get_or_add_object(
        coating_system, "Monitoring",
        vendor_sid("CoatingSystem.Monitoring"),
    )
    for vdef in SYSTEM_MONITORING:
        await add_variable(monitoring, "System", vdef)

    # --- Components folder ---
    components_folder = await get_or_add_object(
        coating_system, "Components",
        vendor_sid("CoatingSystem.Components"),
    )

    # Create each component under Components.
    for comp_name, (proc_vars, cons_vars, health_vars) in COMPONENTS.items():
        comp_prefix = f"CoatingSystem.Components.{comp_name}"

        # Try type instantiation first; fall back to plain object.
        if st_comp_type is not None:
            try:
                vendor_comp_type = await st_comp_type.add_object_type(
                    ua.NodeId(f"{comp_name}Type", VENDOR_NS_IDX),
                    f"{comp_name}Type",
                )
                comp_node = await components_folder.add_object(
                    vendor_sid(comp_prefix),
                    comp_name,
                    objecttype=vendor_comp_type.nodeid,
                )
            except Exception as exc:
                log.warning("Type instantiation for %s failed (%s), creating plain object",
                            comp_name, exc)
                comp_node = await get_or_add_object(
                    components_folder, comp_name, vendor_sid(comp_prefix))
        else:
            comp_node = await get_or_add_object(
                components_folder, comp_name, vendor_sid(comp_prefix))

        # Monitoring sub-folders: Process, Consumption, Health
        comp_monitoring = await get_or_add_object(
            comp_node, "Monitoring",
            vendor_sid(f"{comp_prefix}.Monitoring"),
        )
        process_folder = await get_or_add_object(
            comp_monitoring, "Process",
            vendor_sid(f"{comp_prefix}.Monitoring.Process"),
        )
        consumption_folder = await get_or_add_object(
            comp_monitoring, "Consumption",
            vendor_sid(f"{comp_prefix}.Monitoring.Consumption"),
        )
        health_folder = await get_or_add_object(
            comp_monitoring, "Health",
            vendor_sid(f"{comp_prefix}.Monitoring.Health"),
        )

        reg_prefix = comp_name
        for vdef in proc_vars:
            await add_variable(process_folder, reg_prefix, vdef)
        for vdef in cons_vars:
            await add_variable(consumption_folder, reg_prefix, vdef)
        for vdef in health_vars:
            await add_variable(health_folder, reg_prefix, vdef)

        # Component identification
        comp_ident = await get_or_add_object(
            comp_node, "Identification",
            vendor_sid(f"{comp_prefix}.Identification"),
            objecttype=fg_type.nodeid if fg_type else None,
        )
        await set_or_add_property(
            comp_ident, "Manufacturer",
            vendor_sid(f"{comp_prefix}.Identification.Manufacturer"),
            ua.Variant(ua.LocalizedText("SurfaceTech Industries"),
                       ua.VariantType.LocalizedText),
            bname_ns=DI_NS_IDX,
        )
        await set_or_add_property(
            comp_ident, "SerialNumber",
            vendor_sid(f"{comp_prefix}.Identification.SerialNumber"),
            variant_for(ua.VariantType.String, f"PLX-{comp_name[:3].upper()}-{1000 + hash(comp_name) % 9000:04d}"),
            bname_ns=DI_NS_IDX,
        )

        log.info("Component %s created with %d variables",
                 comp_name,
                 len(proc_vars) + len(cons_vars) + len(health_vars))

    # --- Controller ---
    if st_base_ctrl_type is not None:
        try:
            controller = await objects.add_object(
                vendor_sid("CoatingSystem.Controller"),
                "LineController",
                objecttype=st_base_ctrl_type.nodeid,
            )
            log.info("LineController created (STBaseControllerType)")
        except Exception as exc:
            log.warning("Controller instantiation failed (%s), creating plain object", exc)
            controller = await objects.add_object(
                vendor_sid("CoatingSystem.Controller"), "LineController")
    else:
        controller = await objects.add_object(
            vendor_sid("CoatingSystem.Controller"), "LineController")

    ctrl_ident = await get_or_add_object(
        controller, "Identification",
        vendor_sid("CoatingSystem.Controller.Identification"),
        objecttype=fg_type.nodeid if fg_type else None,
    )
    await set_or_add_property(
        ctrl_ident, "Manufacturer",
        vendor_sid("CoatingSystem.Controller.Identification.Manufacturer"),
        ua.Variant(ua.LocalizedText("SurfaceTech Industries"),
                   ua.VariantType.LocalizedText),
        bname_ns=DI_NS_IDX,
    )
    await set_or_add_property(
        ctrl_ident, "Model",
        vendor_sid("CoatingSystem.Controller.Identification.Model"),
        ua.Variant(ua.LocalizedText("PLC-X500 Controller"),
                   ua.VariantType.LocalizedText),
        bname_ns=DI_NS_IDX,
    )

    total_vars = (len(SYSTEM_MONITORING)
                  + sum(len(p) + len(c) + len(h)
                        for p, c, h in COMPONENTS.values()))
    log.info(
        "Address space built -- 1 system, %d components, 1 controller, %d variables",
        len(COMPONENTS), total_vars,
    )
    return coating_system


# ---------------------------------------------------------------------------
# Simulation loop
# ---------------------------------------------------------------------------
async def write(name: str, value: Any) -> None:
    node = NODE_REGISTRY.get(name)
    if node is None:
        return
    try:
        if isinstance(value, ua.Variant):
            await node.write_value(value)
        else:
            vt = NODE_TYPES.get(name, ua.VariantType.Double)
            await node.write_value(variant_for(vt, value))
    except Exception as exc:
        log.debug("write %s failed: %s", name, exc)


def pid_step(current: float, setpoint: float, k: float = 0.1,
             noise: float = 0.4) -> float:
    return current + k * (setpoint - current) + random.uniform(-noise, noise)


async def simulation_loop() -> None:
    rng = random.Random(42)
    oven_temp = 195.0
    zone_entry = 180.0
    zone_center = 198.0
    zone_exit = 190.0
    exhaust_temp = 145.0
    motor_temp = 48.0
    tank_temp = 55.0

    while True:
        SIM.cycles += 1
        running = SIM.system_status == 1  # Running

        # --- Production counters ---
        if running:
            parts_tick = max(0, int(SIM.line_speed / 3600.0 * INTERVAL))
            rejects_tick = sum(1 for _ in range(max(1, parts_tick))
                               if rng.random() < 0.004)
            SIM.parts_produced += max(0, parts_tick - rejects_tick)
            SIM.parts_rejected += rejects_tick
            SIM.line_speed = pid_step(SIM.line_speed, 120.0, k=0.1, noise=1.5)
            SIM.energy_kwh += 85.0 * INTERVAL / 3600.0
        else:
            SIM.line_speed = max(0.0, SIM.line_speed - 10.0 * INTERVAL)

        SIM.oee = 82.5 + 3.0 * math.sin(SIM.cycles / 500.0)

        await write("System.SystemStatus", SIM.system_status)
        await write("System.PartsProduced", SIM.parts_produced)
        await write("System.PartsRejected", SIM.parts_rejected)
        await write("System.LineSpeed_parts_h", round(SIM.line_speed, 2))
        await write("System.OEE_Overall", round(SIM.oee, 2))

        # --- PretreatmentStation ---
        tank_temp = pid_step(tank_temp, 55.0, k=0.08, noise=0.3)
        await write("PretreatmentStation.TankTemperature_C", round(tank_temp, 2))
        await write("PretreatmentStation.PhLevel",
                     round(7.2 + 0.3 * math.sin(SIM.cycles / 400.0) + rng.uniform(-0.05, 0.05), 3))
        await write("PretreatmentStation.ChemicalConcentration_Pct",
                     round(12.5 + rng.uniform(-0.3, 0.3), 2))
        await write("PretreatmentStation.SprayPressure_bar",
                     round(3.2 + rng.uniform(-0.1, 0.1), 3))
        await write("PretreatmentStation.RinseWaterFlow_L_min",
                     round(18.0 + rng.uniform(-0.5, 0.5), 2))
        await write("PretreatmentStation.ChemicalUsage_L_h",
                     round(4.5 + rng.uniform(-0.2, 0.2), 2))
        await write("PretreatmentStation.WaterUsage_L_h",
                     round(120.0 + rng.uniform(-3, 3), 1))

        # --- PowderBooth ---
        await write("PowderBooth.PowderFlow_g_min",
                     round(180.0 + rng.uniform(-5, 5), 2))
        await write("PowderBooth.GunVoltage_kV",
                     round(65.0 + rng.uniform(-0.5, 0.5), 2))
        await write("PowderBooth.GunCurrent_uA",
                     round(45.0 + rng.uniform(-1.5, 1.5), 2))
        await write("PowderBooth.BoothAirflow_m3_h",
                     round(2800.0 + rng.uniform(-30, 30), 1))
        await write("PowderBooth.FilmThickness_um",
                     round(75.0 + 5.0 * math.sin(SIM.cycles / 200.0) + rng.uniform(-1, 1), 2))
        await write("PowderBooth.RecoveryEfficiency_Pct",
                     round(95.0 + rng.uniform(-0.5, 0.5), 2))
        await write("PowderBooth.PowderUsage_kg_h",
                     round(8.0 + rng.uniform(-0.3, 0.3), 2))
        await write("PowderBooth.CompressedAir_m3_h",
                     round(15.0 + rng.uniform(-0.5, 0.5), 2))

        # --- CuringOven ---
        oven_temp = pid_step(oven_temp, 200.0, k=0.12, noise=0.5)
        zone_entry = pid_step(zone_entry, 180.0, k=0.08, noise=0.8)
        zone_center = pid_step(zone_center, 200.0, k=0.10, noise=0.4)
        zone_exit = pid_step(zone_exit, 190.0, k=0.09, noise=0.6)
        exhaust_temp = pid_step(exhaust_temp, 145.0, k=0.06, noise=0.5)
        heating_pct = max(0.0, min(100.0, 65.0 + 20.0 * (200.0 - oven_temp)))

        await write("CuringOven.OvenTemperature_C", round(oven_temp, 2))
        await write("CuringOven.OvenTempSetpoint_C", 200.0)
        await write("CuringOven.HeatingPower_Pct", round(heating_pct, 2))
        await write("CuringOven.ZoneTemp_Entry_C", round(zone_entry, 2))
        await write("CuringOven.ZoneTemp_Center_C", round(zone_center, 2))
        await write("CuringOven.ZoneTemp_Exit_C", round(zone_exit, 2))
        await write("CuringOven.ExhaustTemp_C", round(exhaust_temp, 2))
        await write("CuringOven.GasConsumption_m3_h",
                     round(12.0 + rng.uniform(-0.4, 0.4), 2))
        await write("CuringOven.ElectricalPower_kW",
                     round(85.0 + rng.uniform(-2, 2), 2))

        # --- ConveyorSystem ---
        motor_temp = pid_step(motor_temp, 50.0, k=0.05, noise=0.3)
        SIM.energy_kwh += 5.0 * INTERVAL / 3600.0
        await write("ConveyorSystem.ChainSpeed_m_min",
                     round(3.5 + rng.uniform(-0.05, 0.05), 3))
        await write("ConveyorSystem.MotorCurrent_A",
                     round(22.0 + rng.uniform(-0.4, 0.4), 2))
        await write("ConveyorSystem.MotorTemp_C", round(motor_temp, 2))
        await write("ConveyorSystem.ChainTension_N",
                     round(1800.0 + rng.uniform(-20, 20), 1))
        await write("ConveyorSystem.PartCount", SIM.parts_produced)
        await write("ConveyorSystem.EnergyConsumption_kWh",
                     round(SIM.energy_kwh, 3))

        await asyncio.sleep(INTERVAL)


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------
app = FastAPI(title="SurfaceTech Coating Simulator")
static_dir = Path(__file__).with_name("static")
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    index = static_dir / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return HTMLResponse(
        "<h1>SurfaceTech Coating Simulator</h1><p>See /api/status</p>"
    )


@app.get("/api/status")
async def status() -> JSONResponse:
    status_names = {0: "Idle", 1: "Running", 2: "Paused", 3: "Error", 4: "Maintenance"}
    return JSONResponse({
        "endpoint": f"opc.tcp://0.0.0.0:{PORT}/{ENDPOINT_PATH}",
        "uptime_seconds": round(time.monotonic() - SIM.started_at, 1),
        "cycles": SIM.cycles,
        "device": "CoatingSystem",
        "components": list(COMPONENTS.keys()),
        "variable_count": (len(SYSTEM_MONITORING)
                           + sum(len(p) + len(c) + len(h)
                                 for p, c, h in COMPONENTS.values())),
        "system_status": status_names.get(SIM.system_status, "Unknown"),
        "parts_produced": SIM.parts_produced,
        "parts_rejected": SIM.parts_rejected,
        "line_speed": round(SIM.line_speed, 2),
    })


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
async def main() -> None:
    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://0.0.0.0:{PORT}/{ENDPOINT_PATH}")
    server.set_server_name("SurfaceTech Coating Line Simulator")

    # Self-signed certificate for Sign / SignAndEncrypt endpoints.
    cert_dir = Path("/tmp/opcua-surfacetech-certs")
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_file = cert_dir / "server-cert.der"
    key_file = cert_dir / "server-key.pem"
    if not cert_file.exists():
        from asyncua.crypto.cert_gen import setup_self_signed_certificate
        from cryptography.x509.oid import ExtendedKeyUsageOID
        await setup_self_signed_certificate(
            key_file, cert_file,
            "urn:surfacetech-demo:coating-system",
            host_name="surfacetech-simulator",
            cert_use=[ExtendedKeyUsageOID.SERVER_AUTH],
            subject_attrs={
                "commonName": "SurfaceTech Coating Line Simulator",
                "organizationName": "SurfaceTechDemo",
                "countryName": "CH",
            },
        )
        log.info("Generated self-signed OPC UA certificate")
    await server.load_certificate(str(cert_file))
    await server.load_private_key(str(key_file))
    server.set_security_policy([
        ua.SecurityPolicyType.NoSecurity,
        ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
        ua.SecurityPolicyType.Basic256Sha256_Sign,
    ])
    server.set_security_IDs(["Anonymous"])

    await build_address_space(server)

    async with server:
        log.info("OPC UA server listening on opc.tcp://0.0.0.0:%d/%s",
                 PORT, ENDPOINT_PATH)

        config = uvicorn.Config(app, host="0.0.0.0", port=WEB_PORT,
                                log_level="warning")
        web_server = uvicorn.Server(config)
        sim_task = asyncio.create_task(simulation_loop())
        web_task = asyncio.create_task(web_server.serve())

        loop = asyncio.get_event_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        await stop.wait()

        sim_task.cancel()
        web_server.should_exit = True
        await asyncio.gather(sim_task, web_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
