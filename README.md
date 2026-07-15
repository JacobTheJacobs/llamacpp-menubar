# Llama Menu

A macOS **menu bar app** for running [llama.cpp](https://github.com/ggerganov/llama.cpp) `llama-server` with your local GGUF models.

Inspired by [Llama-macOS](https://github.com/ggml-org/Llama-macOS) (status lifecycle, launch-at-login, accessory UI) and [llama-server-osx](https://github.com/jaredkhan/llama-server-osx) (model folders, config, logs).

![menu bar](resources/icon.png)

## Features

- **Menu bar only** — no Dock icon (`LSUIElement`)
- **Start / switch / stop** models from `~/models` (configurable)
- **Auto params** from free RAM (context size, Metal layers, threads)
- **Health checks** — “Ready” only when the API answers
- **Copy API URL** + **Open WebUI**
- **Launch at Login** (LaunchAgent; on by default after first launch)
- **Server logs** under `~/.config/llama-menu/logs/`
- **Single instance** lock
- Shippable **`.app` bundle** + install scripts

## Requirements

- macOS 12+
- Python 3.9+ with [`rumps`](https://github.com/jaredks/rumps)
- [`llama-server`](https://github.com/ggerganov/llama.cpp) (Homebrew: `brew install llama.cpp`)
- GGUF models in `~/models` (or set `models_dir` in config)

## Install (shipped app)

```sh
# deps
python3 -m pip install --user rumps
brew install llama.cpp   # if needed

# build + install to /Applications and launch
./scripts/install.sh
```

Or build only:

```sh
./scripts/build_app.sh
open "dist/Llama Menu.app"
```

Uninstall:

```sh
./scripts/uninstall.sh
# optional: also wipe config
./scripts/uninstall.sh --purge-config
```

## Dev launch

```sh
./launch.sh
# or
python3 llama_menu.py
```

## Usage

1. Click the menu bar icon.
2. **Start Server** → pick a `.gguf` model.
3. Wait for **Ready** (notification).
4. Point clients at `http://127.0.0.1:8080/v1`.

```sh
curl http://127.0.0.1:8080/v1/models
```

## Config

`~/.config/llama-menu/config.json`

| Key | Default | Notes |
|-----|---------|--------|
| `llama_server` | auto-detect | Path to binary |
| `models_dir` | `~/models` | Recursive `*.gguf` scan |
| `host` | `127.0.0.1` | Use `0.0.0.0` to expose LAN (careful) |
| `port` | `8080` | 1024–65535 |
| `ngl` | `999` | GPU layers (Metal) |
| `auto_ctx` | `true` | Size context from free RAM |
| `launch_at_login` | `true` | First-run default |
| `stop_server_on_quit` | `false` | Leave API up when quitting menu |

Edit via **Settings → Edit Config…**, then **Reload Config**.

## Layout

```
llama-menu/
  llama_menu.py          # app
  VERSION
  requirements.txt
  resources/             # icons
  scripts/
    build_app.sh         # → dist/Llama Menu.app
    install.sh
    uninstall.sh
  dist/                  # build output (gitignored)
```

## Security notes

- Binds to **localhost** by default.
- Setting `host` to `0.0.0.0` exposes the OpenAI-compatible API on your network — only do this if you understand the risk.
- App is **unsigned**. First open may need System Settings → Privacy & Security → Open Anyway.

## License

MIT — see [LICENSE](LICENSE).

App icon assets adapted from llama-server-osx (Draw Things–style mark). Menu glyph inspired by Llama-macOS.
