#!/bin/sh
#
# Register Conext Bridge settings in Venus OS localsettings (DBUS persistent)
# These settings are used by the bridge/poller and displayed in the GX GUI.
#
# Usage: ./register-settings.sh
#

DBUS="dbus -y com.victronenergy.settings /Settings"

echo "Registering Conext Bridge settings..."

# AddSetting <group> <name> <default> <type> <min> <max>
# Types: s=string, i=integer, f=float

$DBUS AddSetting ConextBridge GatewayIp "192.168.1.223" s 0 0
$DBUS AddSetting ConextBridge GatewayPort 503 i 1 65535
$DBUS AddSetting ConextBridge UnitIds "11,12" s 0 0
$DBUS AddSetting ConextBridge UnitCount 2 i 1 4
$DBUS AddSetting ConextBridge PollInterval 3000 i 1000 30000

echo "Settings registered. Verify with:"
echo "  dbus -y com.victronenergy.settings /Settings/ConextBridge GetValue"
