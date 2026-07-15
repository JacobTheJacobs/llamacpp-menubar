import AppKit
import Foundation

// MARK: - Paths & config

let appName = "Llama Menu"
let bundleId = "com.llamamenu.app"
let defaultPort = 8180
let defaultHost = "127.0.0.1"
let configDir = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".config/llama-menu")
let configPath = configDir.appendingPathComponent("config.json")
let logPath = configDir.appendingPathComponent("logs/server.log")
let modelsDefault = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("models")

struct AppConfig: Codable {
    var llama_server: String = ""
    var models_dir: String = modelsDefault.path
    var host: String = defaultHost
    var port: Int = defaultPort
    var ngl: Int = 999
    var batch: Int = 512
    var threads: Int = 0
    var stop_server_on_quit: Bool = true
}

enum ServerState {
    case stopped, starting, running, error
}

// MARK: - Helpers

func ensureConfigDir() {
    try? FileManager.default.createDirectory(
        at: configDir.appendingPathComponent("logs"),
        withIntermediateDirectories: true
    )
}

func loadConfig() -> AppConfig {
    ensureConfigDir()
    var cfg = AppConfig()
    if let data = try? Data(contentsOf: configPath),
       let decoded = try? JSONDecoder().decode(AppConfig.self, from: data) {
        cfg = decoded
    }
    if cfg.llama_server.isEmpty {
        cfg.llama_server = discoverLlamaServer() ?? ""
    }
    if cfg.threads <= 0 {
        cfg.threads = ProcessInfo.processInfo.activeProcessorCount
    }
    // Avoid Docker on 8080
    if cfg.port == 8080 { cfg.port = defaultPort }
    saveConfig(cfg)
    return cfg
}

func saveConfig(_ cfg: AppConfig) {
    ensureConfigDir()
    if let data = try? JSONEncoder().encode(cfg) {
        try? data.write(to: configPath)
    }
}

func discoverLlamaServer() -> String? {
    let candidates = [
        "/opt/homebrew/bin/llama-server",
        "/usr/local/bin/llama-server",
    ]
    for c in candidates where FileManager.default.isExecutableFile(atPath: c) {
        return c
    }
    return nil
}

func totalRamGB() -> Double {
    Double(ProcessInfo.processInfo.physicalMemory) / 1_073_741_824.0
}

func modelSizeGB(_ path: String) -> Double {
    guard let attrs = try? FileManager.default.attributesOfItem(atPath: path),
          let size = attrs[.size] as? NSNumber else { return 0 }
    return size.doubleValue / 1_073_741_824.0
}

func shortName(_ path: String, limit: Int = 34) -> String {
    var n = (path as NSString).lastPathComponent
    if n.hasSuffix(".gguf") { n = String(n.dropLast(5)) }
    if n.count > limit {
        return String(n.prefix(limit - 1)) + "…"
    }
    return n
}

func findMmproj(for modelPath: String) -> String? {
    let dir = (modelPath as NSString).deletingLastPathComponent
    let fm = FileManager.default
    guard let files = try? fm.contentsOfDirectory(atPath: dir) else { return nil }
    let hits = files.filter {
        let l = $0.lowercased()
        return l.hasSuffix(".gguf") && (l.contains("mmproj") || l.contains("projector"))
    }
    // Prefer f16/f32
    let sorted = hits.sorted { a, b in
        let ascore = (a.lowercased().contains("f32") || a.lowercased().contains("f16")) ? 0 : 1
        let bscore = (b.lowercased().contains("f32") || b.lowercased().contains("f16")) ? 0 : 1
        return ascore < bscore
    }
    return sorted.first.map { (dir as NSString).appendingPathComponent($0) }
}

func recommendCtx(modelPath: String) -> Int {
    let free = totalRamGB() - modelSizeGB(modelPath) - 2.5
    if free >= 14 { return 32768 }
    if free >= 8 { return 16384 }
    if free >= 4 { return 8192 }
    return 4096
}

