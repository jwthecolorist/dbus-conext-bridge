#!/bin/bash
# build-sdcard.sh — Create venus-data.tar.gz for SD card / USB installer
# Usage: ./build-sdcard.sh
# Output: venus-data.tar.gz (copy to root of FAT32 SD card or USB stick)
#
# ZERO-TOUCH INSTALLATION:
#   1. Copy venus-data.tar.gz to root of FAT32 SD card or USB stick
#   2. Insert into GX device and reboot
#   3. Venus OS extracts files to /data/ automatically
#   4. setup.sh self-registers in rc.local on first boot
#   5. Service starts, settings page appears in Settings > Conext Bridge

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="/tmp/venus-data-build"
OUT_FILE="$SCRIPT_DIR/venus-data.tar.gz"

echo "Building venus-data.tar.gz..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/dbus-conext-bridge/service/log"
mkdir -p "$BUILD_DIR/dbus-conext-bridge/poller-service/log"
mkdir -p "$BUILD_DIR/dbus-conext-bridge/gui"

# Copy driver files
cp "$SCRIPT_DIR/dbus-conext-bridge.py" "$BUILD_DIR/dbus-conext-bridge/"
cp "$SCRIPT_DIR/conext-poller.py" "$BUILD_DIR/dbus-conext-bridge/"
cp "$SCRIPT_DIR/config.default.ini" "$BUILD_DIR/dbus-conext-bridge/config.ini"
cp "$SCRIPT_DIR/gui/PageSettingsConextBridge.qml" "$BUILD_DIR/dbus-conext-bridge/gui/"

# Copy service/run scripts
cp "$SCRIPT_DIR/service/run" "$BUILD_DIR/dbus-conext-bridge/service/run"
cp "$SCRIPT_DIR/service/log/run" "$BUILD_DIR/dbus-conext-bridge/service/log/run"
cp "$SCRIPT_DIR/poller-service/run" "$BUILD_DIR/dbus-conext-bridge/poller-service/run"
cp "$SCRIPT_DIR/poller-service/log/run" "$BUILD_DIR/dbus-conext-bridge/poller-service/log/run"
chmod +x "$BUILD_DIR/dbus-conext-bridge/service/run"
chmod +x "$BUILD_DIR/dbus-conext-bridge/service/log/run"
chmod +x "$BUILD_DIR/dbus-conext-bridge/poller-service/run"
chmod +x "$BUILD_DIR/dbus-conext-bridge/poller-service/log/run"

# Create the boot-time setup script (self-registers in rc.local)
cat > "$BUILD_DIR/dbus-conext-bridge/setup.sh" << 'SETUP'
#!/bin/sh
# Conext Bridge boot-time setup (runs on every boot via rc.local)
# Self-registers in /data/rc.local on first run — zero-touch install
INSTALL_DIR="/data/dbus-conext-bridge"
DBUS_CMD="dbus -y com.victronenergy.settings /Settings"
QML_DIR="/opt/victronenergy/gui/qml"

logger "conext-bridge: Running setup..."

# 0. Self-register in rc.local (idempotent)
if [ -f /data/rc.local ]; then
    if ! grep -q 'dbus-conext-bridge/setup.sh' /data/rc.local 2>/dev/null; then
        echo "" >> /data/rc.local
        echo "# --- Conext Bridge (auto-installed) ---" >> /data/rc.local
        echo "[ -x /data/dbus-conext-bridge/setup.sh ] && /data/dbus-conext-bridge/setup.sh" >> /data/rc.local
        logger "conext-bridge: Registered in rc.local"
    fi
else
    printf '#!/bin/sh\n# --- Conext Bridge (auto-installed) ---\n[ -x /data/dbus-conext-bridge/setup.sh ] && /data/dbus-conext-bridge/setup.sh\n' > /data/rc.local
    chmod +x /data/rc.local
    logger "conext-bridge: Created rc.local with setup hook"
