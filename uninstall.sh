#!/bin/sh
#
# dbus-conext-bridge uninstaller for Venus OS
# Removes the Conext bridge driver and service.
#
set -e

INSTALL_DIR="/data/dbus-conext-bridge"
SERVICE_DIR="/service/dbus-conext-bridge"

echo "=== dbus-conext-bridge uninstaller ==="

# Stop the service and firmly kill any lingering python processes
if [ -L "$SERVICE_DIR" ]; then
    svc -d "$SERVICE_DIR" 2>/dev/null || true
    sleep 2
    rm -f "$SERVICE_DIR"
    echo "  Service stopped and unregistered"
fi
pkill -f dbus-conext-bridge.py || true
pkill -f conext-poller.py || true

# Remove startup hook
if [ -f "/data/rc.local" ]; then
    sed -i '/dbus-conext-bridge/d' /data/rc.local
    sed -i '/--- Conext Bridge/d' /data/rc.local
    echo "  Removed rc.local hook"
fi

# Remove installation
if [ -d "$INSTALL_DIR" ]; then
    # Optionally preserve config
    if [ -f "$INSTALL_DIR/config.ini" ]; then
        cp "$INSTALL_DIR/config.ini" "/tmp/dbus-conext-bridge-config.ini.bak"
        echo "  Config backup saved to /tmp/dbus-conext-bridge-config.ini.bak"
    fi
    rm -rf "$INSTALL_DIR"
    echo "  Removed $INSTALL_DIR"
fi

# Remove logs
rm -rf /var/log/dbus-conext-bridge 2>/dev/null || true

echo ""
echo "=== Uninstall complete ==="
