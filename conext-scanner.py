#!/usr/bin/env python3
"""
conext-scanner.py
Automatically finds a Conext Gateway on the local subnet via port 503,
enumerates active Unit IDs (1-30), and updates Venus OS DBus settings.
"""
import socket
import struct
import concurrent.futures
import os
import dbus
import time

PORT = 503
SCAN_TIMEOUT = 0.2
MODBUS_TIMEOUT = 0.5

def get_local_subnet():
    # Attempt to get primary IP
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '192.168.1.1'
    finally:
        s.close()
    
    parts = ip.split('.')
    return f"{parts[0]}.{parts[1]}.{parts[2]}"

def check_port(ip):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(SCAN_TIMEOUT)
        try:
            s.connect((ip, PORT))
            return ip
        except Exception:
            return None

def find_gateways():
    subnet = get_local_subnet()
    ips_to_scan = [f"{subnet}.{i}" for i in range(1, 255)]
    gateways = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        results = [executor.submit(check_port, ip) for ip in ips_to_scan]
        for future in concurrent.futures.as_completed(results):
            ip = future.result()
            if ip:
                gateways.append(ip)
                
    return gateways

def check_modbus_uid(ip, uid):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(MODBUS_TIMEOUT)
        try:
            s.connect((ip, PORT))
            # Modbus Read Holding Registers: reg 64 (DeviceState)
            req = struct.pack(">HHHBBHH", uid, 0, 6, uid, 3, 64, 1)
            s.sendall(req)
            mbap = s.recv(6)
            if len(mbap) == 6:
                tid, pid, length = struct.unpack(">HHH", mbap)
                resp = s.recv(length)
                # Successful FC=3 (Not 0x83 exception)
                if len(resp) >= 2 and resp[1] == 3:
                    return uid
        except Exception:
            pass
    return None

def set_dbus_setting(name, value, dtype='s'):
    DBUS_SVC = "com.victronenergy.settings"
    DBUS_PATH = "/Settings/ConextBridge/" + name
    
    # We use dbus-send for simplest interaction without velib_python dependencies here
    quote = "'" if dtype == 's' else ""
    cmd = f"dbus -y {DBUS_SVC} {DBUS_PATH} SetValue {quote}{value}{quote} >/dev/null 2>&1"
    os.system(cmd)

def main():
    print("Scannning local subnet...")
    gateways = find_gateways()
    if not gateways:
        print("No Conext gateways found on local subnet.")
        return
        
    main_ip = gateways[0]
    print(f"Gateway found at: {main_ip}")
    
    print("Enumerating Unit IDs (1-30)...")
    active_uids = []
    
    # Conext gateways are slow, sequential querying is safer than concurrent for Modbus
    for uid in range(1, 31):
        if check_modbus_uid(main_ip, uid):
            print(f"  Found device at UID {uid}")
            active_uids.append(uid)
            
    if not active_uids:
        print("No responsive inverters found on gateway.")
        return
        
    uid_str = ",".join(str(u) for u in active_uids)
    count = len(active_uids)
    
    print(f"Setting DBus configurations: IP={main_ip}, UIDs={uid_str}, Count={count}")
    set_dbus_setting("GatewayIp", main_ip, 's')
    set_dbus_setting("UnitIds", uid_str, 's')
    set_dbus_setting("UnitCount", count, 'i')
    
    print("Scan complete. Updating bridge.")

if __name__ == "__main__":
    main()
