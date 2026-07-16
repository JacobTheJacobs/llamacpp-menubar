import Foundation

/// The handful of GGUF metadata fields that drive launch decisions.
struct GGUFInfo {
    var arch: String = ""
    /// Context the model was actually trained for. Launching above this is what
    /// produces confident nonsense, so it is a hard ceiling on the tier list.
    var contextLength: Int = 0
    var blockCount: Int = 0
    var embeddingLength: Int = 0
    var headCount: Int = 0
    var headCountKV: Int = 0
    /// Hybrid architectures (nemotron_h, jamba, falcon_h1 …) write head_count_kv
    /// as one value per layer, with 0 for the recurrent/Mamba layers that hold
    /// no KV cache at all. Treating those as attention layers overestimates the
    /// KV cache by more than an order of magnitude.
    var headCountKVPerLayer: [Int] = []
    var keyLength: Int = 0
    var valueLength: Int = 0

    var headDimK: Int {
        if keyLength > 0 { return keyLength }
        guard headCount > 0 else { return 0 }
        return embeddingLength / headCount
    }

    var headDimV: Int {
        if valueLength > 0 { return valueLength }
        guard headCount > 0 else { return 0 }
        return embeddingLength / headCount
    }

    /// Total KV heads summed across every layer — the real multiplier for cache
    /// size. Uniform models collapse to `layers × heads`; hybrids do not.
    var kvHeadUnits: Int {
        if !headCountKVPerLayer.isEmpty {
            return headCountKVPerLayer.reduce(0, +)
        }
        let heads = headCountKV > 0 ? headCountKV : headCount
        return blockCount * heads
    }

    var attentionLayers: Int {
        if !headCountKVPerLayer.isEmpty {
            return headCountKVPerLayer.filter { $0 > 0 }.count
        }
        return blockCount
    }

    /// Whether we read enough to size the KV cache exactly.
    var canSizeKV: Bool {
        kvHeadUnits > 0 && headDimK > 0 && headDimV > 0
    }
}

/// Minimal GGUF metadata parser.
///
/// Only walks the metadata block at the head of the file; the file is memory
/// mapped, so multi-GB tensor data is never faulted in. Assumes a
/// little-endian host, which every Mac this ships to is.
private struct GGUFParser {
    let raw: UnsafeRawBufferPointer
    var off: Int = 0

    private static let tString: UInt32 = 8
    private static let tArray: UInt32 = 9
    /// Per-layer arrays are bounded by layer count; anything longer is a
    /// tokenizer table we have no interest in materialising.
    private static let maxNumericArray = 8192

    private static func fixedSize(_ type: UInt32) -> Int? {
        switch type {
        case 0, 1, 7: return 1          // uint8, int8, bool
        case 2, 3: return 2             // uint16, int16
        case 4, 5, 6: return 4          // uint32, int32, float32
        case 10, 11, 12: return 8       // uint64, int64, float64
        default: return nil             // string, array
        }
    }

    private func has(_ n: Int) -> Bool {
        n >= 0 && off <= raw.count - n
    }

    private mutating func read<T>(_ type: T.Type) -> T? {
        let n = MemoryLayout<T>.size
        guard has(n) else { return nil }
        let v = raw.loadUnaligned(fromByteOffset: off, as: T.self)
        off += n
        return v
    }

    private mutating func readString() -> String? {
        guard let len = read(UInt64.self),
              len <= UInt64(raw.count),
              has(Int(len))
        else { return nil }
        let n = Int(len)
        let slice = UnsafeRawBufferPointer(rebasing: raw[off..<(off + n)])
        off += n
        return String(decoding: slice, as: UTF8.self)
    }

    private mutating func skipString() -> Bool {
        guard let len = read(UInt64.self),
              len <= UInt64(raw.count),
              has(Int(len))
        else { return false }
        off += Int(len)
        return true
    }

    private mutating func readNumeric(_ type: UInt32) -> Double? {
        switch type {
        case 0: return read(UInt8.self).map(Double.init)
        case 1: return read(Int8.self).map(Double.init)
        case 2: return read(UInt16.self).map(Double.init)
        case 3: return read(Int16.self).map(Double.init)
        case 4: return read(UInt32.self).map(Double.init)
        case 5: return read(Int32.self).map(Double.init)
        case 6: return read(Float32.self).map(Double.init)
        case 7: return read(UInt8.self).map(Double.init)
        case 10: return read(UInt64.self).map(Double.init)
        case 11: return read(Int64.self).map(Double.init)
        case 12: return read(Double.self)
        default: return nil
        }
    }

