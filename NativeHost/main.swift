import AppKit
import Darwin

/// Held for the process lifetime — closing the descriptor releases the lock.
private var lockFD: Int32 = -1

/// A second status item for the same app is pure confusion. macOS already
/// dedupes bundle launches, but running the binary directly bypasses that.
private func acquireSingleInstanceLock() -> Bool {
    ensureConfigDir()
    let path = configDir.appendingPathComponent("llama-menu.lock").path
    let fd = Darwin.open(path, O_CREAT | O_RDWR, 0o644)
    guard fd >= 0 else { return true }  // Can't lock — don't block the app.
    guard flock(fd, LOCK_EX | LOCK_NB) == 0 else {
        Darwin.close(fd)
        return false
    }
    lockFD = fd
    ftruncate(fd, 0)
    let pid = "\(getpid())"
    _ = pid.withCString { write(fd, $0, strlen($0)) }
    return true
}

guard acquireSingleInstanceLock() else {
    notify("Already running — look for the llama in the menu bar")
    exit(0)
}

let app = NSApplication.shared
let controller = AppController()
app.delegate = controller
app.run()
