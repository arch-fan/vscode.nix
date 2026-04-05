# vscode.nix

Reproducible VS Code binaries and reusable helpers for pinned VS Code Marketplace extensions.

This flake does two things:

- overrides `nixpkgs` `vscode` and exposes `vscode-insiders`, both pinned to official Microsoft upstream binaries
- exposes a small `lib` and updater apps for locking VS Code Marketplace extensions in JSON and consuming them from your own flakes

## Capabilities

This repository provides:

- `overlays.default`
  - `pkgs.vscode`: pinned stable VS Code
  - `pkgs.vscode-insiders`: pinned VS Code Insiders
- `apps.update` and `apps.update-vscode`
  - refresh `versions.json` with the latest pinned VS Code and VS Code Insiders metadata
- `apps.update-extensions`
  - refresh pinned Marketplace extension versions and hashes in a JSON lock file
- `lib.marketplaceExtensionsFromFile`
  - read a JSON lock file and convert it into values suitable for `programs.vscode.profiles.<name>.extensions`
- `lib.marketplaceExtensionsFromJSON`
  - same as above, but from an already-loaded JSON value

## Supported Systems

- `x86_64-linux`
- `aarch64-linux`
- `armv7l-linux`
- `x86_64-darwin`
- `aarch64-darwin`

## Use This Repo Directly

Build the pinned editors:

```bash
nix build .#vscode
nix build .#vscode-insiders
```

Refresh pinned VS Code metadata:

```bash
nix run .#update
```

Refresh a Marketplace extension lock file:

```bash
nix run .#update-extensions -- ./vscode-marketplace.lock.json
```

## Use In Your Own Flake

Add this flake as an input and apply the overlay:

```nix
{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    vscode-nix.url = "github:arch-fan/vscode.nix";
  };

  outputs = { self, nixpkgs, vscode-nix, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfree = true;
        overlays = [ vscode-nix.overlays.default ];
      };
    in
    {
      packages.${system}.default = pkgs.vscode;
      packages.${system}.insiders = pkgs.vscode-insiders;
    };
}
```

`vscode` is unfree software, so consumers must set `config.allowUnfree = true`.

## Marketplace Extension Lock Files

The extension updater works on JSON lock files. The lock file is the source of truth for Marketplace-backed extensions that are not already provided by `pkgs.vscode-extensions`.

Keep `pkgs.vscode-extensions` declarations in Nix. Put only `extensionsFromVscodeMarketplace` entries into the JSON lock file.

### Flat Lock File

Use a flat list when you only need one extension set:

```json
[
  {
    "publisher": "GitHub",
    "name": "copilot-chat",
    "version": "0.39.2026030501",
    "sha256": "sha256-uXKP6oK/paFpvmq7Gr8y9h7P6wPWS8gRj6eJnzdlWP4="
  },
  {
    "publisher": "openai",
    "name": "chatgpt",
    "version": "26.5304.11628",
    "sha256": "sha256-5kBNp0J6QObJDMYqmYZUIh5Y5t3Rg8eYkqg/TLiepM8="
  }
]
```

Consume it from your flake:

```nix
{
  outputs = { self, nixpkgs, vscode-nix, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfree = true;
        overlays = [ vscode-nix.overlays.default ];
      };

      marketplaceExtensions =
        vscode-nix.lib.marketplaceExtensionsFromFile
          pkgs
          ./vscode-marketplace.lock.json;
    in
    {
      homeManagerModules.default = {
        programs.vscode.profiles.default.extensions =
          with pkgs.vscode-extensions; [
            jnoortheen.nix-ide
            mkhl.direnv
          ]
          ++ marketplaceExtensions;
      };
    };
}
```

### Grouped Lock File

Use a grouped attribute set when you have multiple profiles or shared/profile-specific extension sets:

```json
{
  "base": [
    {
      "publisher": "GitHub",
      "name": "copilot-chat",
      "version": "0.39.2026030501",
      "sha256": "sha256-uXKP6oK/paFpvmq7Gr8y9h7P6wPWS8gRj6eJnzdlWP4="
    }
  ],
  "node": [
    {
      "publisher": "Prisma",
      "name": "prisma",
      "version": "31.5.35",
      "sha256": "sha256-gS1uNqDH3dKcValRXrTbPoXbkLa+A8BaBdhs1WF7jcc="
    }
  ],
  "lua": [
    {
      "publisher": "sumneko",
      "name": "lua",
      "version": "3.17.1",
      "sha256": "sha256-k4BmsvBl0StpAufJbdikM8nlvloClIwS1+Yr00nDnN8="
    }
  ]
}
```

