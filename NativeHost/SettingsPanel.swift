import AppKit

/// The Settings window.
///
/// Changes persist as they are made — there is no OK/Cancel — so the host is
/// always the source of truth and pushes state back after each change rather
/// than letting the page track it.
final class SettingsPanelController: NSObject {
    private var web: WebPanel?
    private let readConfig: () -> AppConfig
    private let writeConfig: (AppConfig) -> Void
    private let isRunning: () -> Bool
    private let onClose: () -> Void

    init(
        readConfig: @escaping () -> AppConfig,
        writeConfig: @escaping (AppConfig) -> Void,
        isRunning: @escaping () -> Bool,
        onClose: @escaping () -> Void
    ) {
        self.readConfig = readConfig
        self.writeConfig = writeConfig
        self.isRunning = isRunning
        self.onClose = onClose
        super.init()
    }

    @discardableResult
    func show() -> Bool {
        guard let html = loadPanelHTML("settings.html") else {
            logLine("settings.html missing — cannot show settings")
            return false
        }
        let panel = WebPanel(
            title: "Llama Menu Settings",
            size: NSSize(width: 640, height: 660),
            minSize: NSSize(width: 560, height: 420),
            html: html,
            payload: state(),
            onMessage: { [weak self] body in self?.handle(body) },
            onClose: { [weak self] in
                self?.web = nil
                self?.onClose()
            }
        )
        web = panel
        return panel.show()
    }

    func close() {
        web?.close()
    }

    // MARK: State

    private func state() -> [String: Any] {
        let cfg = readConfig()
        return [
            "version": appVersion(),
            "models_dir": abbreviatePath(cfg.models_dir),
            "models_dir_full": cfg.models_dir,
            "llama_server": abbreviatePath(cfg.llama_server),
            "has_binary": !cfg.llama_server.isEmpty,
            "port": cfg.port,
            "kv_cache_type": cfg.kv_cache_type,
            "cache_types": uiCacheTypes,
            "stop_server_on_quit": cfg.stop_server_on_quit,
            "launch_at_login": LaunchAtLogin.isEnabled,
            "exposed": cfg.host == "0.0.0.0",
            "running": isRunning(),
            "command": previewCommand(cfg),
        ]
    }

    private func push() {
        web?.apply(state())
    }

    /// A readable preview of what these settings produce. Per-model values come
    /// from the launch panel, so they show as placeholders rather than lies.
    private func previewCommand(_ cfg: AppConfig) -> String {
        let binary = cfg.llama_server.isEmpty ? "llama-server" : cfg.llama_server
        var parts = [
            binary,
            "--model <model>.gguf",
            "--host \(cfg.host)",
            "--port \(cfg.port)",
            "-ngl \(cfg.ngl)",
            "-c <ctx>",
            "-t \(cfg.threads)",
            "-b \(cfg.batch)",
            "-np 1",
            "-ctk \(cfg.kv_cache_type)",
            "-ctv \(cfg.kv_cache_type)",
        ]
        if isQuantizedCache(cfg.kv_cache_type) { parts.append("-fa on") }
        parts.append("--jinja")
        return parts.joined(separator: " \\\n  ")
    }

    // MARK: Bridge

    private func handle(_ body: [String: Any]) {
        guard let action = body["action"] as? String else { return }
        switch action {
        case "set":
            guard let key = body["key"] as? String else { return }
            apply(key: key, value: body["value"])
        case "pickModelsDir":
            pickDirectory()
        case "pickBinary":
            pickBinary()
        case "revealConfig":
            ensureConfigDir()
            if !FileManager.default.fileExists(atPath: configPath.path) {
                writeConfig(readConfig())
            }
            NSWorkspace.shared.activateFileViewerSelecting([configPath])
        case "close":
            close()
        default:
            break
        }
    }

    private func apply(key: String, value: Any?) {
        var cfg = readConfig()
        switch key {
        case "port":
            guard let raw = (value as? NSNumber)?.intValue ?? value as? Int,
                  (1024...65535).contains(raw)
            else { return }
            cfg.port = raw
        case "kv_cache_type":
            guard let raw = value as? String, isValidCacheType(raw) else { return }
            cfg.kv_cache_type = raw
        case "stop_server_on_quit":
            cfg.stop_server_on_quit = (value as? Bool) ?? cfg.stop_server_on_quit
        case "exposed":
            // Only ever localhost or all-interfaces; never an arbitrary string
            // from the page.
            cfg.host = ((value as? Bool) ?? false) ? "0.0.0.0" : defaultHost
        case "launch_at_login":
            LaunchAtLogin.set((value as? Bool) ?? false)
            push()
            return
        default:
            return
        }
        writeConfig(cfg)
        push()
    }

    private func pickDirectory() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Use Folder"
        panel.directoryURL = URL(fileURLWithPath: readConfig().models_dir)
        guard panel.runModal() == .OK, let url = panel.url else { return }
        var cfg = readConfig()
        cfg.models_dir = url.path
        writeConfig(cfg)
        push()
    }

    private func pickBinary() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Use Binary"
        panel.message = "Select the llama-server executable"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        guard FileManager.default.isExecutableFile(atPath: url.path) else {
            NSSound.beep()
            logLine("rejected non-executable llama-server: \(url.path)")
            return
        }
        var cfg = readConfig()
        cfg.llama_server = url.path
        writeConfig(cfg)
        push()
    }
}
