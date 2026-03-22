#!/usr/bin/env python3
"""
dbus-conext-bridge v1.4 - Conext XW Pro to Venus OS DBUS bridge.
Uses standard GLib.timeout_add pattern (same as all Venus OS drivers).
Reads config from /data/dbus-conext-bridge/config.ini if present.
"""
import sys, os, socket, struct, logging, time, configparser
import dbus
from gi.repository import GLib
from dbus.mainloop.glib import DBusGMainLoop

sys.path.insert(1, "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")
from vedbus import VeDbusService

# --- Load config ---
CFG = configparser.ConfigParser()
CFG_PATH = "/data/dbus-conext-bridge/config.ini"
if os.path.exists(CFG_PATH):
    CFG.read(CFG_PATH)

CONEXT_IP = CFG.get("modbus", "ip", fallback="192.168.1.223")
CONEXT_PORT = CFG.getint("modbus", "port", fallback=503)
MODBUS_TIMEOUT = CFG.getint("modbus", "timeout", fallback=2)

_unit_ids = CFG.get("inverters", "unit_ids", fallback="11,12")
UNIT_IDS = [int(x.strip()) for x in _unit_ids.split(",")]
UNIT_L1 = UNIT_IDS[0]
UNIT_L2 = UNIT_IDS[1] if len(UNIT_IDS) > 1 else UNIT_IDS[0]
NUM_UNITS = CFG.getint("inverters", "count", fallback=len(UNIT_IDS))
POLL_INTERVAL_MS = CFG.getint("inverters", "poll_interval_ms", fallback=3000)
INTER_READ_DELAY = CFG.getint("inverters", "inter_read_delay_ms", fallback=50) / 1000.0

PRODUCT_ID = 2623
FIRMWARE_VERSION = 1170500
PRODUCT_NAME = CFG.get("dbus", "product_name",
                        fallback="Conext XW Pro 6848 x%d (Bridge)" % NUM_UNITS)
CUSTOM_NAME = CFG.get("dbus", "custom_name", fallback="Conext XW Pro")
DEVICE_INSTANCE = CFG.getint("dbus", "device_instance", fallback=275)
CONNECTION = "Modbus TCP %s:%d" % (CONEXT_IP, CONEXT_PORT)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("conext-bridge")

REGS = {
    "DeviceState":     (64,1,">H",1),
    "InverterEnabled": (71,1,">H",1),
    "ChargerEnabled":  (72,1,">H",1),
    "DCVoltage":       (80,2,">I",0.001),
    "DCCurrent":       (82,2,">i",0.001),
    "DCPower":         (84,2,">i",1),
    # AC Input 1 (Grid/Shore)
    "AC1Frequency":    (97,1,">H",0.01),
    "AC1Power":        (102,2,">i",1),
    "AC1L1Voltage":    (110,2,">I",0.001),
    "AC1L1Current":    (116,2,">i",0.001),
    # AC Input 2 (Generator) — mirrors AC1 layout with +28 offset
    "AC2Frequency":    (125,1,">H",0.01),
    "AC2Power":        (130,2,">i",1),
    "AC2L1Voltage":    (138,2,">I",0.001),
    "AC2L1Current":    (140,2,">i",0.001),
    # AC Output (Load)
    "ACLoadFrequency": (152,1,">H",0.01),
    "ACLoadPower":     (154,2,">i",1),
    "ACLoadL1Voltage": (142,2,">I",0.001),
    "ACLoadL1Current": (146,2,">i",0.001),
}

POLL_KEYS = [
    "DeviceState","InverterEnabled","ChargerEnabled",
    "DCVoltage","DCCurrent","DCPower",
    "AC1Frequency","AC1Power","AC1L1Voltage","AC1L1Current",
    "AC2Frequency","AC2Power","AC2L1Voltage","AC2L1Current",
    "ACLoadFrequency","ACLoadPower","ACLoadL1Voltage","ACLoadL1Current",
]

