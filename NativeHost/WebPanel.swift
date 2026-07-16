import AppKit
import WebKit

/// A real Mac window hosting a local HTML page.
///
/// Shared by the launch and settings panels: an `NSPanel` with genuine traffic
/// lights and an `NSVisualEffectView` backdrop, with the page in a `WKWebView`.
///
/// The HTML is loaded from memory with a nil base URL, so the page has an
/// opaque origin and no network access at all. State goes in through a
/// `WKUserScript` rather than string templating, and actions come back through
/// a single message handler.
final class WebPanel: NSObject, WKScriptMessageHandler, NSWindowDelegate {
    private var panel: NSPanel?
    private var webView: WKWebView?
    private let onMessage: ([String: Any]) -> Void
    private let onClose: () -> Void
    private static let bridgeName = "bridge"

    private let title: String
    private let size: NSSize
    private let minSize: NSSize
    private let html: String
    private let payload: [String: Any]

    init(
        title: String,
        size: NSSize,
        minSize: NSSize,
        html: String,
        payload: [String: Any],
        onMessage: @escaping ([String: Any]) -> Void,
        onClose: @escaping () -> Void
    ) {
        self.title = title
        self.size = size
        self.minSize = minSize
        self.html = html
        self.payload = payload
        self.onMessage = onMessage
        self.onClose = onClose
        super.init()
    }

    @discardableResult
    func show() -> Bool {
        let config = WKWebViewConfiguration()
        let ucc = WKUserContentController()
        ucc.add(self, name: Self.bridgeName)
        ucc.addUserScript(
            WKUserScript(
                source: "window.DEFAULTS = \(Self.encode(payload));",
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true
            )
        )
        config.userContentController = ucc
        config.suppressesIncrementalRendering = true
        if ProcessInfo.processInfo.environment["LLAMA_MENU_DEBUG"] != nil {
            config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        }

        let panel = NSPanel(
            contentRect: NSRect(origin: .zero, size: size),
            styleMask: [.titled, .closable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        panel.title = title
        panel.titlebarAppearsTransparent = true
        panel.titleVisibility = .visible
        panel.isMovableByWindowBackground = true
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        // These panels are designed dark; a light system theme would wash them out.
        panel.appearance = NSAppearance(named: .darkAqua)
        panel.delegate = self
        panel.minSize = minSize

        // Fit smaller displays rather than running off-screen.
        if let visible = NSScreen.main?.visibleFrame {
            panel.setContentSize(
                NSSize(
                    width: min(size.width, visible.width - 40),
                    height: min(size.height, visible.height - 40)
                )
            )
        }

        let effect = NSVisualEffectView(frame: NSRect(origin: .zero, size: panel.frame.size))
        effect.material = .hudWindow
        effect.blendingMode = .behindWindow
        effect.state = .active
        effect.autoresizingMask = [.width, .height]
        panel.contentView = effect

        let webView = WKWebView(frame: effect.bounds, configuration: config)
        webView.autoresizingMask = [.width, .height]
        // Let the window vibrancy show through the page.
        webView.setValue(false, forKey: "drawsBackground")
        webView.underPageBackgroundColor = .clear
        effect.addSubview(webView)
        webView.loadHTMLString(html, baseURL: nil)

        self.panel = panel
        self.webView = webView

        panel.center()
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        return true
    }

    func close() {
        panel?.close()
    }

    /// Push fresh state into an open page (e.g. after a folder picker).
    func apply(_ state: [String: Any]) {
        webView?.evaluateJavaScript("window.applyState && window.applyState(\(Self.encode(state)));")
    }

    static func encode(_ object: Any) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: object),
              let json = String(data: data, encoding: .utf8)
        else { return "{}" }
        // Defence in depth: the page is inert, but a model in a directory named
        // `</script>…` should still never break out of this literal.
        return json
            .replacingOccurrences(of: "<", with: "\\u003c")
            .replacingOccurrences(of: ">", with: "\\u003e")
            .replacingOccurrences(of: "&", with: "\\u0026")
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        guard let body = message.body as? [String: Any] else { return }
        onMessage(body)
    }

    func windowWillClose(_ notification: Notification) {
        onClose()
        // The content controller retains its message handler and the web view
        // retains the controller — without this the panel leaks itself.
        webView?.configuration.userContentController
            .removeScriptMessageHandler(forName: Self.bridgeName)
        webView?.removeFromSuperview()
        webView = nil
        panel?.delegate = nil
        panel = nil
    }
}

/// Load a page from Resources, inlining the shared stylesheet.
///
/// The page is served with a nil base URL, so it cannot fetch a stylesheet by
/// relative path; substituting it here keeps one CSS source for both panels
/// without granting the page a file:// origin. The CSS is our own asset, never
/// user data, so this is not a templating injection risk.
func loadPanelHTML(_ name: String) -> String? {
    let dir = resolveResourcesDir()
    guard let html = try? String(contentsOf: dir.appendingPathComponent(name), encoding: .utf8)
    else { return nil }
    guard let css = try? String(contentsOf: dir.appendingPathComponent("panel.css"), encoding: .utf8)
    else { return html }
    return html.replacingOccurrences(of: "<!--STYLE-->", with: "<style>\n\(css)\n</style>")
}

