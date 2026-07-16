import Foundation

// MARK: - Identity & paths

let appName = "Llama Menu"
let bundleId = "com.llamamenu.app"
let defaultPort = 8180
let defaultHost = "127.0.0.1"

/// Overridable so tests and dev runs don't write into the real config, whose
/// log and prefs belong to whatever copy of the app the user is actually using.
let configDir: URL = {
    let env = ProcessInfo.processInfo.environment["LLAMA_MENU_CONFIG_DIR"]
    if let env, !env.isEmpty {
        return URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
    }
    return FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".config/llama-menu")
}()
let configPath = configDir.appendingPathComponent("config.json")
let prefsPath = configDir.appendingPathComponent("model_prefs.json")
let logDir = configDir.appendingPathComponent("logs")
let logPath = logDir.appendingPathComponent("server.log")
let modelsDefault = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("models")

/// Server log is rotated past this size, but only ever at server start — never
/// while llama-server holds the descriptor, or it would keep writing to the
/// renamed inode and the new file would stay empty.
let maxLogBytes: UInt64 = 8 * 1024 * 1024

func ensureConfigDir() {
    try? FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
}

/// Resources live beside the executable in the bundle; fall back to the repo
/// layout so the app can be run straight from a build directory.
func resolveResourcesDir() -> URL {
    // Explicit override wins — used when running against a working copy.
    if let env = ProcessInfo.processInfo.environment["LLAMA_MENU_RESOURCES"], !env.isEmpty {
        let dir = URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
        if FileManager.default.fileExists(atPath: dir.appendingPathComponent("launch.html").path) {
            return dir
        }
    }
    if let r = Bundle.main.resourceURL,
       FileManager.default.fileExists(atPath: r.appendingPathComponent("launch.html").path) {
        return r
    }
    let exe = Bundle.main.executableURL ?? URL(fileURLWithPath: CommandLine.arguments[0])
    let contents = exe.deletingLastPathComponent().deletingLastPathComponent()
    let res = contents.appendingPathComponent("Resources")
    if FileManager.default.fileExists(atPath: res.appendingPathComponent("launch.html").path) {
        return res
    }
    return URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .appendingPathComponent("resources")
}

func appVersion() -> String {
    if let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String, !v.isEmpty {
        return v
    }
    // Dev fallback: VERSION sits in Resources when bundled, repo root otherwise.
    let resources = resolveResourcesDir()
    for file in [
        resources.appendingPathComponent("VERSION"),
        resources.deletingLastPathComponent().appendingPathComponent("VERSION"),
    ] {
        if let v = try? String(contentsOf: file, encoding: .utf8) {
            let trimmed = v.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { return trimmed }
        }
    }
    return "0.0.0"
}

/// Shorten a path for display: home becomes `~`, long paths truncate in the
/// middle. Done here rather than in CSS — `direction: rtl` reorders the leading
/// slash to the end and renders `/Users/x` as `Users/x/`.
func abbreviatePath(_ path: String, limit: Int = 34) -> String {
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    var p = path
    if p == home {
        p = "~"
    } else if p.hasPrefix(home + "/") {
        p = "~" + p.dropFirst(home.count)
    }
    guard p.count > limit else { return p }
    let keepTail = limit / 2 - 1
    let keepHead = limit - keepTail - 1
    return p.prefix(keepHead) + "…" + p.suffix(keepTail)
}

// MARK: - Logging

private let logQueue = DispatchQueue(label: "com.llamamenu.log")

func rotateLogIfNeeded() {
    let fm = FileManager.default
    guard let attrs = try? fm.attributesOfItem(atPath: logPath.path),
          let size = (attrs[.size] as? NSNumber)?.uint64Value,
          size > maxLogBytes
    else { return }
    let archived = logDir.appendingPathComponent("server.log.1")
    try? fm.removeItem(at: archived)
    try? fm.moveItem(at: logPath, to: archived)
}

