import QtQuick 2
import com.victron.velib 1.0

MbPage {
title: qsTr("Conext Bridge Settings")
property string settingsPrefix: "com.victronenergy.settings/Settings/ConextBridge"

property VBusItem scanItem: VBusItem { bind: settingsPrefix + "/ScanRequested" }
property VBusItem restartItem: VBusItem { bind: settingsPrefix + "/RestartRequested" }
property VBusItem statusItem: VBusItem { bind: settingsPrefix + "/DriverStatus" }

model: VisibleItemModel {
MbItemOptions {
description: qsTr("Driver Status")
bind: settingsPrefix + "/DriverStatus"
readonly: true
possibleValues: [
MbOption { description: qsTr("Offline"); value: 0 },
MbOption { description: qsTr("Connected"); value: 1 }
]
}

MbEditBoxIp {
description: qsTr("Gateway IP")
item.bind: settingsPrefix + "/GatewayIp"
}

MbSpinBox {
description: qsTr("Gateway Port")
item {
bind: settingsPrefix + "/GatewayPort"
min: 1
max: 65535
step: 1
decimals: 0
}
}

MbEditBox {
description: qsTr("Unit IDs (e.g. 11,12)")
maximumLength: 20
item.bind: settingsPrefix + "/UnitIds"
}

MbSpinBox {
description: qsTr("Number of Inverters")
item {
bind: settingsPrefix + "/UnitCount"
min: 1
max: 4
step: 1
decimals: 0
}
}

MbSpinBox {
description: qsTr("Poll Interval (ms)")
item {
bind: settingsPrefix + "/PollInterval"
min: 1000
max: 10000
step: 500
decimals: 0
}
}

MbOK {
description: qsTr("Auto-Discover Network")
value: qsTr("Starts Modbus scan")
onClicked: {
scanItem.setValue(1)
toast.createToast(qsTr("Scanning subnet (~10s)..."), 5000)
}

}

MbOK {
description: qsTr("Apply Settings Now")
value: qsTr("Press to restart bridge")
onClicked: {
restartItem.setValue(1)
toast.createToast(qsTr("Restarting Conext Bridge..."), 3000)
}

}
}
}
