# Solar Power Control

**A Home Assistant integration that automatically turns on your appliances when your solar panels produce surplus power.**

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/pebitec/power-control)
[![HA Version](https://img.shields.io/badge/HA-2025.8%2B-blue)](https://www.home-assistant.io)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

## What it does

Solar Power Control reads your inverter's power sensors every 30 seconds and decides which appliances to run based on how much surplus solar energy is currently available. When your panels produce more than your home consumes, the integration turns on appliances in priority order. When the surplus drops, it turns them off again.

No batteries, no tariff schedules, no 24-hour planners — just straightforward solar surplus control.

## Features

- **Priority-based control** — assign each appliance a priority (1–1000); lower number = higher priority. High-priority appliances get power first.
- **Preemption** — optionally shed lower-priority appliances to start a higher-priority one when there is not quite enough surplus for both.
- **Switch interval** — configurable cooldown between state changes to protect appliances from rapid cycling.
- **Averaging window** — smooths out short-term fluctuations before making decisions, configurable per appliance.
- **Appliance dependencies** — require one appliance to be running before another starts (e.g. pool pump before heat pump).
- **Helper appliances** — appliances that only run when another appliance needs them.
- **Per-appliance enable/disable switches** — disable individual appliances from the HA dashboard without removing them.
- **Manual override switches** — force an appliance on regardless of surplus level.
- **Analytics sensors** — runtime today, energy today, self-consumption ratio, estimated savings.

## Requirements

- Home Assistant 2025.8 or newer
- A solar inverter with at least one power sensor visible in Home Assistant:
  - PV production power sensor, **and**
  - grid export sensor **or** combined import/export sensor **or** load power sensor
- [HACS](https://hacs.xyz/) for installation

## Installation

### HACS (recommended)

1. Open HACS in your Home Assistant sidebar
2. Click the three-dot menu → **Custom repositories**
3. Add `https://github.com/pebitec/power-control` as an **Integration**
4. Search for **Solar Power Control** and click **Download**
5. Restart Home Assistant
6. Go to **Settings → Devices & Services → Add Integration** and search for **Solar Power Control**

### Manual

1. Copy the `custom_components/solar_power_control` folder into your `config/custom_components/` directory
2. Restart Home Assistant
3. Go to **Settings → Devices & Services → Add Integration** and search for **Solar Power Control**

## Quick start

### 1. Add the integration

Go to **Settings → Devices & Services → Add Integration → Solar Power Control**.

**Step 1 — Sensor mapping**

| Field | Description |
|---|---|
| PV Production Power Sensor | Your inverter's current output in W or kW |
| Grid Export Power Sensor | Power currently exported to the grid (positive = export) |
| Combined Import/Export Sensor | Single sensor: positive = export, negative = import |
| Load Power Sensor | Total household consumption in W or kW |
| Invert Combined Import/Export Sensor | Enable if your meter reports positive = import, negative = export |

You need to provide the PV sensor and at least one of the grid/load sensors. The integration supports both W and kW sensors and converts automatically.

> **Tip:** If your meter reports buying power as a positive number and solar export as a negative number, enable **Invert Combined Import/Export Sensor**.

**Step 2 — Settings**

| Field | Default | Description |
|---|---|---|
| Shed Threshold | −50 W | How far negative the surplus can go before appliances start turning off |
| Controller Interval | 30 s | How often sensors are read |
| Enable Preemption | On | Whether lower-priority appliances can be shed to start higher-priority ones |

### 2. Add appliances

After the integration is set up, click **Configure** and then **Add Appliance**. Repeat for each appliance.

**Step 1 — Basic info**

| Field | Description |
|---|---|
| Name | Friendly name shown in HA |
| Entity | The switch, input_boolean, light, climate or fan entity to control |
| Priority | 1–1000, lower = higher priority |
| Nominal Power | Rated consumption in watts |
| Actual Power Sensor | Optional — for accurate analytics |

**Step 2 — Constraints**

| Field | Default | Description |
|---|---|---|
| Switch Interval | 300 s | Minimum seconds between on/off changes |
| Averaging Window | — | Seconds of history to average (leave empty for global default) |
| On Only | Off | Never turn off automatically |
| Protect From Preemption | Off | Cannot be shed for a higher-priority appliance |
| Activation Buffer | 200 W | Extra surplus required above nominal power to trigger activation |
| Completion Power Threshold | — | Below this wattage, the appliance counts as done (e.g. dishwasher on standby) |
| Requires Appliance | — | Must be running before this appliance can start |
| Helper Only | Off | Only starts when another appliance needs it |

## Entities created

For each appliance the integration creates:

| Entity | Type | Description |
|---|---|---|
| `sensor.*_power` | Sensor | Current power draw |
| `sensor.*_runtime_today` | Sensor | Hours run today |
| `sensor.*_energy_today` | Sensor | kWh consumed today |
| `sensor.*_activations_today` | Sensor | Times turned on today |
| `sensor.*_status` | Sensor | Current decision and reason |
| `switch.*_enabled` | Switch | Enable/disable this appliance |
| `switch.*_override` | Switch | Force on regardless of surplus |
| `number.*_priority` | Number | Adjustable priority |
| `binary_sensor.*_active` | Binary sensor | Is the appliance currently on? |

The integration also creates global entities:

| Entity | Description |
|---|---|
| `sensor.*_excess_power` | Current calculated surplus in W |
| `binary_sensor.*_excess_available` | True when surplus exceeds 50 W (averaged) |
| `switch.*_control_enabled` | Master enable/disable switch |

## How the control logic works

Every cycle the optimizer runs three phases:

1. **Assess** — calculate the average excess power from recent history. If fewer than 3 samples are available (startup), only safety rules apply.
2. **Allocate** — iterate through appliances in priority order. Turn on each one if the average surplus covers its consumption plus the activation buffer.
3. **Shed** — if the instantaneous surplus falls below the shed threshold (default −50 W), turn off the lowest-priority appliances until the balance is restored.

Between phases 2 and 3, **preemption** can shed a lower-priority ON appliance to free up enough power to start a higher-priority IDLE one.

## Excess power calculation

The integration calculates surplus as follows:

- **Combined import/export sensor** (positive = export): surplus = sensor value
- **Inverted combined sensor** (positive = import): surplus = −sensor value
- **Separate grid export sensor**: surplus = export value (positive means sending to grid)
- **PV + load sensors**: surplus = PV production − load

## License

Licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)** — see the [LICENSE](LICENSE) file for details.