func scanModels(dir: String) -> [(group: String, path: String)] {
    let root = (dir as NSString).expandingTildeInPath
    var results: [(String, String)] = []
    let fm = FileManager.default
    guard let enumerator = fm.enumerator(atPath: root) else { return [] }
    while let rel = enumerator.nextObject() as? String {
        if rel.hasSuffix(".gguf"), !rel.lowercased().contains("mmproj"),
           !rel.lowercased().contains("projector") {
            let full = (root as NSString).appendingPathComponent(rel)
            let parts = rel.split(separator: "/")
            let group = parts.count > 1 ? String(parts[0]) : "__root__"
            results.append((group, full))
        }
    }
    return results.sorted { $0.1.localizedCaseInsensitiveCompare($1.1) == .orderedAscending }
}

func logLine(_ s: String) {
    ensureConfigDir()
    let line = "\(ISO8601DateFormatter().string(from: Date())) \(s)\n"
    if let data = line.data(using: .utf8) {
        if FileManager.default.fileExists(atPath: logPath.path) {
            if let h = try? FileHandle(forWritingTo: logPath) {
                h.seekToEndOfFile()
                h.write(data)
                try? h.close()
            }
        } else {
            try? data.write(to: logPath)
        }
    }
}

// MARK: - App

final class AppController: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var statusItem: NSStatusItem!
    private var cfg: AppConfig!
    private var state: ServerState = .stopped
    private var currentModel: String?
    private var serverProcess: Process?
    private var errorMessage: String = ""
    private var pollTimer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Menu-bar style app — process identity is our native binary (like Llama-macOS)
        NSApp.setActivationPolicy(.accessory)

        cfg = loadConfig()
        logLine("Swift host start port=\(cfg.port) binary=\(cfg.llama_server)")

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let btn = statusItem.button {
            btn.title = "🦙 Llama"
            btn.toolTip = "Llama Menu"
            btn.imagePosition = .imageLeading
        }
        // Force visible (macOS can remember a hidden state)
        statusItem.isVisible = true
        statusItem.autosaveName = "com.llamamenu.app.statusitem"

        rebuildMenu()
        updateTitle()

        pollTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.poll()
        }

        // Banner (UserNotifications would need entitlement; osascript is fine)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = [
            "-e",
            "display notification \"Menu bar top-right: 🦙 Llama\" with title \"Llama Menu\"",
        ]
        try? p.run()
    }

    func applicationWillTerminate(_ notification: Notification) {
        if cfg.stop_server_on_quit {
            stopServer()
        }
    }

    // MARK: Title / menu

    private func updateTitle() {
        statusItem.isVisible = true
        guard let btn = statusItem.button else { return }
        switch state {
        case .running:
            btn.title = "🦙 Llama !"
            // Green-ish: use template off + system doesn't allow easy color text;
            // keep bang for state.
        case .starting:
            btn.title = "🦙 Llama …"
        case .error:
            btn.title = "🦙 Llama ×"
        case .stopped:
            btn.title = "🦙 Llama"
        }
    }

    private func rebuildMenu() {
        let menu = NSMenu()
        menu.autoenablesItems = false
        menu.delegate = self

        switch state {
        case .running:
            let name = shortName(currentModel ?? "model", limit: 28)
            let vision = findMmproj(for: currentModel ?? "") != nil ? " · vision" : ""
            addDisabled(menu, "●  On  ·  \(name)\(vision)")
            menu.addItem(.separator())
            add(menu, "Open Chat", #selector(openChat))
            add(menu, "Stop", #selector(stopServerAction))
            menu.addItem(.separator())
            addModelSubmenu(menu, title: "Switch Model")
            menu.addItem(.separator())
            add(menu, "Quit", #selector(quitApp), key: "q")

        case .starting:
            let name = shortName(currentModel ?? "model", limit: 28)
            addDisabled(menu, "…  Starting  ·  \(name)")
            menu.addItem(.separator())
            add(menu, "Stop", #selector(stopServerAction))
            menu.addItem(.separator())
            add(menu, "Quit", #selector(quitApp), key: "q")

        case .error:
            var err = errorMessage.isEmpty ? "Something went wrong" : errorMessage
            if err.count > 42 { err = String(err.prefix(39)) + "…" }
            addDisabled(menu, "!  \(err)")
            menu.addItem(.separator())
            addModelSubmenu(menu, title: "Start Model")
            add(menu, "Show Log", #selector(openLog))
            menu.addItem(.separator())
            add(menu, "Quit", #selector(quitApp), key: "q")

        case .stopped:
            addDisabled(menu, "○  Off")
            if cfg.llama_server.isEmpty || !FileManager.default.isExecutableFile(atPath: cfg.llama_server) {
                addDisabled(menu, "Install llama.cpp first")
            }
            menu.addItem(.separator())
            addModelSubmenu(menu, title: "Start Model")
            menu.addItem(.separator())
            add(menu, "Quit", #selector(quitApp), key: "q")
        }

        statusItem.menu = menu
        updateTitle()
    }

    private func add(_ menu: NSMenu, _ title: String, _ sel: Selector, key: String = "") {
        let item = NSMenuItem(title: title, action: sel, keyEquivalent: key)
        item.target = self
        item.isEnabled = true
        menu.addItem(item)
    }

    private func addDisabled(_ menu: NSMenu, _ title: String) {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        menu.addItem(item)
    }

    private func addModelSubmenu(_ menu: NSMenu, title: String) {
        let parent = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        let sub = NSMenu()
        sub.autoenablesItems = false

        let models = scanModels(dir: cfg.models_dir)
        if models.isEmpty {
            let empty = NSMenuItem(title: "No models yet", action: nil, keyEquivalent: "")
            empty.isEnabled = false
            sub.addItem(empty)
        } else {
            var groups: [String: [String]] = [:]
            for (g, p) in models {
                groups[g, default: []].append(p)
            }
            for g in groups.keys.sorted(by: { $0.localizedCaseInsensitiveCompare($1) == .orderedAscending }) {
                let paths = groups[g]!.sorted {
                    shortName($0).localizedCaseInsensitiveCompare(shortName($1)) == .orderedAscending
                }
                if g == "__root__" {
                    for p in paths { addModelItem(sub, path: p) }
                } else {
                    let gItem = NSMenuItem(title: g, action: nil, keyEquivalent: "")
                    let gSub = NSMenu()
                    gSub.autoenablesItems = false
                    for p in paths { addModelItem(gSub, path: p) }
                    gItem.submenu = gSub
                    sub.addItem(gItem)
                }
            }
        }
        parent.submenu = sub
        menu.addItem(parent)
    }

    private func addModelItem(_ menu: NSMenu, path: String) {
        let size = String(format: "%.1f GB", modelSizeGB(path))
        let active = (state == .running || state == .starting) && currentModel == path
        let eye = findMmproj(for: path) != nil ? " · 👁" : ""
        let prefix = active ? "✓  " : "    "
        let title = "\(prefix)\(shortName(path))\(eye)    \(size)"
        let item = NSMenuItem(title: title, action: #selector(startModel(_:)), keyEquivalent: "")
        item.target = self
        item.representedObject = path
        item.isEnabled = true
        menu.addItem(item)
    }

    // MARK: Actions

    @objc private func startModel(_ sender: NSMenuItem) {
        guard let path = sender.representedObject as? String else { return }
        startServer(model: path)
    }

    @objc private func stopServerAction() { stopServer() }

    @objc private func openChat() {
        guard state == .running else { return }
        let url = URL(string: "http://127.0.0.1:\(cfg.port)/")!
        NSWorkspace.shared.open(url)
    }

    @objc private func openLog() {
        ensureConfigDir()
        if !FileManager.default.fileExists(atPath: logPath.path) {
            try? Data().write(to: logPath)
        }
        NSWorkspace.shared.open(logPath)
    }

    @objc private func quitApp() {
        NSApp.terminate(nil)
    }

    // MARK: Server

    private func startServer(model: String) {
        stopServer()

        let binary = cfg.llama_server
        guard !binary.isEmpty, FileManager.default.isExecutableFile(atPath: binary) else {
            state = .error
            errorMessage = "llama-server not found"
            rebuildMenu()
            return
        }

        let ctx = recommendCtx(modelPath: model)
        let threads = cfg.threads > 0 ? cfg.threads : ProcessInfo.processInfo.activeProcessorCount
        var args = [
            "--model", model,
            "--host", cfg.host,
            "--port", "\(cfg.port)",
            "-ngl", "\(cfg.ngl)",
            "-c", "\(ctx)",
            "-t", "\(threads)",
            "-b", "\(cfg.batch)",
            "-np", "1",
            "--jinja",
        ]
        if let mm = findMmproj(for: model) {
            args += ["--mmproj", mm]
        }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: binary)
        proc.arguments = args
        // Log file
        ensureConfigDir()
        if !FileManager.default.fileExists(atPath: logPath.path) {
            FileManager.default.createFile(atPath: logPath.path, contents: nil)
        }
        if let fh = try? FileHandle(forWritingTo: logPath) {
            fh.seekToEndOfFile()
            let header = "\n--- \(Date()) start \(shortName(model)) ctx=\(ctx) ---\n"
            if let d = header.data(using: .utf8) { fh.write(d) }
            proc.standardOutput = fh
            proc.standardError = fh
        }

        do {
            try proc.run()
            serverProcess = proc
            currentModel = model
            state = .starting
            errorMessage = ""
            logLine("started pid=\(proc.processIdentifier) \(args.joined(separator: " "))")
            rebuildMenu()
            // Poll for readiness off main thread
            let pid = proc.processIdentifier
            DispatchQueue.global(qos: .userInitiated).async {
                Task { await self.waitReady(pid: pid) }
            }
        } catch {
            state = .error
            errorMessage = error.localizedDescription
            rebuildMenu()
        }
    }

    private func stopServer() {
        if let proc = serverProcess, proc.isRunning {
            proc.terminate()
            // Give it a moment then kill
            DispatchQueue.global().asyncAfter(deadline: .now() + 2) {
                if proc.isRunning { proc.interrupt() }
            }
        }
        serverProcess = nil
        // Also pkill by port
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        p.arguments = ["-f", "llama-server.*--port \(cfg.port)"]
        try? p.run()
        p.waitUntilExit()

        currentModel = nil
        state = .stopped
        errorMessage = ""
        rebuildMenu()
    }

    private func waitReady(pid: Int32) async {
        for _ in 0..<120 {
            try? await Task.sleep(nanoseconds: 500_000_000)
            if serverProcess?.isRunning != true {
                DispatchQueue.main.async {
                    self.state = .error
                    self.errorMessage = "Server exited"
                    self.rebuildMenu()
                }
                return
            }
            if healthOKSync() {
                DispatchQueue.main.async {
                    self.state = .running
                    self.rebuildMenu()
                    let p = Process()
                    p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
                    p.arguments = [
                        "-e",
                        "display notification \"Ready · \(shortName(self.currentModel ?? ""))\" with title \"Llama Menu\"",
                    ]
                    try? p.run()
                }
                return
            }
        }
        DispatchQueue.main.async {
            self.state = .error
            self.errorMessage = "Timed out loading"
            self.rebuildMenu()
        }
    }

    private func healthOKSync() -> Bool {
        let urls = [
            "http://127.0.0.1:\(cfg.port)/health",
            "http://127.0.0.1:\(cfg.port)/v1/models",
        ]
        for s in urls {
            guard let url = URL(string: s) else { continue }
            var req = URLRequest(url: url, timeoutInterval: 0.6)
            req.httpMethod = "GET"
            let sem = DispatchSemaphore(value: 0)
            var ok = false
            URLSession.shared.dataTask(with: req) { _, resp, _ in
                ok = (resp as? HTTPURLResponse)?.statusCode == 200
                sem.signal()
            }.resume()
            _ = sem.wait(timeout: .now() + 0.8)
            if ok { return true }
        }
        return false
    }

    private func poll() {
        statusItem.isVisible = true
        // Adopt external server or detect death
        if state == .running || state == .starting {
            if let proc = serverProcess, !proc.isRunning {
                serverProcess = nil
                state = .stopped
                currentModel = nil
                rebuildMenu()
            }
        }
        // If we think stopped but something listens on our port with llama
        if state == .stopped {
            // optional adopt — skip for simplicity
        }
        updateTitle()
    }
}

// MARK: - main

let app = NSApplication.shared
let delegate = AppController()
app.delegate = delegate
// Keep strong ref
_ = delegate
app.run()