class ModbusTCP:
    def __init__(self, ip, port, timeout=2):
        self.ip, self.port, self.timeout = ip, port, timeout
        self.sock, self.tid = None, 0
    def connect(self):
        try:
            if self.sock:
                try: self.sock.close()
                except: pass
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect((self.ip, self.port))
            log.info("Connected to %s:%d", self.ip, self.port)
            return True
        except Exception as e:
            log.error("Connect failed: %s", e)
            self.sock = None
            return False
    def close(self):
        if self.sock:
            try: self.sock.close()
            except: pass
            self.sock = None
    def read(self, uid, reg, cnt):
        if not self.sock: raise ConnectionError("Not connected")
        self.tid = (self.tid + 1) & 0xFFFF
        self.sock.sendall(struct.pack(">HHHBBHH", self.tid, 0, 6, uid, 3, reg, cnt))
        h = self._rx(9)
        if h[7] & 0x80:
            self._rx(1)
            raise Exception("Modbus err fc=%d" % (h[7] & 0x7F))
        return self._rx(h[8])
    def write_register(self, uid, reg, value):
        """Write a single holding register (FC 0x06)."""
        if not self.sock: raise ConnectionError("Not connected")
        self.tid = (self.tid + 1) & 0xFFFF
        self.sock.sendall(struct.pack(">HHHBBHH", self.tid, 0, 6, uid, 6, reg, value))
        resp = self._rx(12)  # Echo response: 7-byte MBAP + 1 uid + 1 fc + 2 reg + 2 val
        if resp[7] & 0x80:
            raise Exception("Modbus write err fc=%d" % (resp[7] & 0x7F))
        log.info("WRITE uid=%d reg=%d val=%d OK", uid, reg, value)
        return True
    def _rx(self, n):
        b = b""
        while len(b) < n:
            c = self.sock.recv(n - len(b))
            if not c: raise ConnectionError("Socket closed")
            b += c
        return b

def _a(p,v):  return "%.2fA"%v
def _w(p,v):  return "%iW"%v
def _va(p,v): return "%iVA"%v
def _v(p,v):  return "%.1fV"%v
def _hz(p,v): return "%.2fHz"%v
def _c(p,v):  return "%i C"%v
def _pct(p,v):return "%.1f%%"%v