Consume grouped data:

```nix
{
  outputs = { self, nixpkgs, vscode-nix, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfree = true;
        overlays = [ vscode-nix.overlays.default ];
      };

      marketplace =
        vscode-nix.lib.marketplaceExtensionsFromFile
          pkgs
          ./vscode-marketplace.lock.json;
    in
    {
      homeManagerModules.default = {
        programs.vscode.profiles.default.extensions =
          with pkgs.vscode-extensions; [
            jnoortheen.nix-ide
            mkhl.direnv
          ]
          ++ marketplace.base;

        programs.vscode.profiles.NodeJS.extensions =
          with pkgs.vscode-extensions; [
            astro-build.astro-vscode
            bradlc.vscode-tailwindcss
          ]
          ++ marketplace.base
          ++ marketplace.node;

        programs.vscode.profiles.Lua.extensions = marketplace.base ++ marketplace.lua;
      };
    };
}
```

### Entry Fields

Each Marketplace extension entry supports:

- `publisher`: required
- `name`: required
- `version`: required
- `sha256`: required
- `prerelease`: optional boolean override for that entry

`sha256` may be:

- a string for a generic VSIX that works on every supported system
- an attribute set keyed by Nix system for platform-specific VSIXs
- an attribute set with `default` plus per-system overrides for mixed generic/native extensions

The updater no longer writes `arch`. Legacy lock files that still contain it are still accepted and will be normalized on update.

`prerelease = true` allows prerelease versions for that one extension even without `--include-prerelease`.

`prerelease = false` blocks prerelease updates for that one extension even when `--include-prerelease` is set globally.

## Extension Updater

Run the updater against a lock file:

```bash
nix run github:arch-fan/vscode.nix#update-extensions -- ./vscode-marketplace.lock.json
```

The updater:

- accepts flat or grouped JSON lock files
- preserves the existing list and group order
- updates only the pinned `version` and `sha256` fields and removes legacy `arch` data when rewriting entries
- supports bounded parallel jobs
- can update all groups or only selected groups

### Parameters

`update-extensions` accepts:

- `PATH`
  - path to the lock file to update
- `--check`
  - print pending updates without writing the file
  - exits with code `1` when updates are available
- `--include-prerelease`
  - allow prerelease versions globally
  - per-entry `prerelease` still takes precedence
- `--group NAME`
  - update only one group in a grouped lock file
  - repeat the flag to select multiple groups
- `--jobs N`
  - maximum number of concurrent update jobs
  - default is `min(8, cpu-count)`

Examples:

```bash
nix run .#update-extensions -- ./vscode-marketplace.lock.json
nix run .#update-extensions -- --check ./vscode-marketplace.lock.json
nix run .#update-extensions -- --include-prerelease ./vscode-marketplace.lock.json
nix run .#update-extensions -- --group base --group node ./vscode-marketplace.lock.json
nix run .#update-extensions -- --jobs 4 ./vscode-marketplace.lock.json
```

### Expose The Updater In Your Own Flake

If you want a local app in your own flake:

```nix
{
  outputs = { self, nixpkgs, vscode-nix, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
    in
    {
      apps.${system}.update-vscode-extensions = {
        type = "app";
        program = "${vscode-nix.apps.${system}.update-extensions.program}";
      };
    };
}
```

Then run:

```bash
nix run .#update-vscode-extensions -- ./vscode-marketplace.lock.json
```

## VS Code Binary Metadata

`versions.json` is the lock file for the pinned editor binaries exposed by the overlay.

The `update` and `update-vscode` apps refresh:

- stable VS Code version and hashes
- VS Code Insiders version and hashes
- VS Code server hash

Data is pulled from:

- `https://update.code.visualstudio.com/api/update/<platform>/stable/latest`
- `https://update.code.visualstudio.com/api/update/<platform>/insider/latest`

## Notes

- This repository does not replace `pkgs.vscode-extensions`; use that first when an extension already exists in `nixpkgs`.
- The Marketplace updater is intended for pinned lock files, not for resolving latest versions at Nix evaluation time.
- JSON key order and list order are preserved logically, but file formatting is normalized when the updater rewrites the file.
