/////// Conext Bridge settings page (gui-v1)
///////
/////// Install to /opt/victronenergy/gui/qml/PageSettingsConextBridge.qml

import QtQuick 1.4
import "utils.js" as Utils
import com.victron.velib 1.0

MbPage
{
        id: root
        title: qsTr("Conext Bridge")

        property string settingsPrefix: "com.victronenergy.settings"
        property string sp: "/Settings/ConextBridge"

        model: VisualItemModel
        {
                MbItemText
                {
                        text: qsTr("Conext XW Pro Bridge Settings")
                        description: qsTr("Configure gateway connection and inverter units")
                }

                MbEditBoxIp
                {
                        description: qsTr("Gateway IP Address")
                        item.bind: Utils.path(settingsPrefix, sp + "/GatewayIp")
                        showAccessLevel: User.AccessInstaller
                }

                MbEditBox
                {
                        description: qsTr("Gateway Port")
                        item.bind: Utils.path(settingsPrefix, sp + "/GatewayPort")
                        maximumLength: 5
                        numericOnlyLayout: true
                        showAccessLevel: User.AccessInstaller
                }

                MbEditBox
                {
                        description: qsTr("Unit IDs (comma-separated)")
                        item.bind: Utils.path(settingsPrefix, sp + "/UnitIds")
                        maximumLength: 20
                        showAccessLevel: User.AccessInstaller
                }

                MbItemOptions
                {
                        description: qsTr("Number of Inverters")
                        bind: Utils.path(settingsPrefix, sp + "/UnitCount")
                        possibleValues:
                        [
                                MbOption { description: "1"; value: 1 },
                                MbOption { description: "2"; value: 2 },
                                MbOption { description: "3"; value: 3 },
                                MbOption { description: "4"; value: 4 }
                        ]
                        showAccessLevel: User.AccessInstaller
                }

                MbItemOptions
                {
                        description: qsTr("Poll Interval")
                        bind: Utils.path(settingsPrefix, sp + "/PollInterval")
                        possibleValues:
                        [
                                MbOption { description: "1 second"; value: 1000 },
                                MbOption { description: "2 seconds"; value: 2000 },
                                MbOption { description: "3 seconds"; value: 3000 },
                                MbOption { description: "5 seconds"; value: 5000 },
                                MbOption { description: "10 seconds"; value: 10000 }
                        ]
                        showAccessLevel: User.AccessInstaller
                }

                MbItemText
                {
                        text: qsTr("Note")
                        description: qsTr("Restart service after changes:\nsvc -t /service/dbus-conext-bridge")
                }
        }
}
