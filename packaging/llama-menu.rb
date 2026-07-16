cask "llama-menu" do
  version "2.0.0"
  sha256 "7ae886e9a93e1bd63a85ae3f972a3a779b98ccae7a0b7dcf5bcef6a0b92e0a88"

  url "https://github.com/JacobTheJacobs/llamacpp-menubar/releases/download/v#{version}/LlamaMenu-#{version}.zip"
  name "Llama Menu"
  desc "Menu bar control for local llama.cpp, at the largest context that fits in memory"
  homepage "https://github.com/JacobTheJacobs/llamacpp-menubar"

  depends_on macos: :ventura
  depends_on arch: :arm64

  app "Llama Menu.app"

  zap trash: [
    "~/.config/llama-menu",
    "~/Library/LaunchAgents/com.llamamenu.app.plist",
  ]

  caveats <<~EOS
    Llama Menu is not signed with an Apple Developer ID, so Gatekeeper will
    refuse to open it unless you install with:

      brew install --cask --no-quarantine llama-menu

    It also needs llama.cpp:

      brew install llama.cpp
  EOS
end
