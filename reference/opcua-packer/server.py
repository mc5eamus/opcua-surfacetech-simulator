"""
OPC UA Packer Simulator -- TMC Cigarette Packer (PROTOS-style)

Purpose
-------
A second OPC UA simulator, additive to the cigarette-maker simulator under
`simulator/opcua-server/`. This one is fully OPC UA DI (OPC 10000-100)
compliant so that the Azure IoT Operations connector for OPC UA + Akri
discovery services can automatically discover the device and its assets
without any hand-written Asset YAML.

See: https://learn.microsoft.com/en-us/azure/iot-operations/discover-manage-assets/howto-detect-opc-ua-assets

Address space
-------------
- DI NodeSet2.xml is loaded so DeviceType / IVendorNameplateType /
  ParameterSet / FunctionalGroupType / DeviceSet exist.
- A `tmcpackermachine` device is created under Objects/DeviceSet typed
  as DI DeviceType.
- IVendorNameplateType properties (Manufacturer, Model, SerialNumber,
  HardwareRevision, SoftwareRevision, ProductCode, DeviceManual,
  DeviceRevision, RevisionCounter) are populated.
- ParameterSet contains all process variables.
- FunctionalGroups (Hopper, GlueStation, FoilWrapper, Cellophaner,
  Cartoner, Conveyor, Status) organise the variables.

Environment variables
---------------------
OPCUA_PORT          OPC UA listening port (default 4840)
WEB_PORT            Web UI port (default 8080)
PUBLISH_INTERVAL_MS Telemetry interval in ms (default 1000)
ENDPOINT_PATH       URL path of the OPC UA endpoint (default aio-demo-packer)
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
from datetime import datetime, timezone
from enum import IntEnum
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
ENDPOINT_PATH = os.getenv("ENDPOINT_PATH", "aio-demo-packer")
INTERVAL = PUBLISH_MS / 1000.0

DI_NAMESPACE = "http://opcfoundation.org/UA/DI/"
PACKML_NAMESPACE = "http://opcfoundation.org/UA/PackML/"
TMC_NAMESPACE = "http://opcfoundation.org/UA/TMC/"
VENDOR_NAMESPACE = "urn:aio-demo:tmc:packer"

DI_NODESET_PATH = Path(__file__).with_name("nodesets") / "Opc.Ua.Di.NodeSet2.xml"
PACKML_NODESET_PATH = Path(__file__).with_name("nodesets") / "Opc.Ua.PackML.NodeSet2.xml"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aio-demo-opcua-packer")


# ---------------------------------------------------------------------------
# TMC PackML enumerations (mirror OPC 30060)
# ---------------------------------------------------------------------------
class MachineState(IntEnum):
    Idle = 0
    Execute = 1
    Held = 2
    Suspended = 3
    Aborted = 4


class ControlMode(IntEnum):
    Production = 0
    Manual = 1
    Maintenance = 2


# ---------------------------------------------------------------------------
# Variable catalogue
# ---------------------------------------------------------------------------
# Each entry: (browse_name, datatype, eu_unit, eu_low, eu_high, initial)
# `eu_unit` may be None for booleans / dimensionless values.
VarDef = tuple[str, ua.VariantType, str | None, float | None, float | None, Any]

DEVICE_VARS: list[VarDef] = [
    ("MachineState",            ua.VariantType.Int32,   None,    0,    4,   int(MachineState.Idle)),
    ("ControlMode",             ua.VariantType.Int32,   None,    0,    2,   int(ControlMode.Production)),
    ("GoodPackCount",           ua.VariantType.UInt64,  "pcs",   0,    1e12, 0),
    ("RejectedPackCount",       ua.VariantType.UInt64,  "pcs",   0,    1e12, 0),
    ("ProductionSpeed_PPM",     ua.VariantType.Double,  "1/min", 0,    600.0, 0.0),
    ("MotorRunningHours",       ua.VariantType.Double,  "h",     0,    1e6,  834.0),
    ("MotorStartStopCounter",   ua.VariantType.UInt32,  "pcs",   0,    1e9,  212),
    ("EnergyConsumption_kWh",   ua.VariantType.Double,  "kWh",   0,    1e9,  91250.0),
    ("OEE_Availability",        ua.VariantType.Double,  "%",     0,    100,  92.5),
    ("OEE_Performance",         ua.VariantType.Double,  "%",     0,    100,  88.0),
    ("OEE_Quality",             ua.VariantType.Double,  "%",     0,    100,  99.4),
    ("OEE_Overall",             ua.VariantType.Double,  "%",     0,    100,  80.8),
    ("Alarm_Active",            ua.VariantType.Boolean, None,    None, None, False),
    ("Alarm_EmergencyStop",     ua.VariantType.Boolean, None,    None, None, False),
    ("Alarm_DoorOpen",          ua.VariantType.Boolean, None,    None, None, False),
    ("CurrentBrand",            ua.VariantType.String,  None,    None, None, "Brand-A King-Size"),
    ("CurrentBatchId",          ua.VariantType.String,  None,    None, None, "BATCH-2026-0422-001"),
]

HOPPER_VARS: list[VarDef] = [
    ("TobaccoLevel_Pct",        ua.VariantType.Double, "%",     0,   100,   72.0),
    ("FeedRate_kg_h",           ua.VariantType.Double, "kg/h",  0,   500,   180.0),
    ("HopperWeight_kg",         ua.VariantType.Double, "kg",    0,   1000,  640.0),
    ("BlockageAlarm",           ua.VariantType.Boolean,None,    None,None,  False),
    ("LowLevelAlarm",           ua.VariantType.Boolean,None,    None,None,  False),
    ("FeederMotorCurrent_A",    ua.VariantType.Double, "A",     0,   30,    8.4),
    ("FeederMotorTemp_C",       ua.VariantType.Double, "Cel",   -20, 150,   42.0),
]

GLUESTATION_VARS: list[VarDef] = [
    ("GlueLevel_Pct",           ua.VariantType.Double, "%",     0,   100,   85.0),
    ("GlueTemperature_C",       ua.VariantType.Double, "Cel",   0,   200,   165.0),
    ("GlueTempSetpoint_C",      ua.VariantType.Double, "Cel",   0,   200,   165.0),
    ("NozzlePressure_bar",      ua.VariantType.Double, "bar",   0,   10,    3.4),
    ("NozzleFlow_g_min",        ua.VariantType.Double, "g/min", 0,   500,   120.0),
    ("HeaterPowerOutput_Pct",   ua.VariantType.Double, "%",     0,   100,   42.0),
    ("LowGlueAlarm",            ua.VariantType.Boolean,None,    None,None,  False),
    ("OverTempAlarm",           ua.VariantType.Boolean,None,    None,None,  False),
    ("NozzleClogAlarm",         ua.VariantType.Boolean,None,    None,None,  False),
]

FOILWRAPPER_VARS: list[VarDef] = [
    ("FoilTension_N",           ua.VariantType.Double, "N",     0,   200,   45.0),
    ("FoilSpeed_mm_s",          ua.VariantType.Double, "mm/s",  0,   3000,  1850.0),
    ("FoilRollDiameter_mm",     ua.VariantType.Double, "mm",    0,   500,   320.0),
    ("FoilSpliceCounter",       ua.VariantType.UInt32, "pcs",   0,   1e9,   18),
    ("FoilBreakAlarm",          ua.VariantType.Boolean,None,    None,None,  False),
    ("FoilLowAlarm",            ua.VariantType.Boolean,None,    None,None,  False),
    ("FoilTrackingError_mm",    ua.VariantType.Double, "mm",    -10, 10,    0.2),
]

CELLOPHANER_VARS: list[VarDef] = [
    ("FilmFeedSpeed_mm_s",      ua.VariantType.Double, "mm/s",  0,   3000,  1900.0),
    ("FilmRollDiameter_mm",     ua.VariantType.Double, "mm",    0,   500,   285.0),
    ("SealTemp_Top_C",          ua.VariantType.Double, "Cel",   0,   250,   180.0),
    ("SealTemp_Side_C",         ua.VariantType.Double, "Cel",   0,   250,   175.0),
    ("SealTemp_Bottom_C",       ua.VariantType.Double, "Cel",   0,   250,   178.0),
    ("SealPressure_bar",        ua.VariantType.Double, "bar",   0,   10,    4.2),
    ("TearTapeAlignment_mm",    ua.VariantType.Double, "mm",    -5,  5,     0.1),
    ("FilmBreakAlarm",          ua.VariantType.Boolean,None,    None,None,  False),
    ("PoorSealAlarm",           ua.VariantType.Boolean,None,    None,None,  False),
]

CARTONER_VARS: list[VarDef] = [
    ("CartonsPerMinute",        ua.VariantType.Double, "1/min", 0,   100,   60.0),
    ("CartonMagazineLevel_Pct", ua.VariantType.Double, "%",     0,   100,   78.0),
    ("GlueGunTemp_C",           ua.VariantType.Double, "Cel",   0,   250,   190.0),
    ("GlueDotsPerCarton",       ua.VariantType.UInt32, "pcs",   0,   100,   8),
    ("CartonJamCount",          ua.VariantType.UInt32, "pcs",   0,   1e9,   3),
    ("RejectedCartons",         ua.VariantType.UInt32, "pcs",   0,   1e9,   17),
    ("MagazineEmptyAlarm",      ua.VariantType.Boolean,None,    None,None,  False),
    ("CartonJamAlarm",          ua.VariantType.Boolean,None,    None,None,  False),
]

CONVEYOR_VARS: list[VarDef] = [
    ("BeltSpeed_mm_s",          ua.VariantType.Double, "mm/s",  0,   2000,  1200.0),
    ("MotorCurrent_A",          ua.VariantType.Double, "A",     0,   60,    18.5),
    ("MotorTemp_C",             ua.VariantType.Double, "Cel",   -20, 150,   55.0),
    ("MotorRPM",                ua.VariantType.Double, "1/min", 0,   3000,  1450.0),
    ("BeltSlipPct",             ua.VariantType.Double, "%",     0,   100,   1.2),
    ("VibrationLevel_mm_s",     ua.VariantType.Double, "mm/s",  0,   30,    1.8),
    ("EncoderPosition",         ua.VariantType.Int64,  "pcs",   -1e12,1e12,  0),
    ("OverloadAlarm",           ua.VariantType.Boolean,None,    None,None,  False),
]

SUBMODULES: dict[str, list[VarDef]] = {
    "Hopper":      HOPPER_VARS,
    "GlueStation": GLUESTATION_VARS,
    "FoilWrapper": FOILWRAPPER_VARS,
    "Cellophaner": CELLOPHANER_VARS,
    "Cartoner":    CARTONER_VARS,
    "Conveyor":    CONVEYOR_VARS,
}


# ---------------------------------------------------------------------------
# Simulation state
# ---------------------------------------------------------------------------
@dataclass
class SimState:
    machine_state: MachineState = MachineState.Execute
    control_mode: ControlMode = ControlMode.Production
    good_packs: int = 1_245_300
    rejected_packs: int = 8_140
    speed_ppm: float = 580.0
    motor_hours: float = 834.0
    energy_kwh: float = 91_250.0
    started_at: float = field(default_factory=time.monotonic)
    cycles: int = 0


SIM = SimState()
NODE_REGISTRY: dict[str, Node] = {}      # "<group>.<name>" -> Node
NODE_TYPES: dict[str, ua.VariantType] = {}
DI_NS_IDX: int = 0
PACKML_NS_IDX: int = 0
TMC_NS_IDX: int = 0
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
    """Find a direct child of `parent` by browse name (any namespace)."""
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
    """Add or update a property. If `bname_ns` is given, the BrowseName is
    qualified into that namespace (required for strict DI: nameplate
    properties must live in the DI namespace)."""
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
    """Attach EngineeringUnits + EURange properties (DI/Akri reads these)."""
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
    AnalogUnitType (i=2368) so DI/PackML clients render the variable as a
    proper engineering value with units."""
    has_typedef = ua.NodeId(40)  # HasTypeDefinition
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


