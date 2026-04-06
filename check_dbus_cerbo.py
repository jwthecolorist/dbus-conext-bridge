#!/usr/bin/env python3
import dbus
import sys

bus = dbus.SystemBus()
try:
    obj = bus.get_object("com.victronenergy.vebus.conext_0", "/Connected")
    connected = obj.GetValue(dbus_interface="com.victronenergy.BusItem")
    print(f"CONNECTED_TYPE: {type(connected)}")
    print(f"CONNECTED_VALUE: {connected}")
except Exception as e:
    print(f"ERROR_CONNECTED: {e}")

try:
    obj2 = bus.get_object("com.victronenergy.vebus.conext_0", "/ProductName")
    name = obj2.GetValue(dbus_interface="com.victronenergy.BusItem")
    print(f"PRODUCT_NAME: {name}")
except Exception as e:
    print(f"ERROR_PRODUCT: {e}")

