#!/usr/bin/env python3
"""
dbus-conext-bridge v2.3 — Pure DBUS service for Conext XW Pro on Venus OS.

Reads pre-cached Modbus data from /tmp/conext_cache.json (written by conext-poller).
Does ZERO Modbus I/O — just reads a local JSON file (~1ms) and publishes to DBUS.
This keeps the GLib main loop completely free for DBUS event processing.

Architecture:
  conext-poller (separate process) -> /tmp/conext_cache.json -> this script -> DBUS
"""
import sys, os, json, logging, time, configparser, subprocess
import dbus
from gi.repository import GLib
from dbus.mainloop.glib import DBusGMainLoop

sys.path.insert(1, "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")
from vedbus import VeDbusService


# --- Load config from config.ini ---
# The bridge does NOT read DBUS settings directly (to avoid subprocess calls
# that crash on Venus). The poller reads DBUS settings and communicates
# via /tmp/conext_cache.json. The bridge reads unit IDs from cache keys.
CFG = configparser.ConfigParser()
CFG_PATH = "/data/dbus-conext-bridge/config.ini"
if os.path.exists(CFG_PATH):
    CFG.read(CFG_PATH)

_unit_ids = CFG.get("inverters", "unit_ids", fallback="11,12")
UNIT_IDS = [int(x.strip()) for x in _unit_ids.split(",")]
UNIT_L1 = UNIT_IDS[0]
UNIT_L2 = UNIT_IDS[1] if len(UNIT_IDS) > 1 else UNIT_IDS[0]
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

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("conext-bridge")

# --- Formatting callbacks ---
def _a(p,v):  return "%.2fA"%v
def _w(p,v):  return "%iW"%v
def _va(p,v): return "%iVA"%v
def _v(p,v):  return "%.1fV"%v
def _hz(p,v): return "%.2fHz"%v
def _c(p,v):  return "%i C"%v
def _pct(p,v):return "%.1f%%"%v


