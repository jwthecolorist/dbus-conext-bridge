#!/usr/bin/env python3
"""
dbus-conext-bridge v3.0 — Pure DBUS service for Conext XW Pro on Venus OS.

Reads pre-cached Modbus data from /tmp/conext_cache.json (written by conext-poller).
Does ZERO Modbus I/O — just reads a local JSON file (~1ms) and publishes to DBUS.
This keeps the GLib main loop completely free for DBUS event processing.

Architecture:
  conext-poller (separate daemontools service) -> /tmp/conext_cache.json -> this script -> DBUS

v3.0 changes:
  - Removed all subprocess/blocking calls (was causing GLib starvation)
  - Delta-based DBUS updates (_set method) to avoid signal flooding
  - Guarded formatters against None (prevents introspection crashes)
  - SIGTERM handler for graceful DBUS shutdown
  - Throttled logging (every 10th cycle instead of every cycle)
  - /Connected toggle based on cache freshness
"""
import sys, os, json, logging, time, configparser, signal
import dbus
from gi.repository import GLib
from dbus.mainloop.glib import DBusGMainLoop

sys.path.insert(1, "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")
from vedbus import VeDbusService
from settingsdevice import SettingsDevice


# --- Load config from config.ini ---
CFG = configparser.ConfigParser()
CFG_PATH = "/data/dbus-conext-bridge/config.ini"
if os.path.exists(CFG_PATH):
    CFG.read(CFG_PATH)

_unit_ids = CFG.get("inverters", "unit_ids", fallback="11,12")
UNIT_IDS = [int(x.strip()) for x in _unit_ids.split(",")]
NUM_UNITS = CFG.getint("inverters", "count", fallback=len(UNIT_IDS))

CONEXT_IP = CFG.get("modbus", "ip", fallback="192.168.1.223")
CONEXT_PORT = CFG.getint("modbus", "port", fallback=503)

PRODUCT_ID = 2623
FIRMWARE_VERSION = 1170500
PRODUCT_NAME = CFG.get("dbus", "product_name",
                        fallback="Conext XW Pro 6848 x%d (Bridge)" % NUM_UNITS)
CUSTOM_NAME = CFG.get("dbus", "custom_name", fallback="Conext XW Pro")
DEVICE_INSTANCE = CFG.getint("dbus", "device_instance", fallback=275)
CONNECTION = "Modbus TCP %s:%d" % (CONEXT_IP, CONEXT_PORT)

CACHE_PATH = "/tmp/conext_cache.json"
WRITE_CMD_PATH = "/tmp/conext_write_cmd.json"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("conext-bridge")

# --- Formatting callbacks (guarded against None to prevent introspection crashes) ---
def _a(p,v):  return "%.2fA"%v if v is not None else "---"
def _w(p,v):  return "%iW"%v if v is not None else "---"
def _va(p,v): return "%iVA"%v if v is not None else "---"
def _v(p,v):  return "%.1fV"%v if v is not None else "---"
def _hz(p,v): return "%.2fHz"%v if v is not None else "---"
def _c(p,v):  return "%i C"%v if v is not None else "---"
def _pct(p,v):return "%.1f%%"%v if v is not None else "---"


def _safe_add(*values):
    """Sum values, treating None as 0. Returns None if ALL values are None."""
    result = 0
    all_none = True
    for v in values:
        if v is not None:
            result += v
            all_none = False
    return None if all_none else result

def _safe_avg(*values):
    """Average values, ignoring None. Returns None if all None."""
    valid = [v for v in values if v is not None]
    if not valid:
        return None
    return sum(valid) / len(valid)

def _safe_first(*values):
    """Return first non-None value."""
    for v in values:
        if v is not None:
            return v
    return None


