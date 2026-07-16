import AppKit

enum ServerState {
    case stopped, starting, running, error
}

final class AppController: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var statusItem: NSStatusItem!
    private var cfg = AppConfig()
    private var state: ServerState = .stopped
    private var currentModel: String?
    private var serverProcess: Process?
    private var errorMessage = ""
    private var pollTimer: Timer?
    private var launchPanel: LaunchPanelController?
    private var settingsPanel: SettingsPanelController?

    /// The port the running server was actually launched on.
    ///
    /// Not the same thing as `cfg.port`: Settings can change the configured port
    /// while a server is up, and that server is still on the old one. Health
    /// checks and stop must follow the process, or the app would declare a
    /// healthy server dead and then fail to kill it, stranding it forever.
    private var activePort: Int?

    /// Invalidates in-flight readiness watchers when the user stops or restarts.
    private var readyGeneration = 0
    private var readyDeadline = Date()
    private var healthFailures = 0

    /// Serialises start/stop. These blocks signal processes and wait on them, so
    /// running two concurrently lets a stale reap outlive a newer spawn and kill
    /// the server the user just asked for.
    private let lifecycle = DispatchQueue(label: "com.llamamenu.app.lifecycle")

    /// Replacing an NSMenu while it is open is glitchy; defer until it closes.
    private var menuIsOpen = false
    private var rebuildPending = false

    // MARK: Lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        cfg = loadConfig()
        logLine("host start port=\(cfg.port) binary=\(cfg.llama_server) chip=\(hardware.chip)")
        // A LaunchAgent and SMAppService both registering would launch twice.
        LaunchAtLogin.migrateLegacyAgent()

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.autosaveName = "com.llamamenu.app.statusitem"
        statusItem.isVisible = true

        rebuildMenu()

        // A server left over from a previous run still owns the port; report it
        // rather than claiming "Off".
        adoptExistingServer()

        pollTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.poll()
        }

        if !cfg.has_seen_welcome {
            cfg.has_seen_welcome = true
            saveConfig(cfg)
            let ready = FileManager.default.isExecutableFile(atPath: cfg.llama_server)
            notify(ready ? "Click the llama in the menu bar → Start Model"
                 : "Install llama.cpp first: brew install llama.cpp")
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        guard cfg.stop_server_on_quit else { return }
        readyGeneration &+= 1
        // Synchronous is correct here — the run loop is going away — but the
        // budget is tight: macOS force-terminates a slow quit, which would
        // strand the server. Keep the total worst case near two seconds.
        terminate(serverProcess, grace: 1.0)
        serverProcess = nil
        for port in Set([activePort, cfg.port].compactMap { $0 }) {
            reapLlamaServers(onPort: port, grace: 1.0)
        }
    }

    // MARK: Menu

    /// Menu bar glyphs, loaded once — `updateTitle` runs on every 2s poll.
    ///
    /// NSImage decodes SVG directly, so there is no PNG pipeline and no @2x
    /// variants to keep in sync; it rasterises per display.
    private static func loadGlyph(_ name: String) -> NSImage? {
        guard let img = NSImage(contentsOf: resolveResourcesDir().appendingPathComponent(name))
        else {
            logLine("menu glyph missing: \(name)")
            return nil
        }
        // Template = AppKit owns the colour, so it adapts to light/dark menu
        // bars, tinting, and Reduce Transparency for free.
        img.isTemplate = true
        img.size = NSSize(width: 18, height: 18)
        return img
    }

    private lazy var glyphOff: NSImage? = Self.loadGlyph("menu-llama-off.svg")
    private lazy var glyphOn: NSImage? = Self.loadGlyph("menu-llama-on.svg")

    private func updateTitle() {
        statusItem.isVisible = true
        guard let btn = statusItem.button else { return }

        // Hollow vs solid carries the state without relying on colour; the tint
        // then says which kind of "on" it is.
        let glyph = state == .stopped ? glyphOff : glyphOn
        if btn.image !== glyph { btn.image = glyph }

        if glyph != nil {
            btn.imagePosition = .imageOnly
            btn.title = ""
        } else {
            // No asset — never show an empty status item.
            btn.imagePosition = .noImage
            btn.title = "Llama"
        }

        switch state {
        case .running: btn.contentTintColor = .systemGreen
        case .starting: btn.contentTintColor = .systemOrange
        case .error: btn.contentTintColor = .systemRed
        case .stopped: btn.contentTintColor = nil  // follows the menu bar
        }

        btn.toolTip = {
            switch state {
            case .running: return "Llama Menu — on · \(shortName(currentModel ?? ""))"
            case .starting: return "Llama Menu — starting \(shortName(currentModel ?? ""))…"
            case .error: return "Llama Menu — \(errorMessage.isEmpty ? "error" : errorMessage)"
            case .stopped: return "Llama Menu — off"
            }
        }()
    }

    // MARK: NSMenuDelegate

    /// Probe for an externally-started server when the user actually looks,
    /// rather than polling a mostly-idle port around the clock.
    func menuWillOpen(_ menu: NSMenu) {
        menuIsOpen = true
        if state == .stopped { adoptExistingServer() }
    }

    func menuDidClose(_ menu: NSMenu) {
        menuIsOpen = false
        if rebuildPending {
            rebuildPending = false
            rebuildMenu()
        }
    }

    private func rebuildMenu() {
        // Swapping the menu out from under an open menu is glitchy.
        guard !menuIsOpen else {
            rebuildPending = true
            return
        }
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
            add(menu, "Settings…", #selector(openSettings), key: ",")
            add(menu, "Show Log", #selector(openLog))
            add(menu, "Quit", #selector(quitApp), key: "q")

        case .starting:
            let name = shortName(currentModel ?? "model", limit: 28)
            addDisabled(menu, "…  Starting  ·  \(name)")
            menu.addItem(.separator())
            add(menu, "Stop", #selector(stopServerAction))
            menu.addItem(.separator())
            add(menu, "Settings…", #selector(openSettings), key: ",")
            add(menu, "Show Log", #selector(openLog))
            add(menu, "Quit", #selector(quitApp), key: "q")

        case .error:
            var err = errorMessage.isEmpty ? "Something went wrong" : errorMessage
            if err.count > 42 { err = String(err.prefix(39)) + "…" }
            addDisabled(menu, "!  \(err)")
            menu.addItem(.separator())
            addModelSubmenu(menu, title: "Start Model")
            menu.addItem(.separator())
            add(menu, "Settings…", #selector(openSettings), key: ",")
            add(menu, "Show Log", #selector(openLog))
            add(menu, "Quit", #selector(quitApp), key: "q")

        case .stopped:
            addDisabled(menu, "○  Off")
            if !FileManager.default.isExecutableFile(atPath: cfg.llama_server) {
                addDisabled(menu, "Install llama.cpp first")
            }
            menu.addItem(.separator())
            addModelSubmenu(menu, title: "Start Model")
            menu.addItem(.separator())
            add(menu, "Settings…", #selector(openSettings), key: ",")
            add(menu, "Show Log", #selector(openLog))
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
            let open = NSMenuItem(
                title: "Open \(cfg.models_dir)…",
                action: #selector(openModelsDir),
                keyEquivalent: ""
            )
            open.target = self
            sub.addItem(open)
        } else {
            var groups: [String: [String]] = [:]
            for (g, p) in models { groups[g, default: []].append(p) }
            let names = groups.keys.sorted {
                $0.localizedCaseInsensitiveCompare($1) == .orderedAscending
            }
            for g in names {
                let paths = (groups[g] ?? []).sorted {
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
        let active = (state == .running || state == .starting) && currentModel == path
        let eye = findMmproj(for: path) != nil ? " · 👁" : ""
        let prefix = active ? "✓  " : "    "
        let item = NSMenuItem(
            title: "\(prefix)\(shortName(path))\(eye)    \(fmtSize(modelSizeGB(path)))",
            action: #selector(startModel(_:)),
            keyEquivalent: ""
        )
        item.target = self
        item.representedObject = path
        item.isEnabled = true
        menu.addItem(item)
    }

    // MARK: Actions

    @objc private func startModel(_ sender: NSMenuItem) {
        guard let path = sender.representedObject as? String else { return }
        guard FileManager.default.isExecutableFile(atPath: cfg.llama_server) else {
            notify("Install llama.cpp first: brew install llama.cpp")
            return
        }
        guard FileManager.default.fileExists(atPath: path) else {
            state = .error
            errorMessage = "Missing: \(shortName(path))"
            rebuildMenu()
            return
        }

        // Reading GGUF metadata and probing memory both touch the disk, so they
        // stay off the main thread; only the panel itself comes back to it.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let info = readGGUFInfo(path: path)
            let rec = recommend(modelPath: path, cfg: self.cfg, info: info)
            DispatchQueue.main.async {
                self.presentPanel(model: path, recommendation: rec, info: info)
            }
        }
    }

    private func presentPanel(model: String, recommendation: Recommendation, info: GGUFInfo?) {
        launchPanel?.close()
        let panel = LaunchPanelController(
            modelPath: model,
            recommendation: recommendation,
            info: info,
            onLaunch: { [weak self] path, params in
                self?.startServer(model: path, params: params)
            },
            onCancel: { [weak self] in
                self?.launchPanel = nil
            }
        )
        launchPanel = panel
        if !panel.show() {
            launchPanel = nil
            // No UI available — fall back to the computed maximum.
            startServer(model: model, params: recommendation.params)
        }
    }

    @objc private func stopServerAction() {
        stopServer()
        notify("Stopped")
    }

    @objc private func openSettings() {
        if let existing = settingsPanel {
            existing.close()
            settingsPanel = nil
        }
        let panel = SettingsPanelController(
            readConfig: { [weak self] in self?.cfg ?? AppConfig() },
            writeConfig: { [weak self] updated in
                guard let self else { return }
                let portOrHostChanged = updated.port != self.cfg.port || updated.host != self.cfg.host
                self.cfg = updated
                saveConfig(updated)
                // Model list and binary warnings are drawn from config.
                self.rebuildMenu()
                if portOrHostChanged {
                    logLine("port/host changed to \(updated.host):\(updated.port)")
                }
            },
            isRunning: { [weak self] in self?.state == .running || self?.state == .starting },
            onClose: { [weak self] in self?.settingsPanel = nil }
        )
        settingsPanel = panel
        if !panel.show() { settingsPanel = nil }
    }

    @objc private func openChat() {
        guard state == .running,
              let url = URL(string: "http://127.0.0.1:\(activePort ?? cfg.port)/")
        else {
            notify("Start a model first — wait until it says Ready")
            return
        }
        NSWorkspace.shared.open(url)
    }

    @objc private func openLog() {
        ensureConfigDir()
        if !FileManager.default.fileExists(atPath: logPath.path) {
            try? Data().write(to: logPath)
        }
        NSWorkspace.shared.open(logPath)
    }

    @objc private func openModelsDir() {
        let dir = URL(fileURLWithPath: cfg.models_dir)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(dir)
    }

    @objc private func quitApp() {
        NSApp.terminate(nil)
    }

    // MARK: Server

    private func startServer(model: String, params: [String: Any]) {
        readyGeneration &+= 1
        let generation = readyGeneration

        currentModel = model
        state = .starting
        errorMessage = ""
        readyDeadline = Date().addingTimeInterval(180)
        rebuildMenu()
        notify("Starting \(shortName(model))\(params["mmproj"] != nil ? " + vision" : "")")

        // Capture on the main thread: reading serverProcess from the background
        // block would race the main thread writing it in spawn/stop.
        let existing = serverProcess
        serverProcess = nil
        let ports = Set([activePort, cfg.port].compactMap { $0 })

        // Freeing the port can take seconds; never do that on the main thread.
        lifecycle.async { [weak self] in
            terminate(existing, grace: 3.0)
            // Clear the old port too, in case Settings moved it while running.
            for port in ports { reapLlamaServers(onPort: port) }
            DispatchQueue.main.async {
                guard let self, generation == self.readyGeneration else { return }
                self.spawn(model: model, params: params, generation: generation)
            }
        }
    }

    private func spawn(model: String, params: [String: Any], generation: Int) {
        let binary = cfg.llama_server
        guard FileManager.default.isExecutableFile(atPath: binary) else {
            state = .error
            errorMessage = "llama-server not found"
            rebuildMenu()
            return
        }

        func int(_ k: String, _ d: Int) -> Int {
            (params[k] as? Int) ?? (params[k] as? NSNumber)?.intValue ?? d
        }
        func dbl(_ k: String, _ d: Double) -> Double {
            (params[k] as? Double) ?? (params[k] as? NSNumber)?.doubleValue ?? d
        }

        let ctx = min(max(int("ctx", 8192), 512), 1_048_576)
        let cacheK = (params["cache_type_k"] as? String).flatMap {
            isValidCacheType($0) ? $0 : nil
        } ?? cfg.kv_cache_type
        let cacheV = (params["cache_type_v"] as? String).flatMap {
            isValidCacheType($0) ? $0 : nil
        } ?? cfg.kv_cache_type

        var args = [
            "--model", model,
            "--host", cfg.host,
            "--port", "\(cfg.port)",
            "-ngl", "\(min(max(int("ngl", 999), 0), 999))",
            "-c", "\(ctx)",
            "-t", "\(min(max(int("threads", hardware.perfCores), 1), 128))",
            "-b", "\(min(max(int("batch", 512), 32), 8192))",
            "-np", "\(min(max(int("parallel", 1), 1), 4))",
            "-n", "\(int("n_predict", -1))",
            "-s", "\(int("seed", -1))",
            "--temp", String(format: "%.4g", dbl("temperature", 0.7)),
            "--top-p", String(format: "%.4g", dbl("top_p", 0.95)),
            "--top-k", "\(int("top_k", 40))",
            "--min-p", String(format: "%.4g", dbl("min_p", 0.05)),
            "--repeat-penalty", String(format: "%.4g", dbl("repeat_penalty", 1.1)),
            "--repeat-last-n", "\(int("repeat_last_n", 64))",
            "-ctk", cacheK,
            "-ctv", cacheV,
            "--jinja",
        ]
        // A quantized KV cache needs flash attention — V in particular is only
        // supported under FA. -fa defaults to 'auto'; be explicit when it matters.
        if isQuantizedCache(cacheK) || isQuantizedCache(cacheV) {
            args += ["-fa", "on"]
        }
        if let mm = params["mmproj"] as? String,
           FileManager.default.fileExists(atPath: mm) {
            args += ["--mmproj", mm]
        }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: binary)
        proc.arguments = args

        ensureConfigDir()
        rotateLogIfNeeded()
        if !FileManager.default.fileExists(atPath: logPath.path) {
            FileManager.default.createFile(atPath: logPath.path, contents: nil)
        }
        if let fh = try? FileHandle(forWritingTo: logPath) {
            fh.seekToEndOfFile()
            let header = "\n--- \(Date()) start \(shortName(model)) "
                + "ctx=\(ctx) kv=\(cacheK)/\(cacheV) ---\n"
            if let d = header.data(using: .utf8) { fh.write(d) }
            if let d = "cmd: \(binary) \(args.joined(separator: " "))\n".data(using: .utf8) {
                fh.write(d)
            }
            proc.standardOutput = fh
            proc.standardError = fh
        }

        do {
            try proc.run()
            serverProcess = proc
            // Remember where it actually landed; cfg.port can move under us.
            activePort = cfg.port
            state = .starting
            logLine("started pid=\(proc.processIdentifier) port=\(cfg.port) ctx=\(ctx) kv=\(cacheK)/\(cacheV)")
            rebuildMenu()
            scheduleReadyCheck(generation: generation)
        } catch {
            state = .error
            errorMessage = error.localizedDescription
            serverProcess = nil
            rebuildMenu()
            notify("Could not start: \(error.localizedDescription)")
        }
    }

    private func stopServer() {
        readyGeneration &+= 1
        state = .stopped
        currentModel = nil
        errorMessage = ""
        healthFailures = 0
        rebuildMenu()

        let proc = serverProcess
        serverProcess = nil
        // Follow the process to the port it is really on, not the configured one.
        let ports = Set([activePort, cfg.port].compactMap { $0 })
        activePort = nil
        lifecycle.async {
            terminate(proc, grace: 3.0)
            for port in ports { reapLlamaServers(onPort: port) }
        }
    }

    // MARK: Readiness

    private func scheduleReadyCheck(generation: Int) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.checkReady(generation: generation)
        }
    }

    private func checkReady(generation: Int) {
        guard generation == readyGeneration, state == .starting else { return }

        if let proc = serverProcess, !proc.isRunning {
            state = .error
            errorMessage = "Server exited (code \(proc.terminationStatus))"
            serverProcess = nil
            activePort = nil
            rebuildMenu()
            notify("Server exited — see Show Log")
            return
        }
        if Date() > readyDeadline {
            // stopServer() resets errorMessage, so the message has to be set
            // after it, not before, or the reason is lost.
            stopServer()
            state = .error
            errorMessage = "Timed out loading"
            rebuildMenu()
            notify("Timed out loading \(shortName(currentModel ?? "model"))")
            return
        }

        healthCheck { [weak self] ok in
            guard let self, generation == self.readyGeneration, self.state == .starting else { return }
            if ok {
                self.state = .running
                self.healthFailures = 0
                self.rebuildMenu()
                notify("Ready · \(shortName(self.currentModel ?? ""))")
            } else {
                self.scheduleReadyCheck(generation: generation)
            }
        }
    }

    /// Take ownership of a healthy server already listening on our port.
    ///
    /// The model name comes from the server's own /v1/models rather than from
    /// parsing `pgrep` output for `--model`, which split on whitespace and so
    /// truncated any path containing a space — and models live in user-named
    /// folders.
    private func adoptExistingServer() {
        let generation = readyGeneration
        healthCheck { [weak self] ok in
            guard let self, ok, generation == self.readyGeneration, self.state == .stopped else { return }
            self.fetchServedModel { id in
                guard generation == self.readyGeneration, self.state == .stopped else { return }
                self.state = .running
                self.currentModel = self.resolveModelPath(id)
                self.activePort = self.cfg.port
                self.healthFailures = 0
                self.rebuildMenu()
                logLine("adopted running server on port \(self.cfg.port) model=\(id ?? "unknown")")
            }
        }
    }

    /// Map what /v1/models reports back onto a real file.
    ///
    /// llama-server answers with a bare filename, but every comparison elsewhere
    /// — the ✓ in Switch Model, the vision marker — is against an absolute path,
    /// so an unresolved id silently matches nothing.
    private func resolveModelPath(_ id: String?) -> String? {
        guard let id, !id.isEmpty else { return nil }
        if FileManager.default.fileExists(atPath: id) { return id }
        let wanted = (id as NSString).lastPathComponent
        let models = scanModels(dir: cfg.models_dir)
        return models.first { ($0.path as NSString).lastPathComponent == wanted }?.path
            ?? models.first { shortName($0.path) == shortName(wanted) }?.path
            ?? id
    }

    private func fetchServedModel(_ completion: @escaping (String?) -> Void) {
        guard let url = URL(string: "http://127.0.0.1:\(cfg.port)/v1/models") else {
            return completion(nil)
        }
        var req = URLRequest(url: url, timeoutInterval: 1.5)
        req.httpMethod = "GET"
        URLSession.shared.dataTask(with: req) { data, _, _ in
            var id: String?
            if let data,
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let entries = obj["data"] as? [[String: Any]] {
                id = entries.first?["id"] as? String
            }
            DispatchQueue.main.async { completion(id) }
        }.resume()
    }

    /// Non-blocking: a semaphore here would stall the caller's thread.
    private func healthCheck(_ completion: @escaping (Bool) -> Void) {
        guard let url = URL(string: "http://127.0.0.1:\(activePort ?? cfg.port)/health") else {
            return completion(false)
        }
        var req = URLRequest(url: url, timeoutInterval: 1.5)
        req.httpMethod = "GET"
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            DispatchQueue.main.async { completion((200..<300).contains(code)) }
        }.resume()
    }

    // MARK: Poll

    private func poll() {
        statusItem.isVisible = true

        if state == .running {
            if let proc = serverProcess, !proc.isRunning {
                serverProcess = nil
                activePort = nil
                state = .stopped
                currentModel = nil
                healthFailures = 0
                rebuildMenu()
                return
            }
            // A live process is not the same as a live server — keep probing so
            // a wedged server stops reporting "On".
            let generation = readyGeneration
            healthCheck { [weak self] ok in
                guard let self, generation == self.readyGeneration, self.state == .running else { return }
                if ok {
                    self.healthFailures = 0
                } else {
                    self.healthFailures += 1
                    if self.healthFailures >= 3 {
                        self.state = .error
                        self.errorMessage = "Server stopped responding"
                        self.rebuildMenu()
                    }
                }
            }
        }
        updateTitle()
    }
}
