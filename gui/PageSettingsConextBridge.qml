import QtQuick 2
import com.victron.velib 1.0

MbPage {
title: qsTr("Conext Bridge Settings")
property string settingsPrefix: "com.victronenergy.settings/Settings/ConextBridge"

model: VisibleItemModel {
MbItemRow {
description: qsTr("Driver Status")
value: statusItem.valid ? (statusItem.value === 1 ? qsTr("Connected") : qsTr("Offline")) : qsTr("Service Down")

VBusItem {
id: statusItem
bind: "com.victronenergy.vebus.conext_0/Connected"
}
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
description: qsTr("Apply Settings Now")
value: qsTr("Press to restart bridge")
onClicked: {
restartItem.setValue(1)
toast.createToast(qsTr("Restarting Conext Bridge..."), 3000)
}

VBusItem {
id: restartItem
bind: settingsPrefix + "/RestartRequested"
}
}
}
}
