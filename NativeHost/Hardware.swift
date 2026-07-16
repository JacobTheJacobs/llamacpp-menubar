import Foundation
import IOKit

/// A snapshot of what this Mac can offer a model.
struct Hardware {
    var totalRamGB: Double
    var availableRamGB: Double
    var perfCores: Int
    var logicalCores: Int
    var gpuCores: Int
    var chip: String
    var appleSilicon: Bool
}

// MARK: - sysctl

private func sysctlInt(_ name: String) -> Int? {
    var size = 0
    guard sysctlbyname(name, nil, &size, nil, 0) == 0, size > 0 else { return nil }
    if size == 8 {
        var v: Int64 = 0
        guard sysctlbyname(name, &v, &size, nil, 0) == 0 else { return nil }
        return Int(v)
    }
    if size == 4 {
        var v: Int32 = 0
        guard sysctlbyname(name, &v, &size, nil, 0) == 0 else { return nil }
        return Int(v)
    }
    return nil
}

private func sysctlString(_ name: String) -> String? {
    var size = 0
    guard sysctlbyname(name, nil, &size, nil, 0) == 0, size > 0 else { return nil }
    var buf = [CChar](repeating: 0, count: size)
    guard sysctlbyname(name, &buf, &size, nil, 0) == 0 else { return nil }
    return String(cString: buf)
}

// MARK: - Memory

func totalRamGB() -> Double {
    Double(ProcessInfo.processInfo.physicalMemory) / 1_073_741_824.0
}

/// Free + inactive + speculative + purgeable, discounted by compressor pressure.
/// Read straight from the mach host rather than by parsing `vm_stat` output.
func availableMemoryGB() -> Double {
    var stats = vm_statistics64_data_t()
    var count = mach_msg_type_number_t(
        MemoryLayout<vm_statistics64_data_t>.stride / MemoryLayout<integer_t>.stride
    )
    let kr = withUnsafeMutablePointer(to: &stats) { ptr -> kern_return_t in
        ptr.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { intPtr in
            host_statistics64(mach_host_self(), HOST_VM_INFO64, intPtr, &count)
        }
    }
    guard kr == KERN_SUCCESS else { return totalRamGB() * 0.45 }

    let page = Double(vm_kernel_page_size)
    let gb = 1_073_741_824.0
    let reclaimable =
        (Double(stats.free_count)
            + Double(stats.inactive_count)
            + Double(stats.speculative_count)
            + Double(stats.purgeable_count)) * page / gb
    let compressed = Double(stats.compressor_page_count) * page / gb

    var available = reclaimable
    if compressed > 2 {
        available = max(0.5, available - compressed * 0.25)
    }
    return max(0.5, available)
}

// MARK: - GPU

/// Apple GPU core count from the IORegistry. The old path shelled out to
/// `system_profiler SPDisplaysDataType`, which can take seconds.
private func appleGPUCores() -> Int {
    var iterator: io_iterator_t = 0
    guard IOServiceGetMatchingServices(
        kIOMainPortDefault,
        IOServiceMatching("AGXAccelerator"),
        &iterator
    ) == KERN_SUCCESS else { return 0 }
    defer { IOObjectRelease(iterator) }

    while case let service = IOIteratorNext(iterator), service != 0 {
        defer { IOObjectRelease(service) }
        if let prop = IORegistryEntryCreateCFProperty(
            service, "gpu-core-count" as CFString, kCFAllocatorDefault, 0
        )?.takeRetainedValue(), let n = prop as? Int {
            return n
        }
    }
    return 0
}

// MARK: - Probe

func detectHardware() -> Hardware {
    let total = totalRamGB()
    let logical = sysctlInt("hw.logicalcpu") ?? ProcessInfo.processInfo.activeProcessorCount
    // perflevel0 = performance cores on Apple Silicon; absent on Intel.
    let perf = sysctlInt("hw.perflevel0.logicalcpu") ?? logical
    let arm = (sysctlInt("hw.optional.arm64") ?? 0) == 1
    let chip = sysctlString("machdep.cpu.brand_string") ?? (arm ? "Apple Silicon" : "Mac")

    return Hardware(
        totalRamGB: total,
        availableRamGB: availableMemoryGB(),
        perfCores: max(1, perf),
        logicalCores: max(1, logical),
        gpuCores: arm ? appleGPUCores() : 0,
        chip: chip,
        appleSilicon: arm
    )
}

/// Static facts (chip, core counts) probed once. Available RAM is volatile and
/// is re-read per panel open via `detectHardware()`.
let hardware: Hardware = detectHardware()
