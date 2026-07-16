import Foundation

// MARK: - KV cache types

/// Bytes per stored element for each KV cache type llama-server accepts.
/// Quantized types are block-encoded: a block holds 32 values plus scale
/// metadata, e.g. q8_0 is 2 bytes scale + 32 int8 = 34 bytes per 32 values.
struct CacheType {
    let id: String
    let label: String
    let bytesPerElement: Double
    let quantized: Bool
}

let cacheTypes: [CacheType] = [
    CacheType(id: "f32", label: "f32", bytesPerElement: 4.0, quantized: false),
    CacheType(id: "f16", label: "f16", bytesPerElement: 2.0, quantized: false),
    CacheType(id: "bf16", label: "bf16", bytesPerElement: 2.0, quantized: false),
    CacheType(id: "q8_0", label: "q8_0", bytesPerElement: 34.0 / 32.0, quantized: true),
    CacheType(id: "q5_1", label: "q5_1", bytesPerElement: 24.0 / 32.0, quantized: true),
    CacheType(id: "q5_0", label: "q5_0", bytesPerElement: 22.0 / 32.0, quantized: true),
    CacheType(id: "q4_1", label: "q4_1", bytesPerElement: 20.0 / 32.0, quantized: true),
    CacheType(id: "q4_0", label: "q4_0", bytesPerElement: 18.0 / 32.0, quantized: true),
    CacheType(id: "iq4_nl", label: "iq4_nl", bytesPerElement: 18.0 / 32.0, quantized: true),
]

/// The ladder we surface in the panel — full precision down to 4-bit.
let uiCacheTypes = ["f16", "q8_0", "q5_1", "q4_0"]

func cacheType(_ id: String) -> CacheType? {
    cacheTypes.first { $0.id == id }
}

func isValidCacheType(_ id: String) -> Bool {
    cacheType(id) != nil
}

func isQuantizedCache(_ id: String) -> Bool {
    cacheType(id)?.quantized ?? false
}

// MARK: - Memory model

let ctxTiers = [4096, 8192, 16384, 32768, 65536, 131072]

/// KV cache size in GB.
///
/// With GGUF metadata this is exact: K and V are stored per attention layer,
/// `kvHeads * headDim` elements per token each. Two details the old size-based
/// guess got badly wrong, both verified against llama-server's own reporting:
///
///  - Grouped-query models store far fewer KV heads than attention heads.
///  - Hybrid models (nemotron_h, jamba …) are mostly Mamba layers holding no
///    KV at all, so `kvHeadUnits` sums per-layer heads rather than assuming
///    every layer caches.
///
/// Falls back to the previous heuristic when metadata is unreadable.
func kvCacheGB(
    info: GGUFInfo?,
    modelGB: Double,
    ctx: Int,
    cacheK: String,
    cacheV: String
) -> Double {
    let kb = cacheType(cacheK)?.bytesPerElement ?? 2.0
    let vb = cacheType(cacheV)?.bytesPerElement ?? 2.0

    if let i = info, i.canSizeKV {
        let perToken = Double(i.kvHeadUnits)
            * (Double(i.headDimK) * kb + Double(i.headDimV) * vb)
        return perToken * Double(ctx) / 1_073_741_824.0
    }

    let per1k = 0.055 * max(1.0, modelGB / 3.5)
    let f16Baseline = 4.0  // 2 bytes K + 2 bytes V
    return per1k * (Double(ctx) / 1000.0) * ((kb + vb) / f16Baseline)
}

/// Graph and batch buffers sitting alongside the weights and KV cache.
///
/// Checked against llama-server's own memory breakdown: a 4.7 GB dense model
/// reports ~361 MiB of compute buffers against the ~550 MiB this allows, and a
/// 7.0 GB hybrid reports ~292 MiB of compute plus ~144 MiB of recurrent Mamba
/// state against ~645 MiB. The slack deliberately absorbs recurrent state —
/// sizing that exactly would need a Mamba layer count that GGUF metadata does
/// not expose (block_count counts MLP blocks too, so deriving it from
/// non-attention layers overestimates it roughly twofold).
func computeOverheadGB(modelGB: Double) -> Double {
    0.35 + modelGB * 0.04
}

// MARK: - Recommendation

