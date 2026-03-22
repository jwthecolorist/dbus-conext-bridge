#!/usr/bin/env python3
"""
conext-poller — Standalone Modbus poller for Conext XW Pro inverters.
Reads registers from all units, writes JSON cache to /tmp/conext_cache.json.
Runs as a separate process from dbus-conext-bridge to avoid GLib/DBUS blocking.

Cache is written atomically (write to tmp file, then rename) so readers
always get a consistent snapshot. If this process crashes, the DBUS bridge
just serves the last cached values.
"""
import sys, os, socket, struct, json, time, configparser, logging

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("conext-poller")

CFG = configparser.ConfigParser()
CFG_PATH = "/data/dbus-conext-bridge/config.ini"
if os.path.exists(CFG_PATH):
    CFG.read(CFG_PATH)

CONEXT_IP = CFG.get("modbus", "ip", fallback="192.168.1.223")
CONEXT_PORT = CFG.getint("modbus", "port", fallback=503)
MODBUS_TIMEOUT = CFG.getfloat("modbus", "timeout", fallback=1.0)

_unit_ids = CFG.get("inverters", "unit_ids", fallback="11,12")
UNIT_IDS = [int(x.strip()) for x in _unit_ids.split(",")]
POLL_INTERVAL = CFG.getfloat("inverters", "poll_interval_ms", fallback=3000) / 1000.0

CACHE_PATH = "/tmp/conext_cache.json"
CACHE_TMP = "/tmp/conext_cache.tmp"

# Register definitions: name -> (reg, count, struct_fmt, scale)
REGS = {
    "DeviceState":     (64,1,">H",1),
    "InverterEnabled": (71,1,">H",1),
    "ChargerEnabled":  (72,1,">H",1),
    "DCVoltage":       (80,2,">I",0.001),
    "DCCurrent":       (82,2,">i",0.001),
    "DCPower":         (84,2,">i",1),
    "AC1Frequency":    (97,1,">H",0.01),
    "AC1Power":        (102,2,">i",1),
    "AC1L1Voltage":    (110,2,">I",0.001),
    "AC1L2Voltage":    (112,2,">I",0.001),
    "AC1L1Current":    (116,2,">i",0.001),
    "AC2Frequency":    (125,1,">H",0.01),
    "AC2Power":        (130,2,">i",1),
    "AC2L1Voltage":    (138,2,">I",0.001),
    "AC2L1Current":    (140,2,">i",0.001),
    "ACLoadL1Voltage": (142,2,">I",0.001),
    "ACLoadL2Voltage": (144,2,">I",0.001),
    "ACLoadL1Current": (146,2,">i",0.001),
    "ACLoadL2Current": (148,2,">i",0.001),
    "ACLoadFrequency": (152,1,">H",0.01),
    "ACLoadPower":     (154,2,">i",1),
}

POLL_KEYS = list(REGS.keys())


class ModbusTCP:
    def __init__(self, ip, port, timeout=1.0):
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

    def _rx(self, n):
        b = b""
        while len(b) < n:
            c = self.sock.recv(n - len(b))
            if not c: raise ConnectionError("Socket closed")
            b += c
        return b


def read_reg(client, uid, name):
    reg, cnt, fmt, scale = REGS[name]
    raw = client.read(uid, reg, cnt)
    return struct.unpack(fmt, raw[:struct.calcsize(fmt)])[0] * scale


def poll_unit(client, uid):
    d = {}
    for name in POLL_KEYS:
        try:
            d[name] = read_reg(client, uid, name)
        except Exception:
            d[name] = None
    return d


def write_cache(units):
    """Atomic write: write to tmp file, then rename."""
    data = {
        "ts": time.time(),
        "units": units,
    }
    try:
        with open(CACHE_TMP, "w") as f:
            json.dump(data, f)
        os.rename(CACHE_TMP, CACHE_PATH)
    except Exception as e:
        log.error("Cache write failed: %s", e)


def main():
    client = ModbusTCP(CONEXT_IP, CONEXT_PORT, MODBUS_TIMEOUT)
    connected = False
    errors = 0
    log.info("Poller starting: units=%s ip=%s:%d interval=%.1fs",
             UNIT_IDS, CONEXT_IP, CONEXT_PORT, POLL_INTERVAL)

    while True:
        try:
            if not connected:
                if client.connect():
                    connected = True
                    errors = 0
                else:
                    time.sleep(5)
                    continue

            units = {}
            for uid in UNIT_IDS:
                units[str(uid)] = poll_unit(client, uid)

            write_cache(units)
            errors = 0

            # Summary log
            for uid in UNIT_IDS:
                u = units[str(uid)]
                log.info("UID%d: %.1fV %.1fA %dW ld:%dW st:%s",
                         uid,
                         u.get("DCVoltage") or 0,
                         u.get("DCCurrent") or 0,
                         u.get("DCPower") or 0,
                         u.get("ACLoadPower") or 0,
                         u.get("DeviceState"))

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