async def add_parameter(parameter_set: Node, group_name: str,
                        vdef: VarDef) -> Node:
    name, dtype, unit, lo, hi, initial = vdef
    nodeid = vendor_sid(f"{group_name}.{name}" if group_name else name)
    key = f"{group_name}.{name}" if group_name else name
    existing = await find_child_by_name(parameter_set, name)
    if existing is not None:
        # Type instantiation already created this variable; just rebind it.
        try:
            await existing.write_value(variant_for(dtype, initial))
        except Exception:
            pass
        NODE_REGISTRY[key] = existing
        NODE_TYPES[key] = dtype
        return existing
    var = await parameter_set.add_variable(
        nodeid, name, variant_for(dtype, initial), varianttype=dtype,
    )
    await var.set_writable(False)
    await add_eu_info(var, unit, lo, hi)
    # Strict DI: numeric variables that carry engineering units should be
    # AnalogUnitType, not plain BaseDataVariableType. This is what real
    # DI/PackML servers expose so clients (UAExpert, Kepware) render them
    # with units automatically.
    if dtype in ANALOG_NUMERIC_TYPES and unit is not None and lo is not None:
        await retype_as_analog_unit(var)
    NODE_REGISTRY[key] = var
    NODE_TYPES[key] = dtype
    return var


