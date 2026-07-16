import AppKit

// Regenerates every piece of generated artwork from the two menu bar SVGs, so
// the icon, the README hero and the state strip can never drift from the app
// the way hand-made binaries do. Run via scripts/make_assets.sh.

let root = URL(fileURLWithPath: CommandLine.arguments[1])
let resources = root.appendingPathComponent("resources")
let assets = root.appendingPathComponent("docs/assets")

let glyphOn = NSImage(contentsOf: resources.appendingPathComponent("menu-llama-on.svg"))!
let glyphOff = NSImage(contentsOf: resources.appendingPathComponent("menu-llama-off.svg"))!

// Brand gradient, shared by the icon and the panels' logo tile.
let brand = [
    NSColor(red: 0.22, green: 0.80, blue: 0.42, alpha: 1),
    NSColor(red: 0.04, green: 0.52, blue: 1.00, alpha: 1),
]

func bitmap(_ w: CGFloat, _ h: CGFloat, scale: CGFloat = 1, _ draw: () -> Void) -> NSBitmapImageRep {
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: Int(w * scale), pixelsHigh: Int(h * scale),
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
    )!
    rep.size = NSSize(width: w, height: h)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    // A fresh rep is not zeroed; without this, "transparent" renders black.
    NSColor.clear.set()
    NSRect(x: 0, y: 0, width: w, height: h).fill(using: .copy)
    draw()
    NSGraphicsContext.restoreGraphicsState()
    return rep
}

func write(_ rep: NSBitmapImageRep, to url: URL) {
    try! rep.representation(using: .png, properties: [:])!.write(to: url)
    print("  \(url.lastPathComponent)")
}

/// Tint a template glyph to a solid colour.
func tinted(_ img: NSImage, _ color: NSColor, size: CGFloat) -> NSImage {
    img.isTemplate = true
    return NSImage(size: NSSize(width: size, height: size), flipped: false) { r in
        img.draw(in: r)
        color.set()
        r.fill(using: .sourceAtop)
        return true
    }
}

// MARK: - App icon
//
// macOS Big Sur grid: a rounded square of 824 inside a 1024 canvas, with the
// 0.2237 corner ratio Apple uses for the circular approximation.

let S: CGFloat = 1024, tile: CGFloat = 824
let inset = (S - tile) / 2

let iconRep = bitmap(S, S) {
    let tileRect = NSRect(x: inset, y: inset, width: tile, height: tile)
    let squircle = NSBezierPath(
        roundedRect: tileRect, xRadius: tile * 0.2237, yRadius: tile * 0.2237
    )

    NSGraphicsContext.saveGraphicsState()
    let shadow = NSShadow()
    shadow.shadowColor = NSColor(white: 0, alpha: 0.28)
    shadow.shadowOffset = NSSize(width: 0, height: -12)
    shadow.shadowBlurRadius = 26
    shadow.set()
    NSColor.black.setFill()
    squircle.fill()
    NSGraphicsContext.restoreGraphicsState()

    NSGraphicsContext.saveGraphicsState()
    squircle.setClip()
    NSGradient(colors: brand)!.draw(in: tileRect, angle: -55)
    NSGradient(colors: [NSColor(white: 1, alpha: 0.22), NSColor(white: 1, alpha: 0)])!
        .draw(in: NSRect(x: inset, y: inset + tile * 0.5, width: tile, height: tile * 0.5), angle: -90)
    NSGraphicsContext.restoreGraphicsState()

    // Centre on the glyph's ink: the muzzle overhangs left and the neck reaches
    // the bottom edge, so the art is not centred within its own viewBox.
    let g = tile * 0.60
    let rect = NSRect(x: (S - g) / 2 + g * 0.02, y: (S - g) / 2 - g * 0.03, width: g, height: g)
    tinted(glyphOn, .white, size: g).draw(in: rect, from: .zero, operation: .sourceOver, fraction: 0.97)
}
print("icon:")
write(iconRep, to: resources.appendingPathComponent("icon.png"))

// MARK: - README hero

let heroRep = bitmap(960, 320, scale: 2) {
    NSGradient(colors: [
        NSColor(red: 0.09, green: 0.10, blue: 0.15, alpha: 1),
        NSColor(red: 0.05, green: 0.06, blue: 0.09, alpha: 1),
    ])!.draw(in: NSRect(x: 0, y: 0, width: 960, height: 320), angle: -70)

    NSImage(cgImage: iconRep.cgImage!, size: NSSize(width: 132, height: 132))
        .draw(in: NSRect(x: 64, y: 320 / 2 - 66, width: 132, height: 132))

    ("Llama Menu" as NSString).draw(at: NSPoint(x: 232, y: 186), withAttributes: [
        .font: NSFont.systemFont(ofSize: 46, weight: .semibold),
        .foregroundColor: NSColor.white,
    ])
    ("Local GGUF models from the macOS menu bar" as NSString).draw(
        at: NSPoint(x: 236, y: 152),
        withAttributes: [
            .font: NSFont.systemFont(ofSize: 17),
            .foregroundColor: NSColor(white: 1, alpha: 0.62),
        ]
    )
    ("At the largest context your Mac can actually hold" as NSString).draw(
        at: NSPoint(x: 236, y: 126),
        withAttributes: [
            .font: NSFont.systemFont(ofSize: 15),
            .foregroundColor: NSColor(red: 0.30, green: 0.85, blue: 0.42, alpha: 1),
        ]
    )

    let bar = NSRect(x: 236, y: 56, width: 300, height: 30)
    NSColor(white: 0.16, alpha: 0.96).setFill()
    NSBezierPath(roundedRect: bar, xRadius: 7, yRadius: 7).fill()
    tinted(glyphOn, .systemGreen, size: 18)
        .draw(in: NSRect(x: bar.minX + 200, y: bar.minY + 6, width: 18, height: 18))
    ("Wi-Fi   9:41" as NSString).draw(at: NSPoint(x: bar.minX + 228, y: bar.minY + 8), withAttributes: [
        .font: NSFont.systemFont(ofSize: 11),
        .foregroundColor: NSColor(white: 1, alpha: 0.75),
    ])
}
print("hero:")
write(heroRep, to: assets.appendingPathComponent("hero.png"))

// MARK: - Menu bar state strip

let states: [(String, NSImage, NSColor?)] = [
    ("stopped", glyphOff, nil), ("starting", glyphOn, .systemOrange),
    ("running", glyphOn, .systemGreen), ("error", glyphOn, .systemRed),
]
let stripRep = bitmap(620, 124, scale: 3) {
    var y: CGFloat = 64
    for dark in [false, true] {
        (dark ? NSColor(white: 0.13, alpha: 1) : NSColor(white: 0.90, alpha: 1)).setFill()
        NSRect(x: 0, y: y, width: 620, height: 58).fill()
        var x: CGFloat = 24
        for (name, img, tint) in states {
            let color = tint ?? (dark ? NSColor.white : NSColor.black)
            tinted(img, color, size: 18).draw(in: NSRect(x: x, y: y + 30, width: 18, height: 18))
            (name as NSString).draw(at: NSPoint(x: x - 6, y: y + 8), withAttributes: [
                .font: NSFont.systemFont(ofSize: 10),
                .foregroundColor: dark ? NSColor.white : NSColor.black,
            ])
            x += 150
        }
        y -= 62
    }
}
print("menu bar states:")
write(stripRep, to: assets.appendingPathComponent("menubar-states.png"))