struct Recommendation {
    var ctx: Int
    var ngl: Int
    var threads: Int
    var batch: Int
    var cacheTypeK: String
    var cacheTypeV: String
    var temperature: Double
    var topP: Double
    var topK: Int
    var minP: Double
    var repeatPenalty: Double
    var repeatLastN: Int
    var nPredict: Int
    var seed: Int
    var parallel: Int

    var modelGB: Double
    var mmproj: String?
    var trainedCtx: Int
    var tiers: [Int]
    var budgetGB: Double
    var availableGB: Double
    var totalRamGB: Double
    var chip: String
    var reason: String

    var params: [String: Any] {
        var p: [String: Any] = [
            "ctx": ctx,
            "ngl": ngl,
            "threads": threads,
            "batch": batch,
            "cache_type_k": cacheTypeK,
            "cache_type_v": cacheTypeV,
            "n_predict": nPredict,
            "seed": seed,
            "temperature": temperature,
            "top_p": topP,
            "top_k": topK,
            "min_p": minP,
            "repeat_penalty": repeatPenalty,
            "repeat_last_n": repeatLastN,
            "parallel": parallel,
        ]
        if let mm = mmproj { p["mmproj"] = mm }
        return p
    }
}

/// Pick the best launch profile this Mac can actually sustain for this model.
func recommend(modelPath: String, cfg: AppConfig, info: GGUFInfo?) -> Recommendation {
    let hw = detectHardware()
    let size = modelSizeGB(modelPath)
    let mmproj = findMmproj(for: modelPath)
    let mmprojGB = mmproj.map { modelSizeGB($0) } ?? 0

    let total = hw.totalRamGB
    let available = hw.availableRamGB
    let osReserve = total <= 16 ? 2.0 : (total <= 32 ? 2.5 : 3.0)

    // Memory left for KV + graph once the weights are resident.
    let afterWeights = max(0.5, total - size - mmprojGB - osReserve)
    // Don't bank on more than what is actually free right now...
    let liveBudget = max(0.5, available - 1.25)
    // ...but a machine with lots of RAM and a busy desktop shouldn't be punished
    // into a tiny context either; the user can quit apps. Blend toward the max.
    var budget = min(afterWeights, liveBudget)
    budget = max(budget, afterWeights * 0.55)
    budget = min(budget, afterWeights)

    let overhead = computeOverheadGB(modelGB: size)

    // Never offer more context than the model was trained for.
    let trained = info?.contextLength ?? 0
    var tiers: [Int]
    if trained > 0 {
        tiers = ctxTiers.filter { $0 <= trained }
        if !tiers.contains(trained), trained >= 2048 { tiers.append(trained) }
        if tiers.isEmpty { tiers = [max(512, trained)] }
    } else {
        tiers = ctxTiers
    }
    tiers.sort()

    func fits(_ ctx: Int, _ type: String) -> Bool {
        kvCacheGB(info: info, modelGB: size, ctx: ctx, cacheK: type, cacheV: type) + overhead
            <= budget * 0.92
    }

    func largestFitting(_ type: String) -> Int {
        for t in tiers.reversed() where fits(t, type) { return t }
        return tiers.first ?? 4096
    }

    // Prefer full precision. Only reach for q8_0 — which is close to lossless —
    // when it buys a genuinely larger context than f16 can hold.
    let maxTier = tiers.last ?? 4096
    var ctx = largestFitting("f16")
    var cache = "f16"
    if ctx < maxTier {
        let q8 = largestFitting("q8_0")
        if q8 > ctx {
            ctx = q8
            cache = "q8_0"
        }
    }

    let threads = max(1, cfg.threads > 0 ? cfg.threads : hw.perfCores)
    let ngl = (hw.appleSilicon || hw.gpuCores > 0) ? 999 : max(0, cfg.ngl)

    let kv = kvCacheGB(info: info, modelGB: size, ctx: ctx, cacheK: cache, cacheV: cache)
    let leftover = max(0, budget - kv - overhead)
    var batch: Int
    if leftover >= 6 { batch = 1024 } else if leftover >= 3 { batch = 512 }
    else if leftover >= 1.5 { batch = 256 } else { batch = 128 }
    if cfg.batch > 0 { batch = min(max(batch, 128), max(cfg.batch, batch)) }

    // Deliberately terse: the chips already show every value and the meter
    // already shows the memory. Only say what those cannot.
    var notes: [String] = []
    if ctx >= maxTier, trained > 0, ctx >= trained {
        notes.append("Full \(fmtCtx(trained)) context — the most this model was trained for.")
    } else {
        notes.append("Largest context that fits this Mac.")
    }
    if cache != "f16" {
        let pct = (1 - (cacheType(cache)?.bytesPerElement ?? 2) / 2) * 100
        notes.append(
            "\(cache) KV is ~\(String(format: "%.0f", pct))% smaller than f16 — that is what makes it fit."
        )
    }
    let reason = notes.joined(separator: " ")

    return Recommendation(
        ctx: ctx,
        ngl: ngl,
        threads: threads,
        batch: batch,
        cacheTypeK: cache,
        cacheTypeV: cache,
        temperature: size >= 20 ? 0.65 : 0.7,
        topP: 0.95,
        topK: 40,
        minP: 0.05,
        repeatPenalty: 1.1,
        repeatLastN: 64,
        nPredict: -1,
        seed: -1,
        parallel: 1,
        modelGB: size,
        mmproj: mmproj,
        trainedCtx: trained,
        tiers: tiers,
        budgetGB: budget,
        availableGB: available,
        totalRamGB: total,
        chip: hw.chip,
        reason: reason
    )
}