# ---------------------------------------------------------------------------
# Address space construction
# ---------------------------------------------------------------------------
async def build_address_space(server: Server) -> Node:
    global DI_NS_IDX, PACKML_NS_IDX, TMC_NS_IDX, VENDOR_NS_IDX

    # Load the OPC UA DI companion nodeset.
    if not DI_NODESET_PATH.exists():
        log.error("DI nodeset not found at %s", DI_NODESET_PATH)
        sys.exit(1)
    log.info("Loading DI nodeset: %s", DI_NODESET_PATH)
    await server.import_xml(str(DI_NODESET_PATH))
    log.info("DI nodeset loaded")

    # Load the OPC UA PackML (OPC 30060) companion nodeset so the device can
    # expose the standard packaging-machine status/admin model alongside DI.
    if PACKML_NODESET_PATH.exists():
        log.info("Loading PackML nodeset: %s", PACKML_NODESET_PATH)
        await server.import_xml(str(PACKML_NODESET_PATH))
        log.info("PackML nodeset loaded")
    else:
        log.warning("PackML nodeset not found at %s -- skipping",
                    PACKML_NODESET_PATH)

    DI_NS_IDX = await server.register_namespace(DI_NAMESPACE)
    PACKML_NS_IDX = await server.register_namespace(PACKML_NAMESPACE)
    TMC_NS_IDX = await server.register_namespace(TMC_NAMESPACE)
    VENDOR_NS_IDX = await server.register_namespace(VENDOR_NAMESPACE)
    log.info("Namespaces -- DI=%d PackML=%d TMC=%d vendor=%d",
             DI_NS_IDX, PACKML_NS_IDX, TMC_NS_IDX, VENDOR_NS_IDX)

    objects = server.nodes.objects

    # DeviceSet -- created by the DI nodeset import. Locate it by browse.
    device_set: Node | None = None
    for child in await objects.get_children():
        bn = await child.read_browse_name()
        if bn.Name == "DeviceSet":
            device_set = child
            break
    if device_set is None:
        log.warning("DeviceSet not present after DI import; creating manually")
        device_set = await objects.add_object(
            ua.NodeId("DeviceSet", DI_NS_IDX), "DeviceSet"
        )

    # DeviceType from the DI namespace (well-known browse name).
    # Walk Types/ObjectTypes/BaseObjectType subtree to find DI types.
    obj_types = server.nodes.base_object_type
    async def find_type(name: str, root: Node) -> Node | None:
        for child in await root.get_children():
            try:
                bn = await child.read_browse_name()
                if bn.Name == name:
                    return child
                deeper = await find_type(name, child)
                if deeper:
                    return deeper
            except Exception:
                continue
        return None

    device_type_node = await find_type("DeviceType", obj_types)
    fg_type_node = await find_type("FunctionalGroupType", obj_types)
    parameterset_type_node = await find_type("ParameterSetType", obj_types)
    ivendor_nameplate_type = await find_type("IVendorNameplateType", obj_types)
    itag_nameplate_type = await find_type("ITagNameplateType", obj_types)
    idevice_health_type = await find_type("IDeviceHealthType", obj_types)
    if device_type_node is None:
        log.error("DI DeviceType not found -- DI import may have failed")
        sys.exit(1)

    # Create a concrete vendor subtype of DI DeviceType (DI DeviceType is abstract).
    vendor_device_type = await device_type_node.add_object_type(
        ua.NodeId("tmcpackermachineType", VENDOR_NS_IDX),
        "tmcpackermachineType",
    )

    # Strict DI: declare HasInterface references at the type level so every
    # instance formally implements the IVendorNameplateType, ITagNameplateType
    # and IDeviceHealthType interfaces (OPC 10000-100, ch. 5).
    has_interface_ref = ua.NodeId(17603, 0)  # standard HasInterface ReferenceType
    for iface in (ivendor_nameplate_type, itag_nameplate_type, idevice_health_type):
        if iface is not None:
            await vendor_device_type.add_reference(
                iface.nodeid, has_interface_ref, forward=True, bidirectional=True,
            )
        else:
            log.warning("DI interface type not found; skipping HasInterface")

    # Create the packer device as instance of the vendor type.
    device = await device_set.add_object(
        vendor_sid("tmcpackermachine"),
        "tmcpackermachine",
        objecttype=vendor_device_type.nodeid,
    )
    # Mirror HasInterface on the instance as well (some DI clients only
    # inspect the instance, not the type).
    for iface in (ivendor_nameplate_type, itag_nameplate_type, idevice_health_type):
        if iface is not None:
            await device.add_reference(
                iface.nodeid, has_interface_ref, forward=True, bidirectional=True,
            )

    # Vendor nameplate properties (IVendorNameplateType).
    nameplate = {
        "Manufacturer":      ("Contoso Tobacco Machinery", ua.VariantType.LocalizedText),
        "ManufacturerUri":   ("urn:contoso:tobacco-machinery", ua.VariantType.String),
        "Model":             ("PROTOS-M5 Packer", ua.VariantType.LocalizedText),
        "ProductCode":       ("PROTOS-M5-2026", ua.VariantType.String),
        "HardwareRevision":  ("HW-2.4.1", ua.VariantType.String),
        "SoftwareRevision":  ("FW-7.12.3", ua.VariantType.String),
        "DeviceRevision":    ("Rev-3", ua.VariantType.String),
        "DeviceManual":      ("https://contoso.example/protos-m5/manual",
                              ua.VariantType.String),
        "SerialNumber":      ("PRT-M5-000482", ua.VariantType.String),
        "RevisionCounter":   (12, ua.VariantType.Int32),
        "AssetId":           ("PACKER-LINE-3-PROTOS-M5", ua.VariantType.String),
        "ComponentName":     ("Cigarette Packer Line 3", ua.VariantType.LocalizedText),
    }
    for prop_name, (val, vt) in nameplate.items():
        if vt == ua.VariantType.LocalizedText:
            v = ua.Variant(ua.LocalizedText(str(val)), vt)
        else:
            v = variant_for(vt, val)
        await set_or_add_property(
            device, prop_name,
            vendor_sid(f"tmcpackermachine.{prop_name}"), v,
            bname_ns=DI_NS_IDX,
        )

    # Strict DI: IDeviceHealthType requires a `DeviceHealth` variable typed as
    # the DI `DeviceHealthEnumeration` plus a `DeviceHealthAlarms` folder.
    device_health_dtype = ua.NodeId(6244, DI_NS_IDX)  # DI DeviceHealthEnumeration
    health_existing = await find_child_by_name(device, "DeviceHealth")
    if health_existing is None:
        health_var = await device.add_variable(
            vendor_sid("tmcpackermachine.DeviceHealth"),
            ua.QualifiedName("DeviceHealth", DI_NS_IDX),
            ua.Variant(0, ua.VariantType.Int32),  # 0 = NORMAL
            datatype=device_health_dtype,
        )
        await health_var.set_writable(False)
        NODE_REGISTRY["DeviceHealth"] = health_var
        NODE_TYPES["DeviceHealth"] = ua.VariantType.Int32
    else:
        NODE_REGISTRY["DeviceHealth"] = health_existing
        NODE_TYPES["DeviceHealth"] = ua.VariantType.Int32
    await get_or_add_object(
        device, "DeviceHealthAlarms",
        vendor_sid("tmcpackermachine.DeviceHealthAlarms"),
    )

    # Identification group (DI convention).
    identification = await get_or_add_object(
        device, "Identification",
        vendor_sid("tmcpackermachine.Identification"),
        objecttype=fg_type_node.nodeid if fg_type_node else None,
    )
    # Mirror a few key nameplate properties under Identification (Akri reads it).
    for prop_name in ("Manufacturer", "Model", "SerialNumber",
                      "HardwareRevision", "SoftwareRevision"):
        val, vt = nameplate[prop_name]
        if vt == ua.VariantType.LocalizedText:
            v = ua.Variant(ua.LocalizedText(str(val)), vt)
        else:
            v = variant_for(vt, val)
        await set_or_add_property(
            identification, prop_name,
            vendor_sid(f"tmcpackermachine.Identification.{prop_name}"), v,
            bname_ns=DI_NS_IDX,
        )

    # ParameterSet -- holds all device-level variables.
    parameter_set = await get_or_add_object(
        device, "ParameterSet",
        vendor_sid("tmcpackermachine.ParameterSet"),
        objecttype=parameterset_type_node.nodeid if parameterset_type_node else None,
    )
    for vdef in DEVICE_VARS:
        await add_parameter(parameter_set, "", vdef)

    # MethodSet (empty placeholder -- DI requires it on a Device).
    await get_or_add_object(
        device, "MethodSet",
        vendor_sid("tmcpackermachine.MethodSet"),
    )

    # Sub-module FunctionalGroups, each with their own ParameterSet so the
    # discovery surface mirrors the physical machine layout.
    for group_name, var_defs in SUBMODULES.items():
        group = await get_or_add_object(
            device, group_name,
            vendor_sid(f"tmcpackermachine.{group_name}"),
            objecttype=fg_type_node.nodeid if fg_type_node else None,
        )
        group_param_set = await get_or_add_object(
            group, "ParameterSet",
            vendor_sid(f"tmcpackermachine.{group_name}.ParameterSet"),
            objecttype=parameterset_type_node.nodeid if parameterset_type_node else None,
        )
        for vdef in var_defs:
            await add_parameter(group_param_set, group_name, vdef)

    # PackML (OPC 30060) -- attach a PackMLBaseObjectType instance under the
    # device so PackML-aware clients see the standard MachineState /
    # ExecuteState / CurMachSpeed / Admin variables.
    if PACKML_NS_IDX > 0:
        await build_packml_object(server, device)

    total_vars = len(DEVICE_VARS) + sum(len(v) for v in SUBMODULES.values())
    log.info(
        "Address space built -- 1 device, %d sub-modules, %d variables",
        len(SUBMODULES), total_vars,
    )
    return device


