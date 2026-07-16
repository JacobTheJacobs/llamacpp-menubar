import AppKit

/// The per-model launch panel: recommended profile, tweaks, then start.
final class LaunchPanelController: NSObject {
    private let modelPath: String
    private let recommendation: Recommendation
    private let info: GGUFInfo?
    private let onLaunch: (String, [String: Any]) -> Void
    private let onCancel: () -> Void

    private var web: WebPanel?
    private var didLaunch = false

    init(
        modelPath: String,
        recommendation: Recommendation,
        info: GGUFInfo?,
        onLaunch: @escaping (String, [String: Any]) -> Void,
        onCancel: @escaping () -> Void
    ) {
        self.modelPath = modelPath
        self.recommendation = recommendation
        self.info = info
        self.onLaunch = onLaunch
        self.onCancel = onCancel
        super.init()
    }

    @discardableResult
    func show() -> Bool {
        guard let html = loadPanelHTML("launch.html") else {
            logLine("launch.html missing — cannot show panel")
            return false
        }
        let panel = WebPanel(
            title: "Launch · \(shortName(modelPath, limit: 48))",
            size: NSSize(width: 700, height: 720),
            minSize: NSSize(width: 560, height: 460),
            html: html,
            payload: payload(),
            onMessage: { [weak self] body in self?.handle(body) },
            onClose: { [weak self] in
                guard let self else { return }
                self.web = nil
                if !self.didLaunch { self.onCancel() }
            }
        )
        web = panel
        return panel.show()
    }

    func close() {
        web?.close()
    }

    // MARK: Payload

    private func payload() -> [String: Any] {
        let rec = recommendation
        var values = rec.params
        // Saved tweaks win, but only for keys the user can actually set, and
        // never above the ceilings this machine and model impose.
        for (k, v) in prefsForModel(modelPath) where prefKeys.contains(k) {
            values[k] = v
        }
        let ceiling = rec.tiers.last ?? rec.ctx
        if let saved = values["ctx"] as? Int, saved > ceiling {
            values["ctx"] = ceiling
        }
        for key in ["cache_type_k", "cache_type_v"] {
            if let v = values[key] as? String, !isValidCacheType(v) {
                values[key] = rec.cacheTypeK
            }
        }

        // KV size is linear in context, so per-token cost lets the panel show a
        // live, exact figure for any context the user types — not just the tiers.
        let overhead = computeOverheadGB(modelGB: rec.modelGB)
        var perToken: [String: Double] = [:]
        for type in uiCacheTypes {
            perToken[type] = kvCacheGB(
                info: info, modelGB: rec.modelGB, ctx: 1, cacheK: type, cacheV: type
            )
        }

        var meta = "\(fmtSize(rec.modelGB)) · \(rec.chip) · "
            + "\(String(format: "%.0f", rec.totalRamGB)) GB RAM"
        if rec.mmproj != nil { meta += " · 👁 vision" }

        return [
            "model_name": (modelPath as NSString).lastPathComponent
                .replacingOccurrences(of: ".gguf", with: ""),
            "model_path": modelPath,
            "model_meta": meta,
            "recommend_reason": rec.reason,
            "recommended": rec.params,
            "values": values,
            "mmproj": rec.mmproj ?? "",
            "tiers": rec.tiers,
            "trained_ctx": rec.trainedCtx,
            "cache_types": uiCacheTypes,
            "budget_gb": (rec.budgetGB * 100).rounded() / 100,
            "kv_per_token_gb": perToken,
            "overhead_gb": overhead,
            "exact_kv": info?.canSizeKV ?? false,
            "hardware": [
                "chip": rec.chip,
                "total_ram_gb": rec.totalRamGB,
                "available_ram_gb": rec.availableGB,
                "perf_cores": rec.threads,
            ],
        ]
    }

    // MARK: Bridge

    private func handle(_ body: [String: Any]) {
        guard let action = body["action"] as? String else { return }
        switch action {
        case "launch":
            let raw = body["params"] as? [String: Any] ?? [:]
            let params = sanitizeParams(raw, recommended: recommendation)
            if (body["save_defaults"] as? Bool) ?? false {
                savePrefsForModel(modelPath, params: params)
            }
            didLaunch = true
            onLaunch(modelPath, params)
            close()
        case "cancel":
            close()
        default:
            break
        }
    }
}
