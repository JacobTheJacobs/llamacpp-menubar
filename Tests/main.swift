import Foundation

// Hermetic checks for the pure logic: no model files, no network, no UI.
// Run with ./scripts/test.sh

setvbuf(stdout, nil, _IONBF, 0)

var failures = 0

func check(_ label: String, _ got: Any?, _ want: String) {
    let g = "\(got ?? "nil")"
    if g == want {
        print("PASS  \(label)")
    } else {
        failures += 1
        print("FAIL  \(label): got=\(g) want=\(want)")
    }
}

func checkClose(_ label: String, _ got: Double, _ want: Double, tolerance: Double = 0.001) {
    if abs(got - want) <= tolerance {
        print("PASS  \(label)")
    } else {
        failures += 1
        print("FAIL  \(label): got=\(got) want≈\(want)")
    }
}

// MARK: - Cache type accounting
//
// Block layouts from ggml: q8_0 is a 2-byte scale + 32 int8 = 34 bytes per 32
// values; q4_0 is a 2-byte scale + 16 packed bytes = 18 per 32.

check("f16 bytes/elem", cacheType("f16")!.bytesPerElement, "2.0")
check("q8_0 bytes/elem", cacheType("q8_0")!.bytesPerElement, "1.0625")
check("q5_1 bytes/elem", cacheType("q5_1")!.bytesPerElement, "0.75")
check("q4_0 bytes/elem", cacheType("q4_0")!.bytesPerElement, "0.5625")
check("f16 is not quantized", isQuantizedCache("f16"), "false")
check("q8_0 is quantized", isQuantizedCache("q8_0"), "true")
check("unknown cache type rejected", isValidCacheType("q3_k"), "false")
check("injection-shaped type rejected", isValidCacheType("q4_0; rm -rf /"), "false")

// MARK: - KV cache sizing
//
// Ground truth captured from llama-server's own reporting for
// UI-Venus-1.5-8B-Q4_K_M (qwen3vl: 36 layers, 8 KV heads, head dim 128):
//   -c 8192 -ctk f16  -> llama_kv_cache: size = 1152.00 MiB
//   -c 8192 -ctk q8_0 -> llama_kv_cache: size =  612.00 MiB

var dense = GGUFInfo()
dense.blockCount = 36
dense.headCount = 32
dense.headCountKV = 8
dense.embeddingLength = 4096
dense.keyLength = 128
dense.valueLength = 128
dense.contextLength = 262_144

check("dense kvHeadUnits", dense.kvHeadUnits, "288")
check("dense can size KV", dense.canSizeKV, "true")
checkClose(
    "dense KV @8192 f16 == 1152 MiB",
    kvCacheGB(info: dense, modelGB: 4.68, ctx: 8192, cacheK: "f16", cacheV: "f16"),
    1152.0 / 1024.0
)
checkClose(
    "dense KV @8192 q8_0 == 612 MiB",
    kvCacheGB(info: dense, modelGB: 4.68, ctx: 8192, cacheK: "q8_0", cacheV: "q8_0"),
    612.0 / 1024.0
)

// Hybrid: DictaLM-3.0-Nemotron-12B (nemotron_h) writes head_count_kv as one
// entry per layer — only 6 of 62 layers hold KV. Assuming all 62 layers cache
// (the pre-GGUF behaviour) overestimated the cache by ~52x.
var hybrid = GGUFInfo()
hybrid.blockCount = 62
hybrid.headCount = 40
hybrid.embeddingLength = 5120
hybrid.keyLength = 128
hybrid.valueLength = 128
hybrid.contextLength = 1_048_576
hybrid.headCountKVPerLayer = (0..<62).map { [7, 16, 25, 34, 43, 52].contains($0) ? 8 : 0 }

check("hybrid kvHeadUnits sums per-layer", hybrid.kvHeadUnits, "48")
check("hybrid attention layer count", hybrid.attentionLayers, "6")
checkClose(
    "hybrid KV @8192 f16 == 192 MiB",
    kvCacheGB(info: hybrid, modelGB: 6.98, ctx: 8192, cacheK: "f16", cacheV: "f16"),
    192.0 / 1024.0
)
// Scaling must stay linear — the panel's live meter depends on it.
checkClose(
    "hybrid KV is linear in ctx",
    kvCacheGB(info: hybrid, modelGB: 6.98, ctx: 65536, cacheK: "f16", cacheV: "f16"),
    8 * kvCacheGB(info: hybrid, modelGB: 6.98, ctx: 8192, cacheK: "f16", cacheV: "f16")
)

// No metadata: fall back to the heuristic rather than claiming 0.
let fallback = kvCacheGB(info: nil, modelGB: 7.0, ctx: 8192, cacheK: "f16", cacheV: "f16")
check("fallback heuristic is positive", fallback > 0, "true")

// MARK: - Sanitizing
//
// The bridge is local and inert, but these values become process arguments.