    private mutating func readArrayHeader() -> (elem: UInt32, count: Int)? {
        guard let elem = read(UInt32.self),
              let count = read(UInt64.self),
              count <= UInt64(raw.count)
        else { return nil }
        return (elem, Int(count))
    }

    private mutating func skipArrayBody(elem: UInt32, count: Int) -> Bool {
        if let size = Self.fixedSize(elem) {
            let total = size.multipliedReportingOverflow(by: count)
            guard !total.overflow, has(total.partialValue) else { return false }
            off += total.partialValue
            return true
        }
        if elem == Self.tString {
            for _ in 0..<count {
                guard skipString() else { return false }
            }
            return true
        }
        // Nested arrays are not emitted by any real writer — bail rather than guess.
        return false
    }

    private mutating func skipValue(_ type: UInt32) -> Bool {
        if let size = Self.fixedSize(type) {
            guard has(size) else { return false }
            off += size
            return true
        }
        if type == Self.tString { return skipString() }
        guard type == Self.tArray, let header = readArrayHeader() else { return false }
        return skipArrayBody(elem: header.elem, count: header.count)
    }

    mutating func parse() -> GGUFInfo? {
        // "GGUF" little-endian.
        guard let magic = read(UInt32.self), magic == 0x4655_4747 else { return nil }
        guard let version = read(UInt32.self), (2...3).contains(version) else { return nil }
        guard read(UInt64.self) != nil else { return nil }               // tensor count
        guard let kvCount = read(UInt64.self), kvCount < 1_000_000 else { return nil }

        // Suffix-matched so we don't need the architecture prefix up front.
        let wanted = [
            ".context_length",
            ".block_count",
            ".embedding_length",
            ".attention.head_count_kv",
            ".attention.head_count",
            ".attention.key_length",
            ".attention.value_length",
        ]

        var info = GGUFInfo()
        var numeric: [String: Double] = [:]
        var arrays: [String: [Double]] = [:]

        for _ in 0..<Int(kvCount) {
            guard let key = readString(), let type = read(UInt32.self) else { break }

            if key == "general.architecture", type == Self.tString {
                info.arch = readString() ?? ""
                continue
            }

            if let suffix = wanted.first(where: { key.hasSuffix($0) }) {
                if Self.fixedSize(type) != nil {
                    guard let v = readNumeric(type) else { break }
                    numeric[suffix] = v
                    continue
                }
                if type == Self.tArray {
                    guard let header = readArrayHeader() else { break }
                    if Self.fixedSize(header.elem) != nil, header.count <= Self.maxNumericArray {
                        var out: [Double] = []
                        out.reserveCapacity(header.count)
                        var ok = true
                        for _ in 0..<header.count {
                            guard let v = readNumeric(header.elem) else { ok = false; break }
                            out.append(v)
                        }
                        guard ok else { break }
                        arrays[suffix] = out
                        continue
                    }
                    guard skipArrayBody(elem: header.elem, count: header.count) else { break }
                    continue
                }
            }
            guard skipValue(type) else { break }
        }

        func int(_ key: String) -> Int {
            guard let d = numeric[key], d.isFinite, d > 0, d < 1e9 else { return 0 }
            return Int(d)
        }

        info.contextLength = int(".context_length")
        info.blockCount = int(".block_count")
        info.embeddingLength = int(".embedding_length")
        info.headCount = int(".attention.head_count")
        info.headCountKV = int(".attention.head_count_kv")
        info.keyLength = int(".attention.key_length")
        info.valueLength = int(".attention.value_length")

        if let perLayer = arrays[".attention.head_count_kv"] {
            info.headCountKVPerLayer = perLayer.map { $0.isFinite && $0 > 0 ? Int($0) : 0 }
        }
        // A per-layer head_count only matters for deriving head dim, which
        // explicit key_length/value_length already covers on these models.
        if info.headCount == 0, let heads = arrays[".attention.head_count"]?.max() {
            info.headCount = heads.isFinite && heads > 0 ? Int(heads) : 0
        }
        return info
    }
}

func readGGUFInfo(path: String) -> GGUFInfo? {
    guard let data = try? Data(contentsOf: URL(fileURLWithPath: path), options: [.mappedIfSafe])
    else { return nil }
    return data.withUnsafeBytes { raw -> GGUFInfo? in
        var parser = GGUFParser(raw: raw)
        return parser.parse()
    }
}