async def build_packml_object(server: Server, device: Node) -> None:
    """Instantiate a PackMLBaseObjectType under the device. Asyncua's
    `add_object(..., objecttype=...)` walks the type hierarchy and copies all
    HasComponent / HasProperty children with ModellingRule Mandatory, so the
    instance ends up with Status (PackMLStatusObjectType), Admin
    (PackMLAdminObjectType), PackMLVersion and TagID -- exactly what a real
    PackML device exposes."""
    obj_types = server.nodes.base_object_type

    async def find_type(name: str, root: Node) -> Node | None:
        for child in await root.get_children():
            try:
                bn = await child.read_browse_name()
                if bn.Name == name:
                    return child
                deeper = await find_type(name, child)
                if deeper:
                    return deeper
            except Exception:
                continue
        return None

    packml_type = await find_type("PackMLBaseObjectType", obj_types)
    if packml_type is None:
        log.warning("PackMLBaseObjectType not found -- skipping PackML instance")
        return
    packml_obj = await device.add_object(
        vendor_sid("tmcpackermachine.PackML"),
        ua.QualifiedName("PackML", PACKML_NS_IDX),
        objecttype=packml_type.nodeid,
        instantiate_optional=False,
    )
    log.info("PackML instance created (subtype of PackMLBaseObjectType)")

    # Walk the instantiated tree and register the PackML status/admin
    # variables in NODE_REGISTRY so the simulation loop can drive them.
    async def register(node: Node, prefix: str) -> None:
        for child in await node.get_children():
            try:
                bn = await child.read_browse_name()
            except Exception:
                continue
            key = f"{prefix}{bn.Name}"
            try:
                ncls = await child.read_node_class()
            except Exception:
                continue
            if ncls == ua.NodeClass.Variable:
                NODE_REGISTRY[f"PackML.{key}"] = child
            await register(child, key + ".")

    await register(packml_obj, "")
    n = sum(1 for k in NODE_REGISTRY if k.startswith("PackML."))
    log.info("PackML node registry populated (%d nodes)", n)


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
    while True:
        SIM.cycles += 1
        executing = SIM.machine_state == MachineState.Execute

        # Production
        if executing:
            packs_this_tick = max(0, int(SIM.speed_ppm / 60.0 * INTERVAL))
            rejects_this_tick = sum(1 for _ in range(packs_this_tick)
                                    if rng.random() < 0.005)
            SIM.good_packs += max(0, packs_this_tick - rejects_this_tick)
            SIM.rejected_packs += rejects_this_tick
            SIM.motor_hours += INTERVAL / 3600.0
            SIM.energy_kwh += 95.0 * INTERVAL / 3600.0  # ~95 kWh/h
            SIM.speed_ppm = pid_step(SIM.speed_ppm, 590.0, k=0.15, noise=2.0)
        else:
            SIM.speed_ppm = max(0.0, SIM.speed_ppm - 30.0 * INTERVAL)

        await write("MachineState", int(SIM.machine_state))
        await write("ControlMode", int(SIM.control_mode))
        await write("GoodPackCount", SIM.good_packs)
        await write("RejectedPackCount", SIM.rejected_packs)
        await write("ProductionSpeed_PPM", round(SIM.speed_ppm, 2))
        await write("MotorRunningHours", round(SIM.motor_hours, 4))
        await write("EnergyConsumption_kWh", round(SIM.energy_kwh, 3))

        # OEE rough rolling
        avail = 92.0 + math.sin(SIM.cycles / 600.0) * 1.5
        perf  = 88.0 + math.sin(SIM.cycles / 400.0) * 2.0
        qual  = 99.4 + math.sin(SIM.cycles / 800.0) * 0.4
        await write("OEE_Availability", round(avail, 2))
        await write("OEE_Performance",  round(perf, 2))
        await write("OEE_Quality",      round(qual, 2))
        await write("OEE_Overall",      round(avail * perf * qual / 10000.0, 2))

        # Hopper
        await write("Hopper.TobaccoLevel_Pct",
                    round(72.0 + 5.0 * math.sin(SIM.cycles / 300.0), 2))
        await write("Hopper.FeedRate_kg_h",
                    round(180.0 + rng.uniform(-3, 3), 2))
        await write("Hopper.HopperWeight_kg",
                    round(640.0 + 40.0 * math.sin(SIM.cycles / 500.0), 2))
        await write("Hopper.FeederMotorCurrent_A",
                    round(8.4 + rng.uniform(-0.3, 0.3), 3))
        await write("Hopper.FeederMotorTemp_C",
                    round(pid_step(42.0, 45.0, k=0.05, noise=0.5), 2))

        # GlueStation
        glue_temp = pid_step(165.0, 165.0, k=0.2, noise=0.6)
        await write("GlueStation.GlueTemperature_C", round(glue_temp, 2))
        await write("GlueStation.GlueLevel_Pct",
                    round(85.0 - (SIM.cycles % 1000) / 200.0, 2))
        await write("GlueStation.NozzlePressure_bar",
                    round(3.4 + rng.uniform(-0.05, 0.05), 3))
        await write("GlueStation.NozzleFlow_g_min",
                    round(120.0 + rng.uniform(-2, 2), 2))
        await write("GlueStation.HeaterPowerOutput_Pct",
                    round(42.0 + rng.uniform(-2, 2), 2))

        # FoilWrapper
        await write("FoilWrapper.FoilTension_N",
                    round(45.0 + rng.uniform(-1, 1), 2))
        await write("FoilWrapper.FoilSpeed_mm_s",
                    round(1850.0 + rng.uniform(-15, 15), 2))
        await write("FoilWrapper.FoilRollDiameter_mm",
                    round(320.0 - (SIM.cycles % 5000) / 100.0, 2))
        await write("FoilWrapper.FoilTrackingError_mm",
                    round(rng.uniform(-0.4, 0.4), 3))

        # Cellophaner
        await write("Cellophaner.FilmFeedSpeed_mm_s",
                    round(1900.0 + rng.uniform(-12, 12), 2))
        await write("Cellophaner.FilmRollDiameter_mm",
                    round(285.0 - (SIM.cycles % 5000) / 110.0, 2))
        await write("Cellophaner.SealTemp_Top_C",
                    round(pid_step(180.0, 180.0, k=0.2, noise=0.5), 2))
        await write("Cellophaner.SealTemp_Side_C",
                    round(pid_step(175.0, 175.0, k=0.2, noise=0.5), 2))
        await write("Cellophaner.SealTemp_Bottom_C",
                    round(pid_step(178.0, 178.0, k=0.2, noise=0.5), 2))
        await write("Cellophaner.SealPressure_bar",
                    round(4.2 + rng.uniform(-0.05, 0.05), 3))
        await write("Cellophaner.TearTapeAlignment_mm",
                    round(rng.uniform(-0.3, 0.3), 3))

        # Cartoner
        cpm = SIM.speed_ppm / 10.0  # 10 packs per carton
        await write("Cartoner.CartonsPerMinute", round(cpm, 2))
        await write("Cartoner.CartonMagazineLevel_Pct",
                    round(78.0 - (SIM.cycles % 2000) / 80.0, 2))
        await write("Cartoner.GlueGunTemp_C",
                    round(pid_step(190.0, 190.0, k=0.2, noise=0.4), 2))

        # Conveyor
        await write("Conveyor.BeltSpeed_mm_s",
                    round(1200.0 + rng.uniform(-8, 8), 2))
        await write("Conveyor.MotorCurrent_A",
                    round(18.5 + rng.uniform(-0.4, 0.4), 3))
        await write("Conveyor.MotorTemp_C",
                    round(pid_step(55.0, 58.0, k=0.05, noise=0.4), 2))
        await write("Conveyor.MotorRPM",
                    round(1450.0 + rng.uniform(-10, 10), 1))
        await write("Conveyor.BeltSlipPct",
                    round(1.2 + rng.uniform(-0.2, 0.2), 3))
        await write("Conveyor.VibrationLevel_mm_s",
                    round(1.8 + rng.uniform(-0.1, 0.1), 3))
        await write("Conveyor.EncoderPosition", SIM.cycles * 1450)

        # PackML status mirror -- written as best-effort because the type tree
        # gives the standard names (CurMachSpeed, MachSpeed, MachineState,
        # AccTimeSinceReset, etc.). Keys in NODE_REGISTRY are
        # "PackML.Status.<Name>" / "PackML.Admin.<Name>".
        await write("PackML.Status.CurMachSpeed", round(SIM.speed_ppm, 2))
        await write("PackML.Status.MachSpeed", round(SIM.speed_ppm, 2))
        await write("PackML.Status.MachineState", int(SIM.machine_state))
        await write("PackML.Status.UnitMode", int(SIM.control_mode))
        await write("PackML.Admin.AccTimeSinceReset",
                    int((time.monotonic() - SIM.started_at) * 1000))

        await asyncio.sleep(INTERVAL)


