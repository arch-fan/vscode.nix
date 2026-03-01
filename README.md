# vscode.nix

Reproducible VS Code overlays for Nix.

This repository provides a flake overlay that overrides nixpkgs `vscode` and adds `vscode-insiders`, both pinned to official Microsoft upstream binaries with per-architecture hashes.

## What this repo provides

- `pkgs.vscode`: latest stable VS Code (official binary, pinned)
- `pkgs.vscode-insiders`: latest VS Code Insiders (official binary, pinned)
- Supported systems:
  - `x86_64-linux`
  - `aarch64-linux`
  - `armv7l-linux`
  - `x86_64-darwin`
  - `aarch64-darwin`

All versions and hashes are sourced from `versions.json`.

## Use as a flake input

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

## Use this repo directly

Build from this flake:

```bash
nix build .#vscode
nix build .#vscode-insiders
```

Run updater locally:

```bash
nix run .#update
```

This updates `versions.json` with the latest stable and insiders metadata from:

- `https://update.code.visualstudio.com/api/update/<platform>/stable/latest`
- `https://update.code.visualstudio.com/api/update/<platform>/insider/latest`

## Automation

### Hourly update PR workflow

File: `.github/workflows/update-vscode.yml`

- Runs hourly and on manual dispatch.
- Executes `nix run .#update`.
- If `versions.json` changed, opens/updates PR branch `automation/update-vscode`.
- Builds both Linux packages before creating the PR.

### CI workflow

File: `.github/workflows/ci.yml`

- Runs on push to `main`, pull requests, and manual dispatch.
- Evaluates overlay outputs for all supported systems.
- Builds Linux `vscode` and `vscode-insiders`.

## Notes

- VS Code is unfree software; consumers must set `config.allowUnfree = true`.
- The overlay reuses nixpkgs VS Code packaging logic and only overrides pinned upstream version/hash metadata.
