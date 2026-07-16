import Foundation

/// Persisted app config.
///
/// Decoding is deliberately hand-written: Swift's synthesized `init(from:)`
/// calls `decode` rather than `decodeIfPresent`, so a property default is NOT
/// used as a fallback for a missing key — it throws instead. Combined with a
/// `try?` at the call site that silently reset every setting whenever a single
/// key was absent. `decodeIfPresent` makes each key independently optional.
struct AppConfig: Codable, Equatable {
    var llama_server: String = ""
    var models_dir: String = modelsDefault.path
    var host: String = defaultHost
    var port: Int = defaultPort
    var ngl: Int = 999
    var batch: Int = 512
    var threads: Int = 0
    var stop_server_on_quit: Bool = true
    var has_seen_welcome: Bool = false
    /// Default KV cache precision for new models; per-model prefs override it.
    var kv_cache_type: String = "f16"

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = AppConfig()
        llama_server = try c.decodeIfPresent(String.self, forKey: .llama_server) ?? d.llama_server
        models_dir = try c.decodeIfPresent(String.self, forKey: .models_dir) ?? d.models_dir
        host = try c.decodeIfPresent(String.self, forKey: .host) ?? d.host
        port = try c.decodeIfPresent(Int.self, forKey: .port) ?? d.port
        ngl = try c.decodeIfPresent(Int.self, forKey: .ngl) ?? d.ngl
        batch = try c.decodeIfPresent(Int.self, forKey: .batch) ?? d.batch
        threads = try c.decodeIfPresent(Int.self, forKey: .threads) ?? d.threads
        stop_server_on_quit = try c.decodeIfPresent(Bool.self, forKey: .stop_server_on_quit)
            ?? d.stop_server_on_quit
        has_seen_welcome = try c.decodeIfPresent(Bool.self, forKey: .has_seen_welcome)
            ?? d.has_seen_welcome
        kv_cache_type = try c.decodeIfPresent(String.self, forKey: .kv_cache_type) ?? d.kv_cache_type
    }
}

func loadConfig() -> AppConfig {
    ensureConfigDir()
    var cfg = AppConfig()
    if let data = try? Data(contentsOf: configPath) {
        do {
            cfg = try JSONDecoder().decode(AppConfig.self, from: data)
        } catch {
            logLine("config unreadable (\(error)) — using defaults")
        }
    }
    if cfg.llama_server.isEmpty {
        cfg.llama_server = discoverLlamaServer() ?? ""
    }
    if cfg.threads <= 0 {
        cfg.threads = hardware.perfCores
    }
    if !(1024...65535).contains(cfg.port) {
        cfg.port = defaultPort
    }
    // 8080 is Docker Desktop's turf and the IPv4/IPv6 split confuses browsers.
    if cfg.port == 8080 { cfg.port = defaultPort }
    if !isValidCacheType(cfg.kv_cache_type) { cfg.kv_cache_type = "f16" }
    // Only two bind addresses are meaningful, and one of them is a network
    // exposure decision — don't honour anything else from the file.
    if cfg.host != "0.0.0.0" { cfg.host = defaultHost }
    cfg.models_dir = (cfg.models_dir as NSString).expandingTildeInPath
    saveConfig(cfg)
    return cfg
}

func saveConfig(_ cfg: AppConfig) {
    ensureConfigDir()
    let enc = JSONEncoder()
    enc.outputFormatting = [.prettyPrinted, .sortedKeys]
    if let data = try? enc.encode(cfg) {
        try? data.write(to: configPath)
    }
}

// MARK: - Per-model preferences

func loadAllPrefs() -> [String: [String: Any]] {
    guard let data = try? Data(contentsOf: prefsPath),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: [String: Any]]
    else { return [:] }
    return obj
}

func prefsForModel(_ path: String) -> [String: Any] {
    loadAllPrefs()[path] ?? [:]
}

func savePrefsForModel(_ path: String, params: [String: Any]) {
    var all = loadAllPrefs()
    var keep: [String: Any] = [:]
    for k in prefKeys where params[k] != nil {
        keep[k] = params[k]
    }
    all[path] = keep
    ensureConfigDir()
    guard let data = try? JSONSerialization.data(
        withJSONObject: all,
        options: [.prettyPrinted, .sortedKeys]
    ) else { return }
    try? data.write(to: prefsPath)
}

let prefKeys = [
    "ctx", "ngl", "threads", "batch", "n_predict", "seed",
    "temperature", "top_p", "top_k", "min_p",
    "repeat_penalty", "repeat_last_n",
    "cache_type_k", "cache_type_v",
]