class ConextBridge:
    def __init__(self):
        self.svc = None
        self._poller_proc = None
        self._cache_ts = 0
        self._stale_count = 0
        self._update_count = 0
        # Settings fingerprint at startup — used to detect GUI changes
        self._settings_fp = "%s:%d:%s" % (CONEXT_IP, CONEXT_PORT, ",".join(str(u) for u in UNIT_IDS))

    # --- Write callbacks (accepted on DBUS, no Modbus write from this process) ---
    def _on_mode_change(self, path, value):
        modes = {1: 'Charger Only', 2: 'Inverter Only', 3: 'On', 4: 'Off'}
        if value not in modes:
            return False
        log.warning("CONTROL Mode -> %s (%s) — accepted on DBUS", value, modes[value])
        return True

    def _on_current_limit_change(self, path, value):
        per_unit = value / NUM_UNITS
        log.warning("CONTROL %s -> %.1fA total (%.1fA per unit x%d)",
                    path, value, per_unit, NUM_UNITS)
        return True

    def _on_control_change(self, path, value):
        log.info("CONTROL %s -> %s (Venus-internal)", path, value)
        return True

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
        try:
            units, ts = self._read_cache()
            if units is None:
                self._stale_count += 1
                if self._stale_count % 30 == 1:
                    log.warning("Cache not available (stale count: %d)", self._stale_count)
                return True

            # Check freshness (stale if >30s old)
            age = time.time() - ts
            if age > 30:
                self._stale_count += 1
                if self._stale_count % 30 == 1:
                    log.warning("Cache stale: %.0fs old (stale count: %d)", age, self._stale_count)
                    # Try to restart poller
                    self._ensure_poller()
                return True

            self._stale_count = 0
            self._cache_ts = ts

            l1 = units.get(str(UNIT_L1), {})
            l2 = units.get(str(UNIT_L2), {})
            s = self.svc

            # DC: average voltage, sum current/power
            dcv1 = l1.get("DCVoltage") or 0
            dcv2 = l2.get("DCVoltage") or 0
            dc_voltage = (dcv1 + dcv2) / 2 if (dcv1 and dcv2) else (dcv1 or dcv2)
            dc_current = (l1.get("DCCurrent") or 0) + (l2.get("DCCurrent") or 0)
            dc_power = (l1.get("DCPower") or 0) + (l2.get("DCPower") or 0)

            s["/Dc/0/Voltage"] = round(dc_voltage, 2)
            s["/Dc/0/Current"] = round(dc_current, 2)
            s["/Dc/0/Power"] = round(dc_power)
            s["/Dc/0/Temperature"] = None

            # === AC Input 1 (Grid/Shore) ===
            ac1_f1 = l1.get("AC1Frequency")
            ac1_f2 = l2.get("AC1Frequency")
            ac1_freq = ac1_f1 if ac1_f1 and ac1_f1 > 0 else ac1_f2
            if ac1_freq and ac1_freq > 100: ac1_freq = None
            ac1_connected = 1 if ac1_freq and ac1_freq > 45 else 0

            # === AC Input 2 (Generator) ===
            ac2_f1 = l1.get("AC2Frequency")
            ac2_f2 = l2.get("AC2Frequency")
            ac2_freq = ac2_f1 if ac2_f1 and ac2_f1 > 0 else ac2_f2
            if ac2_freq and ac2_freq > 100: ac2_freq = None
            ac2_connected = 1 if ac2_freq and ac2_freq > 45 else 0

            if ac1_connected:
                active_input, ac_connected = 0, 1
            elif ac2_connected:
                active_input, ac_connected = 1, 1
            else:
                active_input, ac_connected = 0, 0

            s["/Ac/ActiveIn/ActiveInput"] = active_input
            s["/Ac/ActiveIn/Connected"] = ac_connected
            s["/Ac/State/AcIn1Available"] = ac1_connected
            s["/Ac/State/AcIn2Available"] = ac2_connected

            if active_input == 0:
                s["/Ac/ActiveIn/L1/F"] = ac1_freq
                s["/Ac/ActiveIn/L1/V"] = l2.get("AC1L1Voltage") or l1.get("AC1L1Voltage")
                ac_in_l1_i = (l1.get("AC1L1Current") or 0) + (l2.get("AC1L1Current") or 0)
                s["/Ac/ActiveIn/L1/I"] = round(ac_in_l1_i, 2)
                s["/Ac/ActiveIn/L1/P"] = round(ac_in_l1_i * (l2.get("AC1L1Voltage") or 120))
                s["/Ac/ActiveIn/L2/F"] = ac1_freq
                s["/Ac/ActiveIn/L2/V"] = l2.get("AC1L2Voltage") or l1.get("AC1L2Voltage")
                s["/Ac/ActiveIn/L2/I"] = None
                s["/Ac/ActiveIn/L2/P"] = None
                ac_in_total = (l1.get("AC1Power") or 0) + (l2.get("AC1Power") or 0)
            else:
                s["/Ac/ActiveIn/L1/F"] = ac2_freq
                s["/Ac/ActiveIn/L1/V"] = l2.get("AC2L1Voltage") or l1.get("AC2L1Voltage")
                ac_in_l1_i = (l1.get("AC2L1Current") or 0) + (l2.get("AC2L1Current") or 0)
                s["/Ac/ActiveIn/L1/I"] = round(ac_in_l1_i, 2)
                s["/Ac/ActiveIn/L1/P"] = round(ac_in_l1_i * (l2.get("AC2L1Voltage") or 120))
                s["/Ac/ActiveIn/L2/F"] = ac2_freq
                s["/Ac/ActiveIn/L2/V"] = None
                s["/Ac/ActiveIn/L2/I"] = None
                s["/Ac/ActiveIn/L2/P"] = None
                ac_in_total = (l1.get("AC2Power") or 0) + (l2.get("AC2Power") or 0)
            s["/Ac/ActiveIn/P"] = round(ac_in_total)

            # === AC Output (Load) ===
            lf1 = l1.get("ACLoadFrequency")
            lf2 = l2.get("ACLoadFrequency")
            load_freq = lf1 if lf1 and lf1 > 0 else lf2

            s["/Ac/Out/L1/F"] = load_freq
            s["/Ac/Out/L1/V"] = l2.get("ACLoadL1Voltage") or l1.get("ACLoadL1Voltage")
            total_l1_i = (l1.get("ACLoadL1Current") or 0) + (l2.get("ACLoadL1Current") or 0)
            s["/Ac/Out/L1/I"] = round(total_l1_i, 2)

            s["/Ac/Out/L2/F"] = load_freq
            s["/Ac/Out/L2/V"] = l2.get("ACLoadL2Voltage") or l1.get("ACLoadL2Voltage")
            total_l2_i = (l1.get("ACLoadL2Current") or 0) + (l2.get("ACLoadL2Current") or 0)
            s["/Ac/Out/L2/I"] = round(total_l2_i, 2)

            load_total = (l1.get("ACLoadPower") or 0) + (l2.get("ACLoadPower") or 0)
            s["/Ac/Out/P"] = round(load_total)

            total_i = total_l1_i + total_l2_i
            if total_i > 0:
                s["/Ac/Out/L1/P"] = round(load_total * total_l1_i / total_i)
                s["/Ac/Out/L2/P"] = round(load_total * total_l2_i / total_i)
            else:
                s["/Ac/Out/L1/P"] = 0
                s["/Ac/Out/L2/P"] = 0

            # === State ===
            ds = l1.get("DeviceState")
            ie = l1.get("InverterEnabled")
            ce = l1.get("ChargerEnabled")
            if ds is None or ds < 2:
                venus_state = 0
            elif ds == 2:
                venus_state = 3  # Bulk charging
            elif ds == 3:
                venus_state = 8 if ac_connected else 9
            else:
                venus_state = 0
            s["/State"] = venus_state

            mode = 3
            if ie == 0 and ce == 0: mode = 4
            elif ie == 0 and ce == 1: mode = 1
            elif ie == 1 and ce == 0: mode = 2
            s["/Mode"] = mode
            s["/VebusChargeState"] = 0

            s["/Alarms/GridLost"] = 0

            log.info("L1[%d]:%.1fV %dW ld:%dW | L2[%d]:%.1fV %dW ld:%dW | DC:%dW Ld:%dW (age:%.1fs)",
                     UNIT_L1, dcv1, l1.get("DCPower",0) or 0, l1.get("ACLoadPower",0) or 0,
                     UNIT_L2, dcv2, l2.get("DCPower",0) or 0, l2.get("ACLoadPower",0) or 0,
                     dc_power, load_total, age)

            # Check for settings changes every ~30s (10 cycles * 3s)
            self._update_count += 1
            if self._update_count % 10 == 0:
                self._check_settings_change(units)

        except Exception as e:
            log.warning("DBUS update error: %s", e)
        return True

    def _check_settings_change(self, cache_data):
        """Check if GX UI settings changed. If so, restart service to apply."""
        try:
            with open(CACHE_PATH, "r") as f:
                data = json.load(f)
            settings = data.get("settings", {})
            if not settings:
                return
            new_fp = "%s:%s:%s" % (
                settings.get("ip", ""),
                settings.get("port", ""),
                settings.get("unit_ids", ""))
            if new_fp != self._settings_fp and new_fp != "::":
                log.warning("Settings changed! '%s' -> '%s'. Restarting service...",
                            self._settings_fp, new_fp)
                os.system("svc -t /service/dbus-conext-bridge &")
        except Exception:
            pass  # Non-critical — don't crash on settings check

    def _ensure_poller(self):
        """Start or restart the poller subprocess if it's not running."""
        if self._poller_proc and self._poller_proc.poll() is None:
            return  # still running
        poller_path = os.path.join(os.path.dirname(__file__), "conext-poller.py")
        if not os.path.exists(poller_path):
            poller_path = "/data/dbus-conext-bridge/conext-poller.py"
        try:
            self._poller_proc = subprocess.Popen(
                [sys.executable, poller_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            log.info("Started poller subprocess (pid %d)", self._poller_proc.pid)
        except Exception as e:
            log.error("Failed to start poller: %s", e)

    def setup(self):
        s = VeDbusService("com.victronenergy.vebus.conext_0",
                          bus=dbus.SystemBus(), register=False)
        self.svc = s
        s.add_path("/Mgmt/ProcessName", __file__)
        s.add_path("/Mgmt/ProcessVersion", "2.3.0")
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
        s.add_path("/Devices/NumberOfMultis", 2)
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
        log.info("DBUS service registered (v2.3 — separate process, JSON cache)")

    def run(self):
        DBusGMainLoop(set_as_default=True)
        self.setup()
        self._ensure_poller()
        # Read cache every 1s, publish to DBUS. ~1ms per call, zero blocking.
        GLib.timeout_add(1000, self._update)
        log.info("Bridge v2.3: DBUS-only process, poller runs separately")
        GLib.MainLoop().run()

if __name__ == "__main__":
    ConextBridge().run()
