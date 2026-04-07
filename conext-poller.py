#!/usr/bin/env python3
"""
conext-poller v3.0 — Standalone Modbus poller for Conext XW Pro inverters.
Reads registers from all units, writes JSON cache to /tmp/conext_cache.json.
Runs as a separate daemontools service from dbus-conext-bridge.

Cache is written atomically (write to tmp file, then rename) so readers
always get a consistent snapshot. If this process crashes, the DBUS bridge
just serves the last cached values.

v3.0 changes:
  - Removed dbus_get() — all config from config.ini (no blocking subprocess)
  - Exponential backoff for connection retries (5s, 10s, 20s, max 60s)
  - Throttled per-unit logging (every 10th poll)
"""
import sys, os, socket, struct, json, time, configparser, logging

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("conext-poller")

CFG = configparser.ConfigParser()
CFG_PATH = "/data/dbus-conext-bridge/config.ini"
if os.path.exists(CFG_PATH):
    CFG.read(CFG_PATH)

# All config from config.ini only — no subprocess/dbus CLI calls
CONEXT_IP = CFG.get("modbus", "ip", fallback="192.168.1.223")
CONEXT_PORT = CFG.getint("modbus", "port", fallback=503)
MODBUS_TIMEOUT = CFG.getfloat("modbus", "timeout", fallback=2.0)
INTER_READ_DELAY = CFG.getfloat("modbus", "inter_read_delay_ms", fallback=50) / 1000.0

UNIT_IDS = [int(x.strip()) for x in CFG.get("inverters", "unit_ids", fallback="11,12").split(",")]
POLL_INTERVAL = CFG.getint("inverters", "poll_interval_ms", fallback=3000) / 1000.0

CACHE_PATH = "/tmp/conext_cache.json"
CACHE_TMP = "/tmp/conext_cache.tmp"
WRITE_CMD_PATH = "/tmp/conext_write_cmd.json"

# Sentinel values: Conext returns these when a register is not available
# (e.g. AC2 registers when no generator is connected)
SENTINEL_U16 = 0xFFFF
SENTINEL_U32 = 0xFFFFFFFF
SENTINEL_S32 = -1  # 0xFFFFFFFF as signed

# Register definitions: name -> (reg, count, struct_fmt, scale, is_signed)
# Each XW Pro inverter has:
#   - DC bus (battery side)
#   - AC1 port (Grid/Shore)
#   - AC2 port (Generator)
#   - AC Load port (Critical loads)
# Each port reports per-leg (L1, L2) for split-phase systems
REGS = {
    # Device state
    "DeviceState":     (64,  1, ">H", 1, False),
    "InverterEnabled": (71,  1, ">H", 1, False),
    "ChargerEnabled":  (72,  1, ">H", 1, False),

    # DC (Battery)
    "DCVoltage":       (80,  2, ">I", 0.001, False),
    "DCCurrent":       (82,  2, ">i", 0.001, True),
    "DCPower":         (84,  2, ">i", 1,     True),

    # AC1 (Grid/Shore)
    "AC1Frequency":    (97,  1, ">H", 0.01,  False),
    "AC1Power":        (102, 2, ">i", 1,     True),
    "AC1L1Voltage":    (110, 2, ">I", 0.001, False),
    "AC1L2Voltage":    (112, 2, ">I", 0.001, False),
    "AC1L1Current":    (116, 2, ">i", 0.001, True),

    # AC2 (Generator) — returns 0xFFFF sentinels when not connected
    "AC2Frequency":    (125, 1, ">H", 0.01,  False),
    "AC2Power":        (130, 2, ">i", 1,     True),
    "AC2L1Voltage":    (138, 2, ">I", 0.001, False),
    "AC2L1Current":    (140, 2, ">i", 0.001, True),

    # AC Load (Critical loads) — per-leg for split-phase
    "ACLoadL1Voltage": (142, 2, ">I", 0.001, False),
    "ACLoadL2Voltage": (144, 2, ">I", 0.001, False),
    "ACLoadL1Current": (146, 2, ">i", 0.001, True),
    "ACLoadL2Current": (148, 2, ">i", 0.001, True),
    "ACLoadFrequency": (152, 1, ">H", 0.01,  False),
    "ACLoadPower":     (154, 2, ">i", 1,     True),

    # AC Input Current Limits (Breaker Size) — R/W
    # Doc says reg 393/394, but wire protocol is 0-based: 392/393
    "AC1BreakerSize":  (392, 1, ">H", 0.01,  False),
    "AC2BreakerSize":  (393, 1, ">H", 0.01,  False),
}

