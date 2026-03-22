#!/bin/sh
#
# dbus-conext-bridge installer for Venus OS
# Installs the Conext XW Pro bridge driver as a runit service.
#
set -e

INSTALL_DIR="/data/dbus-conext-bridge"
SERVICE_DIR="/service/dbus-conext-bridge"
SRC_DIR="$(dirname "$0")"

echo "=== dbus-conext-bridge installer ==="
echo ""

# Create install directory
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/service/log"

# Copy driver
cp "$SRC_DIR/dbus-conext-bridge.py" "$INSTALL_DIR/"
echo "  Installed driver"

# Copy config (preserve existing)
if [ -f "$INSTALL_DIR/config.ini" ]; then
    echo "  Existing config.ini preserved (not overwritten)"
    cp "$SRC_DIR/config.default.ini" "$INSTALL_DIR/config.default.ini"
else
    cp "$SRC_DIR/config.default.ini" "$INSTALL_DIR/config.ini"
    echo "  Installed default config.ini"
fi

# Create service run script
cat > "$INSTALL_DIR/service/run" << 'EOF'
#!/bin/sh
exec 2>&1
exec python3 /data/dbus-conext-bridge/dbus-conext-bridge.py
EOF
chmod +x "$INSTALL_DIR/service/run"
echo "  Created service/run"

# Create log service
cat > "$INSTALL_DIR/service/log/run" << 'EOF'
#!/bin/sh
exec svlogd -tt /var/log/dbus-conext-bridge
EOF
chmod +x "$INSTALL_DIR/service/log/run"
mkdir -p /var/log/dbus-conext-bridge
echo "  Created service/log/run"

# Register runit service (symlink to /service)
if [ ! -L "$SERVICE_DIR" ]; then
    ln -s "$INSTALL_DIR/service" "$SERVICE_DIR"
    echo "  Registered runit service"
else
    echo "  Service already registered"
fi

# Restart service
svc -t "$SERVICE_DIR" 2>/dev/null || true
sleep 3

echo ""
echo "=== Installation complete ==="
svstat "$SERVICE_DIR"
echo ""
echo "Edit $INSTALL_DIR/config.ini to configure:"
echo "  - Gateway IP address"
echo "  - Inverter unit IDs"
echo "  - Number of inverters"
echo ""
echo "Restart after config change: svc -t $SERVICE_DIR"
