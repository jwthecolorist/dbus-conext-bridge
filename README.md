# dbus-conext-bridge

Venus OS driver that bridges Schneider Conext XW Pro inverters to the Victron DBUS, making them appear as a Quattro-like device.

## Features

- **Dual inverter polling** — Reads from 2 Conext XW Pro units (split-phase) via Modbus TCP Port 503
- **Quattro emulation** — Dual AC inputs (Grid + Generator), 2-phase AC output, DC battery data
- **Real-time data** — DC voltage/current/power, AC load per phase, frequency, state/mode
- **Inverter controls** — Mode changes (On/Off/Charger/Inverter) written to Conext via Modbus
- **AC input configuration** — Both AC inputs configurable as Grid/Generator/Shore in Venus Settings
- **ESS compatible** — Hub4 paths for ESS systemcalc integration

## Quick Install

```bash
# Copy files to Venus device
scp -r dbus-conext-bridge/ root@<venus-ip>:/tmp/

# SSH into Venus and run installer
ssh root@<venus-ip>
sh /tmp/dbus-conext-bridge/install.sh
```

## Configuration

Edit `/data/dbus-conext-bridge/config.ini`:

```ini
[modbus]
ip = 192.168.1.223
port = 503

[inverters]
count = 2
unit_ids = 11,12
poll_interval_ms = 3000
```

Restart after config change:
```bash
svc -t /service/dbus-conext-bridge
```

## DBUS Paths

| Path | Type | Description |
|---|---|---|
| `/Dc/0/Voltage` | read | Battery voltage |
| `/Dc/0/Power` | read | DC power (neg = charging) |
| `/Ac/Out/L1/P` | read | AC output L1 power |
| `/Ac/Out/L2/P` | read | AC output L2 power |
| `/Ac/ActiveIn/Connected` | read | AC input connected |
| `/Ac/ActiveIn/ActiveInput` | read | Active input (0=AC1, 1=AC2) |
| `/State` | read | Venus state (0=Off, 3=Bulk, 8=Passthru, 9=Inverting) |
| `/Mode` | r/w | Mode (1=Charger, 2=Inverter, 3=On, 4=Off) |
| `/Ac/In/1/CurrentLimit` | r/w | AC1 current limit |
| `/Ac/In/2/CurrentLimit` | r/w | AC2 current limit |

## Register Map

Uses Conext proprietary Port 503 registers:

| Register | Name | Scale | Notes |
|---|---|---|---|
| 64 | DeviceState | 1 | 0=Standby, 1=Search, 2=Charging, 3=Operating |
| 71 | InverterEnabled | 1 | 0/1 (writable) |
| 72 | ChargerEnabled | 1 | 0/1 (writable) |
| 80-81 | DCVoltage | 0.001 | uint32 volts |
| 82-83 | DCCurrent | 0.001 | sint32 amps |
| 84-85 | DCPower | 1 | sint32 watts |
| 97 | AC1Frequency | 0.01 | Hz |
| 102-103 | AC1Power | 1 | sint32 watts |
| 125 | AC2Frequency | 0.01 | Hz (0xFFFF = not present) |
| 130-131 | AC2Power | 1 | sint32 watts |
| 152 | ACLoadFrequency | 0.01 | Hz |
| 154-155 | ACLoadPower | 1 | sint32 watts |

## Uninstall

```bash
sh /data/dbus-conext-bridge/uninstall.sh
```

## License

MIT
