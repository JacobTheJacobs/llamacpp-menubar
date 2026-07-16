# Packaging

## Homebrew cask

`llama-menu.rb` is the cask source of truth. It is published by copying it to
the `Casks/` directory of the `JacobTheJacobs/homebrew-tap` repository, so users
can install with:

```sh
brew tap jacobthejacobs/tap
brew install --cask --no-quarantine llama-menu
```

`--no-quarantine` is required because the app is ad-hoc signed rather than
signed with an Apple Developer ID. Without it Gatekeeper refuses to open the
app, reporting it as damaged. Removing that requirement means a paid Developer
ID and notarisation.

The main `homebrew/cask` tap is not an option yet: it enforces notability
requirements (roughly 75 stars / 30 forks) that a new repository will not meet.

## Cutting a release

```sh
./scripts/build_app.sh
ditto -c -k --sequesterRsrc --keepParent "dist/Llama Menu.app" "LlamaMenu-$(cat VERSION).zip"
shasum -a 256 "LlamaMenu-$(cat VERSION).zip"
```

Attach the zip to a GitHub release tagged `v<VERSION>`, then update `version`
and `sha256` in the cask. The cask's `url` is derived from `version`, so those
two fields are the only edits.

Verify before publishing:

```sh
brew style --cask <tap>/llama-menu
brew audit --cask --new <tap>/llama-menu
```