func fmtCtx(_ n: Int) -> String {
    if n >= 1024 && n % 1024 == 0 { return "\(n / 1024)K" }
    if n >= 1024 { return String(format: "%.1fK", Double(n) / 1024.0) }
    return "\(n)"
}

// MARK: - Sanitizing

/// Clamp everything arriving from the panel. The web view is inert and local,
/// but these values become process arguments, so they get validated anyway.
func sanitizeParams(_ data: [String: Any], recommended: Recommendation) -> [String: Any] {
    func int(_ key: String, _ def: Int) -> Int {
        if let i = data[key] as? Int { return i }
        if let n = data[key] as? NSNumber { return n.intValue }
        if let d = data[key] as? Double, d.isFinite { return Int(d) }
        if let s = data[key] as? String, let i = Int(s) { return i }
        return def
    }
    func dbl(_ key: String, _ def: Double) -> Double {
        if let d = data[key] as? Double, d.isFinite { return d }
        if let n = data[key] as? NSNumber { return n.doubleValue }
        if let s = data[key] as? String, let d = Double(s), d.isFinite { return d }
        return def
    }
    func cache(_ key: String, _ def: String) -> String {
        guard let s = data[key] as? String, isValidCacheType(s) else { return def }
        return s
    }

    // Hard ceiling: the model's trained context, never a UI-supplied number.
    let ctxCeiling = recommended.trainedCtx > 0
        ? recommended.trainedCtx
        : (ctxTiers.last ?? 131072)

    var p: [String: Any] = [
        "ctx": min(max(int("ctx", recommended.ctx), 512), ctxCeiling),
        "ngl": min(max(int("ngl", recommended.ngl), 0), 999),
        "threads": min(max(int("threads", recommended.threads), 1), 128),
        "batch": min(max(int("batch", recommended.batch), 32), 8192),
        "n_predict": max(int("n_predict", -1), -1),
        "seed": int("seed", -1),
        "temperature": min(max(dbl("temperature", 0.7), 0.0), 2.0),
        "top_p": min(max(dbl("top_p", 0.95), 0.0), 1.0),
        "top_k": min(max(int("top_k", 40), 0), 200),
        "min_p": min(max(dbl("min_p", 0.05), 0.0), 1.0),
        "repeat_penalty": min(max(dbl("repeat_penalty", 1.1), 0.5), 2.0),
        "repeat_last_n": min(max(int("repeat_last_n", 64), -1), 8192),
        "parallel": min(max(int("parallel", 1), 1), 4),
        "cache_type_k": cache("cache_type_k", recommended.cacheTypeK),
        "cache_type_v": cache("cache_type_v", recommended.cacheTypeV),
    ]
    if let mm = recommended.mmproj { p["mmproj"] = mm }
    return p
}