# ---------------------------------------------------------------------------
# Web UI (small status page)
# ---------------------------------------------------------------------------
app = FastAPI(title="TMC Packer Simulator")
static_dir = Path(__file__).with_name("static")
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    index = static_dir / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return HTMLResponse("<h1>TMC Packer Simulator</h1><p>See /api/status</p>")


@app.get("/api/status")
async def status() -> JSONResponse:
    return JSONResponse({
        "endpoint": f"opc.tcp://0.0.0.0:{PORT}/{ENDPOINT_PATH}",
        "uptime_seconds": round(time.monotonic() - SIM.started_at, 1),
        "cycles": SIM.cycles,
        "device": "tmcpackermachine",
        "submodules": list(SUBMODULES.keys()),
        "variable_count": len(DEVICE_VARS) + sum(len(v) for v in SUBMODULES.values()),
        "machine_state": SIM.machine_state.name,
        "good_packs": SIM.good_packs,
        "rejected_packs": SIM.rejected_packs,
        "speed_ppm": round(SIM.speed_ppm, 2),
    })


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
async def main() -> None:
    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://0.0.0.0:{PORT}/{ENDPOINT_PATH}")
    server.set_server_name("AIO Demo TMC Packer Simulator")

    # The AIO OPC UA connector always selects the strongest endpoint advertised
    # by the server (regardless of the user's `securityMode: "none"` setting in
    # the device endpoint config). If we only expose NoSecurity, it rejects the
    # connection with `BadSecurityModeRejected`. Match the maker simulator and
    # advertise None + Sign + SignAndEncrypt using a self-signed cert.
    cert_dir = Path("/tmp/opcua-packer-certs")
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_file = cert_dir / "server-cert.der"
    key_file = cert_dir / "server-key.pem"
    if not cert_file.exists():
        from asyncua.crypto.cert_gen import setup_self_signed_certificate
        from cryptography.x509.oid import ExtendedKeyUsageOID
        await setup_self_signed_certificate(
            key_file, cert_file,
            "urn:aio-demo:tmc:packer",
            host_name="opcua-packer-service",
            cert_use=[ExtendedKeyUsageOID.SERVER_AUTH],
            subject_attrs={
                "commonName": "AIO Demo TMC Packer Simulator",
                "organizationName": "AIODemo",
                "countryName": "CH",
            },
        )
        log.info("Generated self-signed OPC UA certificate for packer")
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
        log.info("OPC UA Packer server listening on opc.tcp://0.0.0.0:%d/%s",
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