var rec = Recommendation(
    ctx: 32768, ngl: 999, threads: 8, batch: 512,
    cacheTypeK: "f16", cacheTypeV: "f16",
    temperature: 0.7, topP: 0.95, topK: 40, minP: 0.05,
    repeatPenalty: 1.1, repeatLastN: 64, nPredict: -1, seed: -1, parallel: 1,
    modelGB: 4.68, mmproj: "/models/mmproj-F32.gguf", trainedCtx: 262_144,
    tiers: [4096, 8192, 16384, 32768, 65536, 131_072, 262_144],
    budgetGB: 12.5, availableGB: 10, totalRamGB: 32, chip: "Apple M1 Pro", reason: ""
)

let hostile: [String: Any] = [
    "ctx": 999_999_999,
    "threads": 9999,
    "ngl": -5,
    "batch": 1_000_000,
    "temperature": 99.0,
    "top_p": 5.0,
    "top_k": -3,
    "min_p": 7.0,
    "repeat_penalty": 50.0,
    "parallel": 99,
    "cache_type_k": "q4_0; rm -rf /",
    "cache_type_v": "../../etc/passwd",
    "mmproj": "/tmp/attacker.gguf",
]
let s = sanitizeParams(hostile, recommended: rec)
check("ctx clamped to trained ctx", s["ctx"], "262144")
check("threads clamped", s["threads"], "128")
check("ngl floored at 0", s["ngl"], "0")
check("batch capped", s["batch"], "8192")
check("temperature capped", s["temperature"], "2.0")
check("top_p capped", s["top_p"], "1.0")
check("top_k floored", s["top_k"], "0")
check("min_p capped", s["min_p"], "1.0")
check("repeat_penalty capped", s["repeat_penalty"], "2.0")
check("parallel capped", s["parallel"], "4")
check("bad cache_type_k falls back", s["cache_type_k"], "f16")
check("bad cache_type_v falls back", s["cache_type_v"], "f16")
check("mmproj comes from disk, not the page", s["mmproj"], "/models/mmproj-F32.gguf")

let good: [String: Any] = [
    "ctx": 8192, "threads": 6, "cache_type_k": "q4_0", "cache_type_v": "q4_0",
    "temperature": 0.4, "top_k": 20,
]
let g = sanitizeParams(good, recommended: rec)
check("valid ctx preserved", g["ctx"], "8192")
check("valid threads preserved", g["threads"], "6")
check("valid cache type preserved", g["cache_type_k"], "q4_0")
check("valid temperature preserved", g["temperature"], "0.4")
check("valid top_k preserved", g["top_k"], "20")

// A model with no trained-ctx metadata must still get a sane ceiling.
rec.trainedCtx = 0
check("ctx ceiling without metadata", sanitizeParams(["ctx": 999_999_999], recommended: rec)["ctx"], "131072")

// MARK: - Config decoding
//
// Swift's synthesized Codable throws on any missing key, which previously reset
// every setting to defaults. Each key must be independently optional.

let partial = #"{"port": 9000}"#.data(using: .utf8)!
if let cfg = try? JSONDecoder().decode(AppConfig.self, from: partial) {
    check("partial config keeps provided key", cfg.port, "9000")
    check("partial config defaults missing key", cfg.stop_server_on_quit, "true")
    check("partial config defaults kv type", cfg.kv_cache_type, "f16")
} else {
    failures += 1
    print("FAIL  partial config decode threw")
}

let unknown = #"{"port": 9000, "future_setting": 42}"#.data(using: .utf8)!
check(
    "unknown keys tolerated",
    (try? JSONDecoder().decode(AppConfig.self, from: unknown))?.port,
    "9000"
)

// MARK: - Formatting

check("fmtCtx power of two", fmtCtx(131_072), "128K")
check("fmtCtx non-round", fmtCtx(40960), "40K")
check("fmtSize large", fmtSize(15.4), "15 GB")
check("fmtSize small", fmtSize(0.5), "512 MB")
check("shortName strips extension", shortName("/m/Qwen3.gguf"), "Qwen3")

// Paths are abbreviated host-side. CSS `direction: rtl` used to do this and
// reordered the leading slash to the end, rendering /Users/x as "Users/x/".
let home = FileManager.default.homeDirectoryForCurrentUser.path
check("home becomes tilde", abbreviatePath(home + "/models"), "~/models")
check("home alone becomes tilde", abbreviatePath(home), "~")
check("absolute path keeps leading slash", abbreviatePath("/opt/homebrew/bin/x"), "/opt/homebrew/bin/x")
check("long path truncates in the middle", abbreviatePath(String(repeating: "a", count: 60), limit: 10).count, "10")
check(
    "long path keeps both ends",
    abbreviatePath("/opt/homebrew/verylongdirectory/name/llama-server", limit: 20).hasPrefix("/opt"),
    "true"
)

print("")
if failures == 0 {
    print("ALL PASS")
    exit(0)
} else {
    print("\(failures) FAILURE(S)")
    exit(1)
}