fi

# 1. Service symlink (/service is tmpfs, recreated each boot)
if [ ! -L /service/dbus-conext-bridge ]; then
    ln -sf "$INSTALL_DIR/service" /service/dbus-conext-bridge
    logger "conext-bridge: Bridge Service linked"
fi
if [ ! -L /service/conext-poller ]; then
    ln -sf "$INSTALL_DIR/poller-service" /service/conext-poller
    logger "conext-bridge: Poller Service linked"
fi

# 2. Register DBUS settings (safe to re-run, won't overwrite existing values)
$DBUS_CMD AddSetting ConextBridge GatewayIp "192.168.1.223" s 0 0 2>/dev/null || true
$DBUS_CMD AddSetting ConextBridge GatewayPort 503 i 1 65535 2>/dev/null || true
$DBUS_CMD AddSetting ConextBridge UnitIds "11,12" s 0 0 2>/dev/null || true
$DBUS_CMD AddSetting ConextBridge UnitCount 2 i 1 4 2>/dev/null || true
$DBUS_CMD AddSetting ConextBridge PollInterval 3000 i 1000 30000 2>/dev/null || true
$DBUS_CMD AddSetting ConextBridge RestartRequested 0 i 0 1 2>/dev/null || true

# 3. Deploy QML settings page (survives firmware updates)
if [ -d "$QML_DIR" ] && [ -f "$INSTALL_DIR/gui/PageSettingsConextBridge.qml" ]; then
    cp "$INSTALL_DIR/gui/PageSettingsConextBridge.qml" "$QML_DIR/"
    # Patch PageSettings.qml menu if not already done
    if [ -f "$QML_DIR/PageSettings.qml" ] && ! grep -q ConextBridge "$QML_DIR/PageSettings.qml"; then
        cp "$QML_DIR/PageSettings.qml" "$QML_DIR/PageSettings.qml.orig.conext"
        python3 -c "
p='$QML_DIR/PageSettings.qml'
with open(p) as f: lines=f.readlines()
entry='\n\t\tMbSubMenu {\n\t\t\tdescription: qsTr(\"Conext Bridge\")\n\t\t\tsubpage: Component { PageSettingsConextBridge {} }\n\t\t}\n'
lines.insert(-2, entry)
with open(p,'w') as f: f.writelines(lines)
" 2>/dev/null && logger "conext-bridge: QML settings page patched"
    fi
fi

logger "conext-bridge: Setup complete"
SETUP
chmod +x "$BUILD_DIR/dbus-conext-bridge/setup.sh"

# Build the tar.gz (venus-data.tar.gz extracts to /data/)
cd "$BUILD_DIR"
tar czf "$OUT_FILE" \
    dbus-conext-bridge/dbus-conext-bridge.py \
    dbus-conext-bridge/conext-poller.py \
    dbus-conext-bridge/config.ini \
    dbus-conext-bridge/gui/PageSettingsConextBridge.qml \
    dbus-conext-bridge/service/run \
    dbus-conext-bridge/service/log/run \
    dbus-conext-bridge/poller-service/run \
    dbus-conext-bridge/poller-service/log/run \
    dbus-conext-bridge/setup.sh

echo ""
echo "================================================"
echo "  venus-data.tar.gz created: $OUT_FILE"
echo "================================================"
echo ""
echo "ZERO-TOUCH INSTALLATION:"
echo "  1. Copy venus-data.tar.gz to root of FAT32 SD card or USB stick"
echo "  2. Insert into GX device and reboot"
echo "  3. Venus OS extracts to /data/ -> setup.sh self-registers in rc.local"
echo "  4. Service starts, Settings > Conext Bridge appears"
echo ""
echo "FIRMWARE UPDATES: Just reboot — rc.local re-deploys everything."
echo "DRIVER UPDATES:   Replace venus-data.tar.gz on SD card and reboot."

rm -rf "$BUILD_DIR"
