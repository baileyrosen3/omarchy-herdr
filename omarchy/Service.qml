import QtQuick
import Quickshell

Item {
  id: root

  function startSupervisor(): void {
    const helperUrl = String(Qt.resolvedUrl("../bin/herdr-shell"))
    // Accept a local absolute file URL only, and decode its path before using
    // an argv array. Spaces, percent signs, and quotes never become shell code.
    if (!helperUrl.startsWith("file:///")) {
      console.warn("Herdr Shell: cannot resolve its local helper")
      return
    }
    let helperPath
    try {
      helperPath = decodeURIComponent(helperUrl.slice(7))
    } catch (error) {
      console.warn("Herdr Shell: cannot decode its helper path", error)
      return
    }
    Quickshell.execDetached(["python3", helperPath, "_omarchy", "start"])
  }

  // The detached worker owns a singleton lock and a private runtime snapshot.
  // It survives shell hot reload and manager deletion to complete cleanup.
  Component.onCompleted: Qt.callLater(root.startSupervisor)

  Timer {
    interval: 30000
    repeat: true
    running: true
    onTriggered: root.startSupervisor()
  }
}