POLL_KEYS = list(REGS.keys())


class ModbusTCP:
    def __init__(self, ip, port, timeout=2.0):
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

    def _rx(self, n):
        b = b""
        while len(b) < n:
            try:
                c = self.sock.recv(n - len(b))
            except socket.timeout:
                raise ConnectionError("Socket timeout")
            if not c: raise ConnectionError("Socket closed")
            b += c
        return b

    def _read_frame(self, expected_tid):
        # Read MBAP header minus UID to get the exact payload length
        mbap = self._rx(6)
        tid, pid, length = struct.unpack(">HHH", mbap)
        # Read the rest of the frame (UID + PDU) specified by Length
        rest = self._rx(length)
        if tid != expected_tid:
            raise ConnectionError(f"Modbus TID mismatch: expected {expected_tid}, got {tid}. TCP stream corrupted!")
        return mbap + rest

    def read(self, uid, reg, cnt):
        if not self.sock: raise ConnectionError("Not connected")
        self.tid = (self.tid + 1) & 0xFFFF
        self.sock.sendall(struct.pack(">HHHBBHH", self.tid, 0, 6, uid, 3, reg, cnt))
        frame = self._read_frame(self.tid)
        fc = frame[7]
        if fc & 0x80:
            raise Exception("Modbus err fc=%d code=%d" % (fc & 0x7F, frame[8]))
        data_len = frame[8]
        return frame[9:9+data_len]

    def write_register(self, uid, reg, value):
        """Write a single holding register (FC6). value is a uint16."""
        if not self.sock: raise ConnectionError("Not connected")
        self.tid = (self.tid + 1) & 0xFFFF
        req = struct.pack(">HHHBBHH", self.tid, 0, 6, uid, 6, reg, value)
        self.sock.sendall(req)
        frame = self._read_frame(self.tid)
        fc = frame[7]
        if fc & 0x80:
            raise Exception("Modbus write err fc=%d code=%d" % (fc & 0x7F, frame[8]))
        return True


def read_reg(client, uid, name):
    """Read a register and apply sentinel filtering.
    Returns None if the register value is a sentinel (not available)."""
    reg, cnt, fmt, scale, is_signed = REGS[name]
    raw = client.read(uid, reg, cnt)
    raw_bytes = raw[:struct.calcsize(fmt)]
    val = struct.unpack(fmt, raw_bytes)[0]

    # Sentinel detection: Conext uses 0xFFFF/0xFFFFFFFF for unavailable regs
    if cnt == 1 and val == SENTINEL_U16:
        return None
    if cnt == 2:
        raw_u32 = struct.unpack(">I", raw_bytes)[0]
        if raw_u32 in (SENTINEL_U32, 0x0000FFFF, 0xFFFF0000):
            return None
        # Partial sentinel: either half-word is 0xFFFF (e.g. AC2Power = 00 00 FF FF)
        # ONLY for unsigned registers — signed registers use 0xFFFF in hi-word
        # for small negative values (e.g. DCCurrent=-8.54A = 0xFFFFDEA4)
        if not is_signed:
            hi = (raw_u32 >> 16) & 0xFFFF
            lo = raw_u32 & 0xFFFF
            if hi == SENTINEL_U16 or lo == SENTINEL_U16:
                return None

    scaled = val * scale

    # Sanity checks for known value ranges
    if "Frequency" in name and (scaled > 100 or scaled < 0):
        return None  # Frequency should be 0-100 Hz
    if "Voltage" in name and scaled > 1000:
        return None  # Voltage shouldn't exceed 1000V
    if "Current" in name and abs(scaled) > 200:
        return None  # Current shouldn't exceed 200A per unit

    return scaled


