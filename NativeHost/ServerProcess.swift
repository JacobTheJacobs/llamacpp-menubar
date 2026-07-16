import Foundation

/// PIDs of llama-servers listening on `port`.
///
/// Narrow by design: a process is only a candidate if it holds this exact port
/// *and* its command line is a llama-server, so a server on another port, or
/// anything else holding this one, is never signalled.
private func llamaServerPIDs(onPort port: Int) -> [Int32] {
    let out = runTool("/usr/sbin/lsof", ["-i", "TCP:\(port)", "-sTCP:LISTEN", "-n", "-P", "-t"])
    return out.split(whereSeparator: \.isNewline).compactMap { line -> Int32? in
        guard let pid = Int32(line.trimmingCharacters(in: .whitespaces)) else { return nil }
        let args = runTool("/bin/ps", ["-p", "\(pid)", "-o", "args="])
        guard args.contains("llama-server") || args.contains("llama-cli") else { return nil }
        return pid
    }
}

/// SIGTERM a child, escalating to SIGKILL if it outlasts `grace`.
///
/// `Process.terminate()` is SIGTERM and `interrupt()` is SIGINT — neither is a
/// kill, so the escalation has to go through `kill(2)` directly.
func terminate(_ proc: Process?, grace: TimeInterval) {
    guard let proc, proc.isRunning else { return }
    let pid = proc.processIdentifier
    proc.terminate()
    let deadline = Date().addingTimeInterval(grace)
    while proc.isRunning, Date() < deadline {
        usleep(100_000)
    }
    guard proc.isRunning else { return }
    logLine("SIGKILL child pid \(pid) after \(grace)s")
    kill(pid, SIGKILL)
    // SIGKILL is delivered, not applied — the process still has to be torn down
    // and reaped. Wait for that, so returning from here genuinely means gone;
    // callers free the port immediately afterwards.
    let hardDeadline = Date().addingTimeInterval(2.0)
    while proc.isRunning, Date() < hardDeadline {
        usleep(20_000)
    }
}

/// Terminate llama-servers holding `port`, escalating to SIGKILL after `grace`.
///
/// Blocks for up to `grace`, so callers keep it off the main thread. Quit uses
/// a short budget because macOS force-terminates an app that takes too long,
/// which would strand the very server this is trying to stop.
func reapLlamaServers(onPort port: Int, grace: TimeInterval = 3.0) {
    let pids = llamaServerPIDs(onPort: port)
    guard !pids.isEmpty else { return }
    for pid in pids { kill(pid, SIGTERM) }

    // Poll the known PIDs with a signal probe. Re-running lsof every tick would
    // spawn a subprocess per 100ms to learn nothing new — the set can only shrink.
    var alive = pids
    let deadline = Date().addingTimeInterval(grace)
    while Date() < deadline {
        alive = alive.filter { kill($0, 0) == 0 }
        if alive.isEmpty { return }
        usleep(100_000)
    }
    for pid in alive {
        logLine("SIGKILL pid \(pid) on port \(port) after \(grace)s")
        kill(pid, SIGKILL)
    }
}
