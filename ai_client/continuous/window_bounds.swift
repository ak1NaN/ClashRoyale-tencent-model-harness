import AppKit
import CoreGraphics
import Foundation

let bundleID = "com.netease.mumu.nemux.emulator"
let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
let candidates: [[String: Any]] = windows.compactMap { info in
    guard let pid = info[kCGWindowOwnerPID as String] as? Int,
          let app = NSRunningApplication(processIdentifier: pid_t(pid)),
          app.bundleIdentifier == bundleID,
          let bounds = info[kCGWindowBounds as String] as? [String: Any],
          let x = bounds["X"] as? Double,
          let y = bounds["Y"] as? Double,
          let width = bounds["Width"] as? Double,
          let height = bounds["Height"] as? Double,
          width > 200, height > 300 else { return nil }
    return ["x": x, "y": y, "width": width, "height": height,
            "title": info[kCGWindowName as String] as? String ?? ""]
}
guard let window = candidates.max(by: {
    ($0["width"] as! Double) * ($0["height"] as! Double) <
    ($1["width"] as! Double) * ($1["height"] as! Double)
}) else {
    fputs("MuMu Android device window not found\n", stderr)
    exit(2)
}
let data = try JSONSerialization.data(withJSONObject: window)
print(String(data: data, encoding: .utf8)!)