private let logStamp: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd HH:mm:ss"
    return f
}()

func logLine(_ s: String) {
    logQueue.async {
        ensureConfigDir()
        let line = "\(logStamp.string(from: Date())) \(s)\n"
        guard let data = line.data(using: .utf8) else { return }
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

// MARK: - Subprocess

/// Run a tool and capture stdout. Reads the pipe before waiting so a chatty
/// child can't deadlock us by filling the pipe buffer.
@discardableResult
func runTool(_ path: String, _ args: [String]) -> String {
    guard FileManager.default.isExecutableFile(atPath: path) else { return "" }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: path)
    p.arguments = args
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = FileHandle.nullDevice
    do { try p.run() } catch { return "" }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    return String(data: data, encoding: .utf8) ?? ""
}

// MARK: - Notifications

/// Quote a string for embedding in an AppleScript literal. Model names are
/// filenames, so they can legitimately contain quotes and backslashes.
func applescriptQuoted(_ s: String) -> String {
    let escaped = s
        .replacingOccurrences(of: "\\", with: "\\\\")
        .replacingOccurrences(of: "\"", with: "\\\"")
    return "\"\(escaped)\""
}

func notify(_ message: String, title: String = appName) {
    let script = "display notification \(applescriptQuoted(message)) with title \(applescriptQuoted(title))"
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    p.arguments = ["-e", script]
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    try? p.run()
}

// MARK: - Models

func discoverLlamaServer() -> String? {
    var candidates = [
        "/opt/homebrew/bin/llama-server",
        "/usr/local/bin/llama-server",
    ]
    candidates.append(
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("llama.cpp/build/bin/llama-server").path
    )
    for c in candidates where FileManager.default.isExecutableFile(atPath: c) {
        return c
    }
    return nil
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

func fmtSize(_ gb: Double) -> String {
    if gb >= 10 { return String(format: "%.0f GB", gb) }
    if gb >= 1 { return String(format: "%.1f GB", gb) }
    return String(format: "%.0f MB", gb * 1024)
}

/// Find a multimodal projector sitting next to a model, preferring full precision.
func findMmproj(for modelPath: String) -> String? {
    let dir = (modelPath as NSString).deletingLastPathComponent
    let fm = FileManager.default
    guard let files = try? fm.contentsOfDirectory(atPath: dir) else { return nil }
    let modelName = (modelPath as NSString).lastPathComponent
    let hits = files.filter {
        let l = $0.lowercased()
        return l.hasSuffix(".gguf")
            && (l.contains("mmproj") || l.contains("projector"))
            && $0 != modelName
    }
    let sorted = hits.sorted { a, b in
        let ascore = isFullPrecision(a) ? 0 : 1
        let bscore = isFullPrecision(b) ? 0 : 1
        if ascore != bscore { return ascore < bscore }
        return a.localizedCaseInsensitiveCompare(b) == .orderedAscending
    }
    return sorted.first.map { (dir as NSString).appendingPathComponent($0) }
}

private func isFullPrecision(_ name: String) -> Bool {
    let l = name.lowercased()
    return l.contains("f32") || l.contains("f16") || l.contains("fp16") || l.contains("fp32")
}

func scanModels(dir: String) -> [(group: String, path: String)] {
    let root = (dir as NSString).expandingTildeInPath
    var results: [(String, String)] = []
    let fm = FileManager.default
    guard let enumerator = fm.enumerator(atPath: root) else { return [] }
    while let rel = enumerator.nextObject() as? String {
        let l = rel.lowercased()
        guard l.hasSuffix(".gguf"), !l.contains("mmproj"), !l.contains("projector") else { continue }
        let full = (root as NSString).appendingPathComponent(rel)
        let parts = rel.split(separator: "/")
        let group = parts.count > 1 ? String(parts[0]) : "__root__"
        results.append((group, full))
    }
    return results.sorted { $0.1.localizedCaseInsensitiveCompare($1.1) == .orderedAscending }
}
