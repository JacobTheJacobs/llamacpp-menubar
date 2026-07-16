import Foundation
import ServiceManagement

/// Launch-at-login via SMAppService (macOS 13+).
///
/// The supported API: no plist to write, and it reports real state rather than
/// inferring it from whether a file exists.
enum LaunchAtLogin {
    /// Registration only works from a real bundle, so a dev binary reports off.
    static var isAvailable: Bool {
        Bundle.main.bundleIdentifier != nil
    }

    static var isEnabled: Bool {
        guard isAvailable else { return false }
        return SMAppService.mainApp.status == .enabled
    }

    @discardableResult
    static func set(_ enabled: Bool) -> Bool {
        guard isAvailable else { return false }
        do {
            if enabled {
                // register() throws if already registered.
                if SMAppService.mainApp.status != .enabled {
                    try SMAppService.mainApp.register()
                }
            } else if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            }
            return true
        } catch {
            logLine("launch at login \(enabled ? "enable" : "disable") failed: \(error)")
            return false
        }
    }

    /// Retire a LaunchAgent plist left by an older install of this app, so it
    /// and SMAppService can't both try to start us.
    ///
    /// Only this app's own bundle id is touched — anything else in
    /// ~/Library/LaunchAgents belongs to another app and is not ours to remove.
    static func migrateLegacyAgent() {
        let path = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(bundleId).plist")
        guard FileManager.default.fileExists(atPath: path.path) else { return }

        // Carry the user's intent across: if the old agent actually launched us
        // at login, register with SMAppService before dropping it. Otherwise
        // removing the file would silently turn the setting off.
        let runAtLoad = (try? Data(contentsOf: path))
            .flatMap {
                try? PropertyListSerialization.propertyList(from: $0, format: nil)
                    as? [String: Any]
            }
            .flatMap { $0?["RunAtLoad"] as? Bool } ?? false

        if runAtLoad {
            let ok = set(true)
            logLine("migrated launch-at-login from LaunchAgent to SMAppService (ok=\(ok))")
        }
        runTool("/bin/launchctl", ["unload", path.path])
        try? FileManager.default.removeItem(at: path)
        logLine("removed legacy launch agent \(bundleId).plist (RunAtLoad=\(runAtLoad))")
    }
}