class ConextBridge:
    def __init__(self):
        self.client = ModbusTCP(CONEXT_IP, CONEXT_PORT, MODBUS_TIMEOUT)
        self.svc = None
        self.connected = False
        self.errors = 0

    # --- Write callbacks ---
    def _on_mode_change(self, path, value):
        """Venus requests mode change. Writes to InverterEnabled (r71) and ChargerEnabled (r72)."""
        mode_map = {
            1: (0, 1),  # Charger Only
            2: (1, 0),  # Inverter Only
            3: (1, 1),  # On (inv + chg)
            4: (0, 0),  # Off
        }
        modes = {1: 'Charger Only', 2: 'Inverter Only', 3: 'On', 4: 'Off'}
        if value not in mode_map:
            log.warning("CONTROL Mode: invalid value %s", value)
            return False
        inv_en, chg_en = mode_map[value]
        log.warning("CONTROL Mode -> %s (%s): InvEn=%d ChgEn=%d",
                    value, modes[value], inv_en, chg_en)
        try:
            if self.connected:
                for uid in [UNIT_L1, UNIT_L2]:
                    self.client.write_register(uid, 71, inv_en)  # InverterEnabledStatus
                    time.sleep(INTER_READ_DELAY)
                    self.client.write_register(uid, 72, chg_en)  # ChargerEnabledStatus
                    time.sleep(INTER_READ_DELAY)
                log.warning("CONTROL Mode written to both units")
        except Exception as e:
            log.error("CONTROL Mode write failed: %s", e)
        return True  # accept the value on DBUS regardless

    def _on_current_limit_change(self, path, value):
        """Venus requests AC input current limit change.
        Accepted on DBUS. No direct Conext register equivalent yet."""
        log.warning("CONTROL %s -> %.1fA (accepted, no Conext write)", path, value)
        return True

    def _on_control_change(self, path, value):
        """Hub4/ESS control write. These are Venus-internal (systemcalc reads them).
        Accepted on DBUS. No Modbus write needed."""
        log.info("CONTROL %s -> %s (Venus-internal)", path, value)
        return True

    def read_reg(self, uid, name):
        reg, cnt, fmt, scale = REGS[name]
        raw = self.client.read(uid, reg, cnt)
        return struct.unpack(fmt, raw[:struct.calcsize(fmt)])[0] * scale

    def poll_unit(self, uid):
        d = {}
        for name in POLL_KEYS:
            try:
                d[name] = self.read_reg(uid, name)
            except Exception:
                d[name] = None
            time.sleep(INTER_READ_DELAY)
        return d

    def _update(self):
        """GLib timer callback - polls Modbus and updates DBUS."""
        try:
            if not self.connected:
                if self.client.connect():
                    self.connected = True
                    self.errors = 0
                else:
                    return True  # retry next cycle

            l1 = self.poll_unit(UNIT_L1)
            l2 = self.poll_unit(UNIT_L2)
            self.errors = 0
            s = self.svc

            # DC: average voltage, sum current/power
            dcv1 = l1.get("DCVoltage") or 0
            dcv2 = l2.get("DCVoltage") or 0
            dc_voltage = (dcv1 + dcv2) / 2 if (dcv1 and dcv2) else (dcv1 or dcv2)
            dc_current = (l1.get("DCCurrent") or 0) + (l2.get("DCCurrent") or 0)
            dc_power = (l1.get("DCPower") or 0) + (l2.get("DCPower") or 0)
            # Temperature: Conext returns sentinel values (0xFF00, 0x00FF, timeouts).
            # Don't use it — the Discover battery provides accurate temp on DBUS.

            s["/Dc/0/Voltage"] = round(dc_voltage, 2)
            s["/Dc/0/Current"] = round(dc_current, 2)
            s["/Dc/0/Power"] = round(dc_power)
            s["/Dc/0/Temperature"] = None  # Conext returns sentinels; battery provides this

            # === AC Input 1 (Grid/Shore): L1 from unit 11, L2 from unit 12 ===
            ac1_f1 = l1.get("AC1Frequency")
            ac1_f2 = l2.get("AC1Frequency")
            ac1_freq = ac1_f1 if ac1_f1 and ac1_f1 > 0 else ac1_f2
            # Sentinel: 0xFFFF (655.35) or 0 means not present
            if ac1_freq and ac1_freq > 100: ac1_freq = None
            ac1_connected = 1 if ac1_freq and ac1_freq > 45 else 0

            # === AC Input 2 (Generator): L1 from unit 11, L2 from unit 12 ===
            ac2_f1 = l1.get("AC2Frequency")
            ac2_f2 = l2.get("AC2Frequency")
            ac2_freq = ac2_f1 if ac2_f1 and ac2_f1 > 0 else ac2_f2
            if ac2_freq and ac2_freq > 100: ac2_freq = None
            ac2_connected = 1 if ac2_freq and ac2_freq > 45 else 0

            # Determine active input (Quattro-style: 0=AC1, 1=AC2)
            if ac1_connected:
                active_input = 0
                ac_connected = 1
            elif ac2_connected:
                active_input = 1
                ac_connected = 1
            else:
                active_input = 0  # default to AC1
                ac_connected = 0

            s["/Ac/ActiveIn/ActiveInput"] = active_input
            s["/Ac/ActiveIn/Connected"] = ac_connected
            s["/Ac/State/AcIn1Available"] = ac1_connected
            s["/Ac/State/AcIn2Available"] = ac2_connected

            # Populate ActiveIn with whichever input is active
            if active_input == 0:
                # AC1 active
                s["/Ac/ActiveIn/L1/F"] = ac1_f1 if ac1_f1 and 0 < ac1_f1 < 100 else None
                s["/Ac/ActiveIn/L1/V"] = l1.get("AC1L1Voltage")
                s["/Ac/ActiveIn/L1/I"] = l1.get("AC1L1Current")
                s["/Ac/ActiveIn/L1/P"] = l1.get("AC1Power")
                s["/Ac/ActiveIn/L2/F"] = ac1_f2 if ac1_f2 and 0 < ac1_f2 < 100 else None
                s["/Ac/ActiveIn/L2/V"] = l2.get("AC1L1Voltage")
                s["/Ac/ActiveIn/L2/I"] = l2.get("AC1L1Current")
                s["/Ac/ActiveIn/L2/P"] = l2.get("AC1Power")
                ac_in_total = (l1.get("AC1Power") or 0) + (l2.get("AC1Power") or 0)
            else:
                # AC2 active
                s["/Ac/ActiveIn/L1/F"] = ac2_f1 if ac2_f1 and 0 < ac2_f1 < 100 else None
                s["/Ac/ActiveIn/L1/V"] = l1.get("AC2L1Voltage")
                s["/Ac/ActiveIn/L1/I"] = l1.get("AC2L1Current")
                s["/Ac/ActiveIn/L1/P"] = l1.get("AC2Power")
                s["/Ac/ActiveIn/L2/F"] = ac2_f2 if ac2_f2 and 0 < ac2_f2 < 100 else None
                s["/Ac/ActiveIn/L2/V"] = l2.get("AC2L1Voltage")
                s["/Ac/ActiveIn/L2/I"] = l2.get("AC2L1Current")
                s["/Ac/ActiveIn/L2/P"] = l2.get("AC2Power")
                ac_in_total = (l1.get("AC2Power") or 0) + (l2.get("AC2Power") or 0)
            s["/Ac/ActiveIn/P"] = round(ac_in_total)

            # AC Output (Load): L1 from unit 11, L2 from unit 12
            lf1 = l1.get("ACLoadFrequency")
            lf2 = l2.get("ACLoadFrequency")
            load_freq = lf1 if lf1 and lf1 > 0 else lf2
            s["/Ac/Out/L1/F"] = lf1 if lf1 and lf1 > 0 else load_freq
            s["/Ac/Out/L1/V"] = l1.get("ACLoadL1Voltage")
            s["/Ac/Out/L1/I"] = l1.get("ACLoadL1Current")
            s["/Ac/Out/L1/P"] = l1.get("ACLoadPower")
            s["/Ac/Out/L2/F"] = lf2 if lf2 and lf2 > 0 else load_freq
            s["/Ac/Out/L2/V"] = l2.get("ACLoadL1Voltage")
            s["/Ac/Out/L2/I"] = l2.get("ACLoadL1Current")
            s["/Ac/Out/L2/P"] = l2.get("ACLoadPower")
            load_total = (l1.get("ACLoadPower") or 0) + (l2.get("ACLoadPower") or 0)
            s["/Ac/Out/P"] = round(load_total)

            # State mapping: Venus values: 0=Off 3=Bulk 4=Abs 5=Float 8=Passthru 9=Inverting
            # Conext DeviceState: 0=Standby 1=Search 2=Charging 3=Operating
            # "Operating" can be inverting (no grid) or passthru (grid connected)
            ds = l1.get("DeviceState")
            ie = l1.get("InverterEnabled")
            ce = l1.get("ChargerEnabled")
            if ds is None:
                venus_state = 0
            elif ds == 0:
                venus_state = 0   # Off/Standby
            elif ds == 1:
                venus_state = 9   # Search → Inverting
            elif ds == 2:
                venus_state = 3   # Charging → Bulk
            elif ds == 3:
                # Operating: check if grid is present
                if ac_connected:
                    venus_state = 8   # Passthru (grid present)
                else:
                    venus_state = 9   # Inverting (no grid)
            else:
                venus_state = 9   # Unknown → assume inverting
            s["/State"] = venus_state
            if ie and ce:     s["/Mode"] = 3
            elif ie:          s["/Mode"] = 2
            elif ce:          s["/Mode"] = 1
            else:             s["/Mode"] = 3
            # Don't raise GridLost alarm - AC1 registers read 0 during "Qualifying AC"
            # state even when grid is physically present. Only alarm if we previously
            # had grid and lost it (not implemented yet - always 0 for now).
            s["/Alarms/GridLost"] = 0

            log.info(
                "L1[%d]:%.1fV %dW ld:%dW | L2[%d]:%.1fV %dW ld:%dW | Tot DC:%dW Ld:%dW",
                UNIT_L1, dcv1, l1.get("DCPower",0) or 0, l1.get("ACLoadPower",0) or 0,
                UNIT_L2, dcv2, l2.get("DCPower",0) or 0, l2.get("ACLoadPower",0) or 0,
                dc_power, load_total)

        except ConnectionError as e:
            self.errors += 1
            log.warning("Connection lost: %s (err %d)", e, self.errors)
            self.connected = False
            self.client.close()
        except Exception as e:
            self.errors += 1
            log.warning("Poll error: %s (err %d)", e, self.errors)
            if self.errors > 5:
                self.connected = False
                self.client.close()
        return True

    def setup(self):
        s = VeDbusService("com.victronenergy.vebus.conext_0",
                          bus=dbus.SystemBus(), register=False)
        self.svc = s
        s.add_path("/Mgmt/ProcessName", __file__)
        s.add_path("/Mgmt/ProcessVersion", "1.4.0")
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
        log.info("DBUS service registered (v1.4 — controls are LOG ONLY, no Modbus writes)")

    def run(self):
        DBusGMainLoop(set_as_default=True)
        self.setup()
        GLib.timeout_add(POLL_INTERVAL_MS, self._update)
        log.info("Bridge v1.4: L1=ID%d L2=ID%d @ %s:%d every %dms",
                 UNIT_L1, UNIT_L2, CONEXT_IP, CONEXT_PORT, POLL_INTERVAL_MS)
        GLib.MainLoop().run()

if __name__ == "__main__":
    ConextBridge().run()