class ConextBridge:
    def __init__(self):
        self.svc = None
        self._mainloop = None
        self._cache_ts = 0
        self._stale_count = 0
        self._update_count = 0
        self._last_values = {}  # Delta tracking: only update DBUS on value change

    def _set(self, path, value):
        """Update a DBUS path only if the value has changed. Avoids flooding
        the GLib event loop with DBUS signal emissions on every cycle."""
        if self._last_values.get(path) != value:
            self.svc[path] = value
            self._last_values[path] = value

    # --- Write callbacks ---
    def _on_mode_change(self, path, value):
        modes = {1: 'Charger Only', 2: 'Inverter Only', 3: 'On', 4: 'Off'}
        if value not in modes:
            return False
        log.warning("CONTROL Mode -> %s (%s) — accepted on DBUS", value, modes[value])
        return True

    def _on_current_limit_change(self, path, value):
        """Write AC input current limit back to Conext via command file."""
        try:
            value = float(value)
        except (TypeError, ValueError):
            log.warning("CONTROL %s: invalid value %s", path, value)
            return False
        if value < 0 or value > 100:
            log.warning("CONTROL %s: value %.1f out of range (0-100A)", path, value)
            return False
        # Determine which register to write
        if "/In/1/" in path:
            reg_name = "AC1BreakerSize"
        elif "/In/2/" in path:
            reg_name = "AC2BreakerSize"
        else:
            return False
        # Write command file for poller to process
        cmd = {"register": reg_name, "value": value, "unit_ids": UNIT_IDS}
        try:
            with open(WRITE_CMD_PATH, "w") as f:
                json.dump(cmd, f)
            log.warning("CONTROL %s -> %.1fA (write cmd sent to poller)", path, value)
        except Exception as e:
            log.error("CONTROL write cmd failed: %s", e)
        return True

    def _on_control_change(self, path, value):
        log.info("CONTROL %s -> %s (Venus-internal)", path, value)
        return True

    def _handle_setting_changed(self, setting, oldvalue, newvalue):
        if setting == 'RestartRequested' and newvalue == 1:
            log.info("Settings restart requested by GUI. Updating config.ini...")
            config = configparser.ConfigParser()
            if os.path.exists(CFG_PATH): config.read(CFG_PATH)
            
            if not config.has_section("modbus"): config.add_section("modbus")
            if not config.has_section("inverters"): config.add_section("inverters")
            
            config.set("modbus", "ip", str(self.settings['GatewayIp']))
            config.set("modbus", "port", str(self.settings['GatewayPort']))
            config.set("inverters", "unit_ids", str(self.settings['UnitIds']))
            config.set("inverters", "count", str(self.settings['UnitCount']))
            config.set("inverters", "poll_interval_ms", str(self.settings['PollInterval']))
            
            try:
                with open(CFG_PATH, "w") as f:
                    config.write(f)
            except Exception as e:
                log.error("Failed to save config.ini: %s", e)
                
            self.settings['RestartRequested'] = 0
            
            log.warning("Restarting Conext services to apply new settings...")
            os.system("svc -t /service/conext-poller")
            os.system("svc -t /service/dbus-conext-bridge")

        elif setting == 'ScanRequested' and newvalue == 1:
            log.info("Auto-Scan requested by GUI. Running conext-scanner.py...")
            # Run scanner script asynchronously to avoid blocking the DBus GLib thread
            import subprocess
            subprocess.Popen(["python3", "/data/dbus-conext-bridge/conext-scanner.py"])
            
            # The python script writes directly to DBus, which will trigger our other setting callbacks.
            # But the scanner takes ~10 seconds. We just reset the GUI flag.
            self.settings['ScanRequested'] = 0

    def _read_cache(self):
        """Read /tmp/conext_cache.json. Returns (units_dict, timestamp) or (None, 0)."""
        try:
            with open(CACHE_PATH, "r") as f:
                data = json.load(f)
            return data.get("units", {}), data.get("ts", 0)
        except (FileNotFoundError, json.JSONDecodeError):
            return None, 0
        except Exception as e:
            log.warning("Cache read error: %s", e)
            return None, 0

    def _update(self):
        """GLib timer: read JSON cache, publish to DBUS. ~1ms, zero blocking."""
        if self.svc is None:
            return True  # setup() failed, nothing to do
        try:
            units, ts = self._read_cache()
            if units is None:
                self._stale_count += 1
                if self._stale_count % 30 == 1:
                    log.warning("Cache not available (stale count: %d)", self._stale_count)
                self._set("/Connected", 0)
                return True

            # Check freshness (stale if >30s old)
            age = time.time() - ts
            if age > 30:
                self._stale_count += 1
                if self._stale_count % 30 == 1:
                    log.warning("Cache stale: %.0fs old (stale count: %d)", age, self._stale_count)
                self._set("/Connected", 0)
                return True

            self._stale_count = 0
            self._cache_ts = ts
            self._set("/Connected", 1)

            # Collect data from ALL inverter units
            all_units = [units.get(str(uid), {}) for uid in UNIT_IDS]

            # === AC Input Current Limits (read from first unit — same for all) ===
            u0 = all_units[0] if all_units else {}
            ac1_limit = u0.get("AC1BreakerSize")
            ac2_limit = u0.get("AC2BreakerSize")
            if ac1_limit is not None:
                self._set("/Ac/In/1/CurrentLimit", round(ac1_limit, 1))
            if ac2_limit is not None:
                self._set("/Ac/In/2/CurrentLimit", round(ac2_limit, 1))

            # === DC: average voltage, sum current/power across all units ===
            dc_voltages = [u.get("DCVoltage") for u in all_units]
            dc_voltage = _safe_avg(*dc_voltages)
            dc_current = _safe_add(*[u.get("DCCurrent") for u in all_units])
            dc_power = _safe_add(*[u.get("DCPower") for u in all_units])

            self._set("/Dc/0/Voltage", round(dc_voltage, 2) if dc_voltage is not None else None)
            self._set("/Dc/0/Current", round(dc_current, 2) if dc_current is not None else None)
            self._set("/Dc/0/Power", round(dc_power) if dc_power is not None else None)
            self._set("/Dc/0/Temperature", None)

            # === AC Input 1 (Grid/Shore) ===
            ac1_freq = _safe_first(*[u.get("AC1Frequency") for u in all_units])
            # Validate frequency (must be 45-65Hz to be "connected")
            ac1_connected = 1 if ac1_freq and 45 < ac1_freq < 65 else 0

            # === AC Input 2 (Generator) ===
            ac2_freq = _safe_first(*[u.get("AC2Frequency") for u in all_units])
            ac2_connected = 1 if ac2_freq and 45 < ac2_freq < 65 else 0

            if ac1_connected:
                active_input, ac_connected = 0, 1
            elif ac2_connected:
                active_input, ac_connected = 1, 1
            else:
                active_input, ac_connected = 240, 0  # 240 = disconnected
            self._set("/Ac/ActiveIn/ActiveInput", active_input)
            self._set("/Ac/ActiveIn/Connected", ac_connected)
            self._set("/Ac/NumberOfAcInputs", 2)
            self._set("/Ac/State/AcIn1Available", ac1_connected)
            self._set("/Ac/State/AcIn2Available", ac2_connected)

            ac_in_total = 0  # Initialize before if/elif/else to prevent NameError
            if active_input == 0 and ac1_connected:
                # Grid/Shore connected
                self._set("/Ac/ActiveIn/L1/F", ac1_freq)
                ac_in_l1_v = _safe_first(*[u.get("AC1L1Voltage") for u in all_units])
                ac_in_l2_v = _safe_first(*[u.get("AC1L2Voltage") for u in all_units])
                ac_in_l1_i = _safe_add(*[u.get("AC1L1Current") for u in all_units])
                self._set("/Ac/ActiveIn/L1/V", round(ac_in_l1_v, 1) if ac_in_l1_v else None)
                self._set("/Ac/ActiveIn/L1/I", round(ac_in_l1_i, 2) if ac_in_l1_i else None)
                self._set("/Ac/ActiveIn/L2/F", ac1_freq)
                self._set("/Ac/ActiveIn/L2/V", round(ac_in_l2_v, 1) if ac_in_l2_v else None)
                self._set("/Ac/ActiveIn/L2/I", None)  # L2 current not in register map
                ac_in_total = _safe_add(*[u.get("AC1Power") for u in all_units])
                # Approximate L1/L2 power from total (even split for split-phase)
                if ac_in_total is not None:
                    self._set("/Ac/ActiveIn/L1/P", round(ac_in_total / 2))
                    self._set("/Ac/ActiveIn/L2/P", round(ac_in_total / 2))
                else:
                    self._set("/Ac/ActiveIn/L1/P", None)
                    self._set("/Ac/ActiveIn/L2/P", None)
            elif active_input == 1 and ac2_connected:
                # Generator connected
                self._set("/Ac/ActiveIn/L1/F", ac2_freq)
                ac_in_l1_v = _safe_first(*[u.get("AC2L1Voltage") for u in all_units])
                ac_in_l1_i = _safe_add(*[u.get("AC2L1Current") for u in all_units])
                self._set("/Ac/ActiveIn/L1/V", round(ac_in_l1_v, 1) if ac_in_l1_v else None)
                self._set("/Ac/ActiveIn/L1/I", round(ac_in_l1_i, 2) if ac_in_l1_i else None)
                self._set("/Ac/ActiveIn/L2/F", ac2_freq)
                self._set("/Ac/ActiveIn/L2/V", None)
                self._set("/Ac/ActiveIn/L2/I", None)
                ac_in_total = _safe_add(*[u.get("AC2Power") for u in all_units])
                self._set("/Ac/ActiveIn/L1/P", round(ac_in_total / 2) if ac_in_total else None)
                self._set("/Ac/ActiveIn/L2/P", round(ac_in_total / 2) if ac_in_total else None)
            else:
                # No AC input
                for leg in ["L1", "L2"]:
                    self._set("/Ac/ActiveIn/%s/F" % leg, None)
                    self._set("/Ac/ActiveIn/%s/V" % leg, None)
                    self._set("/Ac/ActiveIn/%s/I" % leg, None)
                    self._set("/Ac/ActiveIn/%s/P" % leg, None)
                ac_in_total = 0
            self._set("/Ac/ActiveIn/P", round(ac_in_total) if ac_in_total else 0)

            # === AC Output (Load) — sum across inverters, per-leg ===
            load_freq = _safe_first(*[u.get("ACLoadFrequency") for u in all_units])

            # L1 Load: sum current across all inverters, use first available voltage
            load_l1_v = _safe_first(*[u.get("ACLoadL1Voltage") for u in all_units])
            load_l1_i = _safe_add(*[u.get("ACLoadL1Current") for u in all_units])

            # L2 Load: same
            load_l2_v = _safe_first(*[u.get("ACLoadL2Voltage") for u in all_units])
            load_l2_i = _safe_add(*[u.get("ACLoadL2Current") for u in all_units])

            self._set("/Ac/Out/L1/F", load_freq)
            self._set("/Ac/Out/L1/V", round(load_l1_v, 1) if load_l1_v is not None else None)
            self._set("/Ac/Out/L1/I", round(load_l1_i, 2) if load_l1_i is not None else None)

            self._set("/Ac/Out/L2/F", load_freq)
            self._set("/Ac/Out/L2/V", round(load_l2_v, 1) if load_l2_v is not None else None)
            self._set("/Ac/Out/L2/I", round(load_l2_i, 2) if load_l2_i is not None else None)

            # Total load power = sum from all inverters
            load_total = _safe_add(*[u.get("ACLoadPower") for u in all_units])
            self._set("/Ac/Out/P", round(load_total) if load_total is not None else None)

            # Per-leg power: proportional split based on current
            total_i = (load_l1_i or 0) + (load_l2_i or 0)
            if load_total is not None and total_i > 0:
                self._set("/Ac/Out/L1/P", round(load_total * (load_l1_i or 0) / total_i))
                self._set("/Ac/Out/L2/P", round(load_total * (load_l2_i or 0) / total_i))
            else:
                self._set("/Ac/Out/L1/P", round(load_total / 2) if load_total else 0)
                self._set("/Ac/Out/L2/P", round(load_total / 2) if load_total else 0)

            # === State ===
            # Use first unit's state (all units should be in the same state)
            u0 = all_units[0] if all_units else {}
            ds = u0.get("DeviceState")
            ie = u0.get("InverterEnabled")
            ce = u0.get("ChargerEnabled")
            if ds is None or ds < 2:
                venus_state = 0
            elif ds == 2:
                venus_state = 3  # Bulk charging
            elif ds == 3:
                venus_state = 8 if ac_connected else 9
            else:
                venus_state = 0
            self._set("/State", venus_state)

            mode = 3
            if ie == 0 and ce == 0: mode = 4
            elif ie == 0 and ce == 1: mode = 1
            elif ie == 1 and ce == 0: mode = 2
            self._set("/Mode", mode)
            self._set("/VebusChargeState", 0)

            self._set("/Alarms/GridLost", 0)

            self._update_count += 1

            # Log summary every 10th cycle (~30s) to avoid log spam
            if self._update_count % 10 == 1:
                log.info("DC:%.1fV %.1fA %dW | Ld:L1=%.0fV/%.1fA L2=%.0fV/%.1fA %dW | st:%d (age:%.1fs) [#%d]",
                         dc_voltage or 0, dc_current or 0, dc_power or 0,
                         load_l1_v or 0, load_l1_i or 0, load_l2_v or 0, load_l2_i or 0,
                         load_total or 0, venus_state, age, self._update_count)

        except Exception as e:
            log.warning("DBUS update error: %s", e, exc_info=True)
        return True


    def setup(self):
        s = VeDbusService("com.victronenergy.vebus.conext_0",
                          bus=dbus.SystemBus(), register=False)
        self.svc = s

        # Initialize Settings Device (syncs from DBus to config.ini)
        self.settings = SettingsDevice(dbus.SystemBus(), supportedSettings={
            'GatewayIp': ['/Settings/ConextBridge/GatewayIp', '192.168.1.223', 0, 0],
            'GatewayPort': ['/Settings/ConextBridge/GatewayPort', 503, 0, 65535],
            'UnitIds': ['/Settings/ConextBridge/UnitIds', '11,12', 0, 0],
            'UnitCount': ['/Settings/ConextBridge/UnitCount', 2, 0, 4],
            'PollInterval': ['/Settings/ConextBridge/PollInterval', 3000, 1000, 30000],
            'RestartRequested': ['/Settings/ConextBridge/RestartRequested', 0, 0, 1],
            'ScanRequested': ['/Settings/ConextBridge/ScanRequested', 0, 0, 1]
        }, eventCallback=self._handle_setting_changed)

        s.add_path("/Mgmt/ProcessName", __file__)
        s.add_path("/Mgmt/ProcessVersion", "2.7.0")
        s.add_path("/Mgmt/Connection", CONNECTION)
        s.add_path("/DeviceInstance", DEVICE_INSTANCE)
        s.add_path("/ProductId", PRODUCT_ID)
        s.add_path("/ProductName", PRODUCT_NAME)
        s.add_path("/CustomName", CUSTOM_NAME)
        s.add_path("/FirmwareVersion", FIRMWARE_VERSION)
        s.add_path("/Serial", "CONEXT-BRIDGE-001")
        s.add_path("/Connected", 1)
        s.add_path("/Ac/ActiveIn/ActiveInput", 0, writeable=True)
        s.add_path("/Ac/ActiveIn/Connected", 1)
        s.add_path("/Ac/ActiveIn/CurrentLimitIsAdjustable", 1)
        for ph in ["L1", "L2"]:
            s.add_path("/Ac/ActiveIn/%s/F" % ph, None, gettextcallback=_hz)
            s.add_path("/Ac/ActiveIn/%s/I" % ph, None, gettextcallback=_a)
            s.add_path("/Ac/ActiveIn/%s/P" % ph, None, gettextcallback=_w)
            s.add_path("/Ac/ActiveIn/%s/S" % ph, None, gettextcallback=_va)
            s.add_path("/Ac/ActiveIn/%s/V" % ph, None, gettextcallback=_v)
        s.add_path("/Ac/ActiveIn/P", 0, gettextcallback=_w)
        s.add_path("/Ac/ActiveIn/S", 0, gettextcallback=_va)
        s.add_path("/Ac/Control/IgnoreAcIn1", 0, writeable=True,
                   onchangecallback=self._on_control_change)
        s.add_path("/Ac/Control/RemoteGeneratorSelected", 0, writeable=True,
                   onchangecallback=self._on_control_change)
        s.add_path("/Ac/In/1/CurrentLimit", 50.0, gettextcallback=_a, writeable=True,
                   onchangecallback=self._on_current_limit_change)
        s.add_path("/Ac/In/1/CurrentLimitIsAdjustable", 1)
        s.add_path("/Ac/In/2/CurrentLimit", 50.0, gettextcallback=_a, writeable=True,
                   onchangecallback=self._on_current_limit_change)
        s.add_path("/Ac/In/2/CurrentLimitIsAdjustable", 1)
        s.add_path("/Ac/NumberOfAcInputs", 2)
        s.add_path("/Ac/NumberOfPhases", 2)
        for ph in ["L1", "L2"]:
            s.add_path("/Ac/Out/%s/F" % ph, None, gettextcallback=_hz)
            s.add_path("/Ac/Out/%s/I" % ph, None, gettextcallback=_a)
            s.add_path("/Ac/Out/%s/NominalInverterPower" % ph, 6800, gettextcallback=_w)
            s.add_path("/Ac/Out/%s/P" % ph, None, gettextcallback=_w)
            s.add_path("/Ac/Out/%s/S" % ph, None, gettextcallback=_va)
            s.add_path("/Ac/Out/%s/V" % ph, None, gettextcallback=_v)
        s.add_path("/Ac/Out/NominalInverterPower", 13600, gettextcallback=_w)
        s.add_path("/Ac/Out/P", None, gettextcallback=_w)
        s.add_path("/Ac/Out/S", None, gettextcallback=_va)
        s.add_path("/Ac/PowerMeasurementType", 4)
        s.add_path("/Ac/State/AcIn1Available", 0)
        s.add_path("/Ac/State/AcIn2Available", 0)
        for a in ["GridLost", "HighDcCurrent", "HighDcVoltage", "HighTemperature",
                   "LowBattery", "Overload", "PhaseRotation", "Ripple",
                   "TemperatureSensor", "VoltageSensor"]:
            s.add_path("/Alarms/%s" % a, 0)
        for ph in ["L1", "L2"]:
            for a in ["HighTemperature", "LowBattery", "Overload", "Ripple"]:
                s.add_path("/Alarms/%s/%s" % (ph, a), 0)
        s.add_path("/Bms/AllowToCharge", 1)
        s.add_path("/Bms/AllowToDischarge", 1)
        s.add_path("/Bms/BmsExpected", 0)
        s.add_path("/Bms/BmsType", 0)
        s.add_path("/Bms/Error", 0)
        s.add_path("/Dc/0/Current", None, gettextcallback=_a)
        s.add_path("/Dc/0/MaxChargeCurrent", None, gettextcallback=_a)
        s.add_path("/Dc/0/Power", None, gettextcallback=_w)
        s.add_path("/Dc/0/Temperature", None, gettextcallback=_c)
        s.add_path("/Dc/0/Voltage", None, gettextcallback=_v)
        s.add_path("/Devices/NumberOfMultis", NUM_UNITS)
        for ep in ["AcIn1ToAcOut", "AcIn1ToInverter", "AcIn2ToAcOut",
                    "AcIn2ToInverter", "AcOutToAcIn1", "AcOutToAcIn2",
                    "InverterToAcIn1", "InverterToAcIn2", "InverterToAcOut",
                    "OutToInverter"]:
            s.add_path("/Energy/%s" % ep, None)
        s.add_path("/FirmwareFeatures/BolFrame", 1)
        s.add_path("/FirmwareFeatures/BolUBatAndTBatSense", 1)
        s.add_path("/FirmwareFeatures/CommandWriteViaId", 1)
        s.add_path("/FirmwareFeatures/IBatSOCBroadcast", 1)
        s.add_path("/FirmwareFeatures/NewPanelFrame", 1)
        s.add_path("/FirmwareFeatures/SetChargeState", 1)
        s.add_path("/FirmwareSubVersion", 0)
        s.add_path("/Hub4/AssistantId", 5)
        s.add_path("/Hub4/DisableCharge", 0, writeable=True,
                   onchangecallback=self._on_control_change)
        s.add_path("/Hub4/DisableFeedIn", 0, writeable=True,
                   onchangecallback=self._on_control_change)
        s.add_path("/Hub4/DoNotFeedInOvervoltage", 1, writeable=True,
                   onchangecallback=self._on_control_change)
        s.add_path("/Hub4/FixSolarOffsetTo100mV", 1)
        for ph in ["L1", "L2"]:
            s.add_path("/Hub4/%s/AcPowerSetpoint" % ph, 0, writeable=True,
                       onchangecallback=self._on_control_change)
            s.add_path("/Hub4/%s/MaxFeedInPower" % ph, 32766, writeable=True,
                       onchangecallback=self._on_control_change)
        s.add_path("/Hub4/Sustain", 0)
        s.add_path("/Hub4/TargetPowerIsMaxFeedIn", 0)
        s.add_path("/Mode", 3, writeable=True,
                   onchangecallback=self._on_mode_change)
        s.add_path("/ModeIsAdjustable", 1)
        s.add_path("/State", 0)
        s.add_path("/VebusChargeState", 0)
        s.add_path("/VebusError", 0)
        s.add_path("/Soc", None, gettextcallback=_pct)
        s.register()
        log.info("DBUS service registered (v2.7 — sentinel filtering, proper L1/L2)")

    def _on_sigterm(self, signum, frame):
        """Graceful shutdown: unregister DBUS name before exit."""
        log.info("SIGTERM received — shutting down gracefully")
        if self._mainloop:
            self._mainloop.quit()

    def run(self):
        DBusGMainLoop(set_as_default=True)
        signal.signal(signal.SIGTERM, self._on_sigterm)
        self.setup()
        GLib.timeout_add(3000, self._update)
        log.info("Bridge v3.0: DBUS service started (poller is separate daemontools service)")
        self._mainloop = GLib.MainLoop()
        self._mainloop.run()
        log.info("Bridge v3.0: shutdown complete")

if __name__ == "__main__":
    ConextBridge().run()