def poll_unit(client, uid):
    d = {}
    for name in POLL_KEYS:
        try:
            d[name] = read_reg(client, uid, name)
            if INTER_READ_DELAY > 0:
                time.sleep(INTER_READ_DELAY)
        except Exception as e:
            log.debug("UID%d read %s failed: %s", uid, name, e)
            d[name] = None

    # Port-level invalidation: if a port frequency is None (sentinel),
    # all values for that port are garbage from a disconnected port
    if d.get("AC1Frequency") is None:
        for k in list(d.keys()):
            if k.startswith("AC1") and k != "AC1Frequency":
                d[k] = None
    if d.get("AC2Frequency") is None:
        for k in list(d.keys()):
            if k.startswith("AC2") and k != "AC2Frequency":
                d[k] = None
    if d.get("ACLoadFrequency") is None:
        for k in list(d.keys()):
            if k.startswith("ACLoad") and k != "ACLoadFrequency":
                d[k] = None

    return d


def write_cache(units, static_info=None):
    """Atomic write: write to tmp file, then rename."""
    data = {
        "ts": time.time(),
        "units": units,
        "info": static_info or {},
        "settings": {
            "ip": CONEXT_IP,
            "port": CONEXT_PORT,
            "unit_ids": ",".join(str(u) for u in UNIT_IDS),
            "poll_ms": int(POLL_INTERVAL * 1000),
        }
    }
    try:
        with open(CACHE_TMP, "w") as f:
            json.dump(data, f)
        os.rename(CACHE_TMP, CACHE_PATH)
    except Exception as e:
        log.error("Cache write failed: %s", e)

def fetch_static_info(client):
    """Fetch Hardware Serial Numbers and Firmware strings explicitly at startup."""
    info = {}
    
    # Gateway is typically UID 1, Master Inverter is UNIT_IDS[0]
    
    def _get_serial(uid, is_gw):
        try:
            count = 8 if is_gw else 16
            raw = client.read(uid, 43, count)
            return raw.decode('utf-8', errors='ignore').replace('\x00', '').strip()
        except: return None
        
    def _get_fw(uid, is_gw):
        try:
            if is_gw:
                raw = client.read(uid, 30, 10)
                return raw.decode('utf-8', errors='ignore').replace('\x00', '').strip()
            else:
                raw = client.read(uid, 30, 2)
                return str(struct.unpack('>I', raw)[0])
        except: return None

    # Fetch Gateway (UID 1)
    gw_ser = _get_serial(1, True)
    gw_fw = _get_fw(1, True)
    if gw_ser: info['GatewaySerial'] = gw_ser
    if gw_fw: info['GatewayFirmware'] = gw_fw
    
    # Fetch Inverters
    inverter_serials = {}
    for uid in UNIT_IDS:
        ser = _get_serial(uid, False)
        if ser: inverter_serials[str(uid)] = ser
        if len(UNIT_IDS) > 0 and uid == UNIT_IDS[0]:
            if ser: info['MasterSerial'] = ser
            fw = _get_fw(uid, False)
            if fw: info['MasterFirmware'] = fw
            
    if inverter_serials:
        info['InverterSerials'] = inverter_serials
        
    return info


# --- Writable registers: name -> (reg, scale) ---
WRITABLE_REGS = {
    "AC1BreakerSize": (392, 0.01),
    "AC2BreakerSize": (393, 0.01),
}

