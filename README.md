# OPC UA SurfaceTechnology Simulator

An OPC UA simulator for an industrial powder coating line, built on the
[SurfaceTechnology/GeneralTypes](https://reference.opcfoundation.org/SurfaceTechnology/GeneralTypes/v100/docs/)
companion specification (OPC 40563).

The simulator loads the official OPC Foundation companion nodesets and
instantiates a complete coating system with four components, a line
controller, and 43 process variables driven by a PID-style simulation
loop. It is designed as a discovery and telemetry target for OPC UA
clients such as
[Azure IoT Operations](https://learn.microsoft.com/en-us/azure/iot-operations/discover-manage-assets/howto-detect-opc-ua-assets).

---

## Table of Contents

- [Architecture](#architecture)
- [Address Space](#address-space)
- [Prerequisites](#prerequisites)
- [Running Locally](#running-locally)
- [Running with Docker](#running-with-docker)
- [Deploying to Kubernetes](#deploying-to-kubernetes)
- [Configuration](#configuration)
- [Running Tests](#running-tests)
- [Project Structure](#project-structure)
- [Companion Nodesets](#companion-nodesets)

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    OPC UA Server (:4840)                  │
│  ┌────────────────────────────────────────────────────┐  │
│  │                  CoatingSystem                     │  │
│  │  (vendor subtype of STSysType)                     │  │
│  │                                                    │  │
│  │  ├── Identification (nameplate)                    │  │
│  │  ├── Monitoring (system-level variables)           │  │
│  │  └── Components                                    │  │
│  │       ├── PretreatmentStation (STCompType)         │  │
│  │       │    └── Monitoring/{Process,Consumption,    │  │
│  │       │                     Health}                │  │
│  │       ├── PowderBooth (STCompType)                 │  │
│  │       │    └── Monitoring/{Process,Consumption,    │  │
│  │       │                     Health}                │  │
│  │       ├── CuringOven (STCompType)                  │  │
│  │       │    └── Monitoring/{Process,Consumption,    │  │
│  │       │                     Health}                │  │
│  │       └── ConveyorSystem (STCompType)              │  │
│  │            └── Monitoring/{Process,Consumption,    │  │
│  │                             Health}                │  │
│  ├── LineController (STBaseControllerType)            │  │
│  └── (DI / IA / Machinery / ISA95 / STGeneralTypes   │  │
│       type hierarchy from imported nodesets)          │  │
│  └────────────────────────────────────────────────────┘  │
├──────────────────────────────────────────────────────────┤
│                    Web Dashboard (:8080)                  │
│  GET /           → status page                           │
│  GET /api/status → JSON telemetry snapshot                │
└──────────────────────────────────────────────────────────┘
```

## Address Space

### System-Level Variables (under `CoatingSystem/Monitoring`)

| Variable             | Type   | Unit | Range    | Description                |
|----------------------|--------|------|----------|----------------------------|
| SystemStatus         | Int32  | —    | 0–4      | 0=Idle 1=Running 2=Paused 3=Error 4=Maintenance |
| PartsProduced        | UInt64 | pcs  | 0–10¹²   | Cumulative good parts      |
| PartsRejected        | UInt64 | pcs  | 0–10¹²   | Cumulative rejects         |
| LineSpeed_parts_h    | Double | 1/h  | 0–500    | Current throughput         |
| OEE_Overall          | Double | %    | 0–100    | Overall equipment effectiveness |

### Component Variables

Each component exposes variables under `Monitoring/Process`, `Monitoring/Consumption`, and `Monitoring/Health`:

**PretreatmentStation** — chemical pretreatment of parts before coating
- Process: TankTemperature_C, PhLevel, ChemicalConcentration_Pct, SprayPressure_bar, RinseWaterFlow_L_min
- Consumption: ChemicalUsage_L_h, WaterUsage_L_h
- Health: TankLevelLow, FilterClogged

**PowderBooth** — electrostatic powder application
- Process: PowderFlow_g_min, GunVoltage_kV, GunCurrent_uA, BoothAirflow_m3_h, FilmThickness_um, RecoveryEfficiency_Pct
- Consumption: PowderUsage_kg_h, CompressedAir_m3_h
- Health: GunClogAlarm, FilterDiffPressureHigh

**CuringOven** — heat curing of powder coat
- Process: OvenTemperature_C, OvenTempSetpoint_C, HeatingPower_Pct, ZoneTemp_Entry_C, ZoneTemp_Center_C, ZoneTemp_Exit_C, ExhaustTemp_C
- Consumption: GasConsumption_m3_h, ElectricalPower_kW
- Health: OverTempAlarm, BurnerFault

**ConveyorSystem** — overhead conveyor moving parts through the line
- Process: ChainSpeed_m_min, MotorCurrent_A, MotorTemp_C, ChainTension_N, PartCount
- Consumption: EnergyConsumption_kWh
- Health: ChainOverloadAlarm, LubricationLow

---

## Prerequisites

- **Python 3.12+**
- **pip** (or any Python package manager)
- **Docker** (optional, for containerized deployment)
- **kubectl** (optional, for Kubernetes deployment)

---

## Running Locally

1. **Clone the repository:**

   ```bash
   git clone https://github.com/mc5eamus/opcua-surfacetech-simulator.git
   cd opcua-surfacetech-simulator
   ```

2. **Install dependencies:**

   ```bash
   pip install -r simulator/requirements.txt
   ```

3. **Start the simulator:**

   ```bash
   python simulator/server.py
   ```

4. **Connect:**
   - OPC UA endpoint: `opc.tcp://localhost:4840/surfacetech-demo`
   - Web dashboard: `http://localhost:8080`
   - Status API: `http://localhost:8080/api/status`

The server generates a self-signed certificate on first start (stored in
`/tmp/opcua-surfacetech-certs/`) and advertises NoSecurity, Sign, and
SignAndEncrypt security policies with Anonymous authentication.

---

## Running with Docker

1. **Build the image:**

   ```bash
   docker build -t surfacetech-sim:latest simulator/
   ```

2. **Run the container:**

   ```bash
   docker run -d \
     --name surfacetech-sim \
     -p 4840:4840 \
     -p 8080:8080 \
     surfacetech-sim:latest
   ```

3. **Verify it's running:**

   ```bash
   curl http://localhost:8080/api/status
   ```

4. **Stop the container:**

   ```bash
   docker stop surfacetech-sim && docker rm surfacetech-sim
   ```

---

## Deploying to Kubernetes

A ready-made manifest is provided for Kubernetes (targeting the
`azure-iot-operations` namespace):

```bash
kubectl apply -f simulator/k8s-deployment.yaml
```

This creates:
- A `surfacetech-simulator` Deployment (1 replica)
- A `surfacetech-simulator-service` ClusterIP Service exposing ports 4840 (OPC UA) and 8080 (web)

The OPC UA endpoint inside the cluster is:
```
opc.tcp://surfacetech-simulator-service:4840/surfacetech-demo
```

> **Note:** The manifest uses `imagePullPolicy: Never`, so the Docker
> image must be pre-loaded into the cluster (e.g. via `docker build` on
> a local Kind/Minikube node).

---

## Configuration

The simulator is configured via environment variables:

| Variable             | Default            | Description                          |
|----------------------|--------------------|--------------------------------------|
| `OPCUA_PORT`         | `4840`             | OPC UA server listening port         |
| `WEB_PORT`           | `8080`             | Web dashboard / API port             |
| `PUBLISH_INTERVAL_MS`| `1000`             | Simulation tick interval in ms       |
| `ENDPOINT_PATH`      | `surfacetech-demo` | URL path of the OPC UA endpoint      |

Example:

```bash
OPCUA_PORT=4841 WEB_PORT=9090 PUBLISH_INTERVAL_MS=500 python simulator/server.py
```

---

## Running Tests

The test suite acts as an OPC UA client — it starts a test server
instance, connects to it, and performs endpoint discovery, address space
traversal, and telemetry reads.

1. **Install test dependencies** (in addition to simulator deps):

   ```bash
   pip install -r simulator/requirements.txt
   pip install pytest pytest-asyncio
   ```

2. **Run the tests:**

   ```bash
   python -m pytest tests/test_discovery.py -v
   ```

   Expected output (14 tests):

   ```
   tests/test_discovery.py::TestEndpointDiscovery::test_get_endpoints PASSED
   tests/test_discovery.py::TestEndpointDiscovery::test_server_namespaces PASSED
   tests/test_discovery.py::TestAddressSpaceStructure::test_coating_system_exists PASSED
   tests/test_discovery.py::TestAddressSpaceStructure::test_coating_system_has_identification PASSED
   tests/test_discovery.py::TestAddressSpaceStructure::test_components_folder_exists PASSED
   tests/test_discovery.py::TestAddressSpaceStructure::test_component_monitoring_structure PASSED
   tests/test_discovery.py::TestAddressSpaceStructure::test_line_controller_exists PASSED
   tests/test_discovery.py::TestTelemetryCollection::test_read_process_variables PASSED
   tests/test_discovery.py::TestTelemetryCollection::test_read_oven_temperatures PASSED
   tests/test_discovery.py::TestTelemetryCollection::test_read_system_monitoring PASSED
   tests/test_discovery.py::TestTelemetryCollection::test_read_health_alarms PASSED
   tests/test_discovery.py::TestTreeTraversal::test_full_tree_traversal PASSED
   tests/test_discovery.py::TestTreeTraversal::test_variable_count PASSED
   tests/test_discovery.py::TestTreeTraversal::test_engineering_units_present PASSED

   ======================== 14 passed in ~9s =========================
   ```

### What the Tests Cover

| Test Class                | Tests | What It Verifies                                              |
|---------------------------|-------|---------------------------------------------------------------|
| `TestEndpointDiscovery`   | 2     | Endpoint URLs contain "surfacetech"; DI, ST, Machinery namespaces registered |
| `TestAddressSpaceStructure` | 5   | CoatingSystem node exists; Identification has Manufacturer/SerialNumber; Components folder has all 4 components; Monitoring/Process/Consumption/Health structure; LineController present |
| `TestTelemetryCollection` | 4     | Read PowderBooth process variables and verify numeric ranges; read CuringOven temperatures; read system-level monitoring; read health alarm booleans |
| `TestTreeTraversal`       | 3     | Full recursive tree walk finds 30+ nodes; 43+ Variable nodes exist; EngineeringUnits properties are attached to numeric variables |

---

## Project Structure

```
opcua-surfacetech-simulator/
├── simulator/
│   ├── server.py               # OPC UA server + simulation loop + web API
│   ├── requirements.txt        # Python dependencies
│   ├── Dockerfile              # Container image definition
│   ├── k8s-deployment.yaml     # Kubernetes Deployment + Service
│   ├── static/
│   │   └── index.html          # Web dashboard
│   └── nodesets/                # OPC Foundation companion spec XMLs
│       ├── Opc.Ua.Di.NodeSet2.xml
│       ├── Opc.Ua.IA.NodeSet2.xml
│       ├── Opc.Ua.Machinery.NodeSet2.xml
│       ├── Opc.Ua.ISA95-JOBCONTROL.NodeSet2.xml
│       ├── Opc.Ua.Machinery.Jobs.NodeSet2.xml
│       └── Opc.Ua.STGeneralTypes.NodeSet2.xml
├── tests/
│   └── test_discovery.py       # OPC UA client discovery & traversal tests
├── reference/
│   └── opcua-packer/           # Reference implementation (TMC packer)
├── pyproject.toml              # pytest configuration
└── .gitignore
```

---

## Companion Nodesets

The simulator loads 6 companion specification nodesets in dependency
order. These are sourced from the
[OPCFoundation/UA-Nodeset](https://github.com/OPCFoundation/UA-Nodeset)
repository:

| Nodeset                             | Namespace URI                                             | Spec    |
|-------------------------------------|-----------------------------------------------------------|---------|
| Opc.Ua.Di.NodeSet2.xml              | `http://opcfoundation.org/UA/DI/`                         | OPC 10000-100 |
| Opc.Ua.IA.NodeSet2.xml              | `http://opcfoundation.org/UA/IA/`                         | OPC 40001 |
| Opc.Ua.Machinery.NodeSet2.xml       | `http://opcfoundation.org/UA/Machinery/`                  | OPC 40001-1 |
| Opc.Ua.ISA95-JOBCONTROL.NodeSet2.xml| `http://opcfoundation.org/UA/ISA95-JOBCONTROL_V2/`        | OPC 30140 |
| Opc.Ua.Machinery.Jobs.NodeSet2.xml  | `http://opcfoundation.org/UA/Machinery/Jobs/`             | OPC 40001-3 |
| Opc.Ua.STGeneralTypes.NodeSet2.xml  | `http://opcfoundation.org/UA/SurfaceTechnology/GeneralTypes/` | OPC 40563 |

> **Compatibility note:** The STGeneralTypes nodeset requires OPC UA
> base namespace ≥ 1.05.06, but the `asyncua` library ships with
> 1.05.04. The bundled nodeset has this requirement relaxed to 1.05.04.
> Additionally, `strict_mode=False` is used during XML import because
> some newer nodesets reference parent nodes not present in `asyncua`'s
> built-in address space.