def process_write_commands(client):
    """Check for write commands from the DBUS bridge and execute them.
    Command file format: {"register": "AC1BreakerSize", "value": 30.0, "unit_ids": [11,12]}
    """
    if not os.path.exists(WRITE_CMD_PATH):
        return
    try:
        with open(WRITE_CMD_PATH, "r") as f:
            cmd = json.load(f)
        os.remove(WRITE_CMD_PATH)  # Consume the command immediately

        reg_name = cmd.get("register")
        value = cmd.get("value")
        target_uids = cmd.get("unit_ids", UNIT_IDS)

        if reg_name not in WRITABLE_REGS:
            log.warning("Write rejected: unknown register '%s'", reg_name)
            return
        if value is None or not isinstance(value, (int, float)):
            log.warning("Write rejected: invalid value '%s'", value)
            return

        reg_addr, scale = WRITABLE_REGS[reg_name]
        raw_value = int(round(value / scale))  # Convert from scaled to raw uint16
        raw_value = max(0, min(raw_value, 0xFFFF))  # Clamp to uint16

        for uid in target_uids:
            try:
                client.write_register(uid, reg_addr, raw_value)
                log.info("WRITE OK: UID%d %s reg=%d raw=%d (%.1fA)",
                         uid, reg_name, reg_addr, raw_value, value)
            except Exception as e:
                log.error("WRITE FAIL: UID%d %s: %s", uid, reg_name, e)
    except json.JSONDecodeError:
        log.warning("Write cmd: invalid JSON, ignoring")
        try: os.remove(WRITE_CMD_PATH)
        except: pass
    except Exception as e:
        log.error("Write cmd error: %s", e)


def main():
    client = ModbusTCP(CONEXT_IP, CONEXT_PORT, MODBUS_TIMEOUT)
    connected = False
    errors = 0
    poll_count = 0
    retry_delay = 5  # Exponential backoff: 5, 10, 20, 40, 60 max
    static_info = {}
    log.info("Poller v3.1 starting: units=%s ip=%s:%d interval=%.1fs",
             UNIT_IDS, CONEXT_IP, CONEXT_PORT, POLL_INTERVAL)

    while True:
        try:
            if not connected:
                if client.connect():
                    connected = True
                    errors = 0
                    retry_delay = 5  # Reset backoff on success
                    static_info = fetch_static_info(client)
                    log.info("Loaded static metadata: %s", static_info)
                else:
                    log.warning("Retry in %ds...", retry_delay)
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)  # Exponential backoff
                    continue

            units = {}
            for uid in UNIT_IDS:
                units[str(uid)] = poll_unit(client, uid)

            write_cache(units, static_info)
            process_write_commands(client)
            errors = 0
            poll_count += 1

            # Summary log every 10th poll (~30s) to avoid log spam
            if poll_count % 10 == 1:
                for uid in UNIT_IDS:
                    u = units[str(uid)]
                    dcv = u.get("DCVoltage") or 0
                    dci = u.get("DCCurrent") or 0
                    dcp = u.get("DCPower") or 0
                    ldp = u.get("ACLoadPower") or 0
                    ldv1 = u.get("ACLoadL1Voltage") or 0
                    ldv2 = u.get("ACLoadL2Voltage") or 0
                    ldi1 = u.get("ACLoadL1Current") or 0
                    ldi2 = u.get("ACLoadL2Current") or 0
                    log.info("UID%d: DC:%.1fV %.1fA %dW | Ld:L1=%.0fV/%.1fA L2=%.0fV/%.1fA %dW | st:%s [#%d]",
                             uid, dcv, dci, dcp, ldv1, ldi1, ldv2, ldi2, ldp,
                             u.get("DeviceState"), poll_count)

            time.sleep(POLL_INTERVAL)

        except ConnectionError as e:
            errors += 1
            log.warning("Connection lost: %s (err %d)", e, errors)
            connected = False
            client.close()
            time.sleep(3)
        except Exception as e:
            errors += 1
            log.warning("Poll error: %s (err %d)", e, errors)
            if errors > 5:
                connected = False
                client.close()
            time.sleep(3)


if __name__ == "__main__":
    main()
