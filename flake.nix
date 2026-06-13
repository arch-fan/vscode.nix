{
  description = "Latest reproducible VS Code and VS Code Insiders overlays for nixpkgs";

  inputs.flake-parts.url = "github:hercules-ci/flake-parts";
  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs =
    inputs@{
      self,
      flake-parts,
      nixpkgs,
      ...
    }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "armv7l-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];

      flake =
        let
          platformForSystem = {
            x86_64-linux = "linux-x64";
            aarch64-linux = "linux-arm64";
            armv7l-linux = "linux-armhf";
            x86_64-darwin = "darwin";
            aarch64-darwin = "darwin-arm64";
          };

          versions = builtins.fromJSON (builtins.readFile ./versions.json);
          latestStable = versions.stable;
          latestInsiders = versions.insiders;

          marketplaceExtensionsFromJSON =
            pkgs: value:
            let
              resolveExtension =
                entry:
                let
                  system = pkgs.stdenv.hostPlatform.system;
                  sha256Value = entry.sha256;
                  targetPlatform = platformForSystem.${system} or (throw "Unsupported system: ${system}");
                  legacyArch = entry.arch or null;
                in
                if builtins.isAttrs sha256Value then
                  let
                    sha256 =
                      sha256Value.${system} or sha256Value.default
                        or (throw "No sha256 for system ${system} in extension ${entry.publisher}.${entry.name}");
                    baseEntry = builtins.removeAttrs entry [ "arch" ];
                  in
                  if builtins.hasAttr system sha256Value then
                    baseEntry
                    // {
                      inherit sha256;
                      arch = targetPlatform;
                    }
                  else
                    baseEntry // { inherit sha256; }
                else if builtins.isString sha256Value then
                  if legacyArch == null || builtins.isString legacyArch then
                    entry
                    // {
                      sha256 = sha256Value;
                    }
                  else
                    throw "'arch' must be a string when 'sha256' is a string for extension ${entry.publisher}.${entry.name}"
                else
                  throw "'sha256' must be a string or an attribute set for extension ${entry.publisher}.${entry.name}";

              normalizeExtensions = extensions: map resolveExtension extensions;

              mkExtensions =
                extensions:
                if builtins.isList extensions then
                  pkgs.vscode-utils.extensionsFromVscodeMarketplace (normalizeExtensions extensions)
                else
                  throw "Expected a list of VS Code Marketplace extensions.";
            in
            if builtins.isList value then
              mkExtensions value
            else if builtins.isAttrs value then
              nixpkgs.lib.mapAttrs (_: exts: mkExtensions exts) value
            else
              throw "Expected a Marketplace extension lock file to contain either a list or an attribute set of lists.";

          marketplaceExtensionsFromFile =
            pkgs: path: marketplaceExtensionsFromJSON pkgs (builtins.fromJSON (builtins.readFile path));

          mkSrc =
            pkgs:
            {
              version,
              rev,
              hashes,
              quality,
            }:
            let
              inherit (pkgs.stdenv.hostPlatform) system;
              throwSystem = throw "Unsupported system: ${system}";
              platform = platformForSystem.${system} or throwSystem;
              archiveFmt = if pkgs.stdenv.hostPlatform.isDarwin then "zip" else "tar.gz";
              hash = hashes.${system} or throwSystem;
            in
            pkgs.fetchurl {
              name = "VSCode_${version}_${platform}.${archiveFmt}";
              url = "https://update.code.visualstudio.com/commit:${rev}/${platform}/${quality}";
              inherit hash;
            };

          mkServer =
            pkgs:
            {
              rev,
              quality,
              serverHash,
            }:
            pkgs.srcOnly {
              name = "vscode-server-${rev}.tar.gz";
              src = pkgs.fetchurl {
                name = "vscode-server-${rev}.tar.gz";
                url = "https://update.code.visualstudio.com/commit:${rev}/server-linux-x64/${quality}";
                hash = serverHash;
              };
              stdenv = pkgs.stdenvNoCC;
            };

          overlay =
            final: prev:
            let
              inherit (final.stdenv.hostPlatform) system;
              platform = platformForSystem.${system} or (throw "Unsupported system: ${system}");

              fixRipgrepPath =
                builtins.replaceStrings
                  [ "@vscode/ripgrep/bin/rg" ]
                  [ "@vscode/ripgrep-universal/bin/${platform}/rg" ];

              mkPassthru =
                old:
                {
                  version,
                  rev,
                  quality,
                  serverHash,
                }:
                old.passthru
                // {
                  vscodeVersion = version;
                  inherit rev;
                  updateScript = null;
                  vscodeServer = mkServer final {
                    inherit
                      rev
                      quality
                      serverHash
                      ;
                  };
                };

              mkVscodeAttrs = release: quality: old: {
                version = release.version;
                rev = release.rev;
                src = mkSrc final {
                  inherit (release) version rev;
                  hashes = release.hashes;
                  inherit quality;
                };
                postPatch = fixRipgrepPath (old.postPatch or "");
                # Newer VS Code bundles extra native modules (e.g. the GitHub
                # Copilot "computer use" prebuilds) that link against libraries
                # the nixpkgs derivation does not ship. Add them so
                # autoPatchelfHook can resolve the new dependencies on Linux.
                buildInputs =
                  (old.buildInputs or [ ])
                  ++ final.lib.optionals final.stdenv.hostPlatform.isLinux (
                    map final.lib.getLib (
                      with final;
                      [
                        libxtst # libXtst.so.6
                        libjpeg8 # libjpeg.so.8
                        pipewire # libpipewire-0.3.so.0
                        libei # libei.so.1
                      ]
                    )
                  );
                passthru = mkPassthru old {
                  inherit (release) version rev serverHash;
                  inherit quality;
                };
              };
            in
            {
              vscode = prev.vscode.overrideAttrs (mkVscodeAttrs latestStable "stable");

              vscode-insiders = (prev.vscode.override { isInsiders = true; }).overrideAttrs (
                old:
                (mkVscodeAttrs latestInsiders "insider" old)
                // {
                  meta = old.meta // {
                    mainProgram = "code-insiders";
                  };
                }
              );
            };
        in
        {
          overlays.default = overlay;

          lib = {
            inherit marketplaceExtensionsFromJSON marketplaceExtensionsFromFile;
          };
        };

      perSystem =
        { system, ... }:
        let
          pkgs = import nixpkgs {
            inherit system;
            config.allowUnfree = true;
            overlays = [ self.overlays.default ];
          };

          updateVscodeApp =
            let
              updater = pkgs.writeShellApplication {
                name = "update-vscode-metadata";
                runtimeInputs = with pkgs; [
                  coreutils
                  curl
                  jq
                  nix
                ];
                text = builtins.readFile ./scripts/update-vscode-metadata.sh;
              };
            in
            {
              type = "app";
              program = "${updater}/bin/update-vscode-metadata";
              meta.description = "Refresh pinned VS Code metadata";
            };

          updateExtensionsApp =
            let
              script = builtins.path {
                path = ./scripts/update-vscode-extensions.py;
                name = "update-vscode-extensions.py";
              };
              updater = pkgs.writeShellApplication {
                name = "update-vscode-extensions";
                runtimeInputs = with pkgs; [
                  nix
                  python3
                ];
                text = ''
                  exec python ${script} "$@"
                '';
              };
            in
            {
              type = "app";
              program = "${updater}/bin/update-vscode-extensions";
              meta.description = "Refresh pinned VS Code Marketplace extension metadata";
            };

          flatFixture = builtins.fromJSON (builtins.readFile ./tests/fixtures/extensions-flat.json);
          groupedFixture = builtins.fromJSON (builtins.readFile ./tests/fixtures/extensions-grouped.json);
          flatResolved = self.lib.marketplaceExtensionsFromJSON pkgs flatFixture;
          groupedResolved = self.lib.marketplaceExtensionsFromJSON pkgs groupedFixture;
        in
        {
          packages = {
            inherit (pkgs) vscode vscode-insiders;
            default = pkgs.vscode;
          };

          apps = {
            update-vscode = updateVscodeApp;
            update-extensions = updateExtensionsApp;
          };

          devShells.default = pkgs.mkShellNoCC {
            packages = with pkgs; [
              curl
              jq
              python3
              nixfmt
            ];
          };

          formatter = pkgs.nixfmt-tree;

          checks = {
            lib-marketplace-flat =
              assert builtins.length flatResolved == 2;
              pkgs.runCommand "lib-marketplace-flat" { } ''
                touch $out
              '';

            lib-marketplace-grouped =
              assert
                builtins.attrNames groupedResolved == [
                  "base"
                  "native"
                  "node"
                ];
              assert builtins.length groupedResolved.base == 2;
              assert builtins.length groupedResolved.node == 1;
              assert builtins.length groupedResolved.native == 1;
              pkgs.runCommand "lib-marketplace-grouped" { } ''
                touch $out
              '';

            update-extensions =
              pkgs.runCommand "update-vscode-extensions-test"
                {
                  nativeBuildInputs = with pkgs; [
                    bash
                    coreutils
                    diffutils
                    gnugrep
                    jq
                    python3
                  ];
                }
                ''
                  bash ${./tests/test-update-extensions.sh} \
                    ${./scripts/update-vscode-extensions.py} \
                    ${./tests/fixtures} \
                    ${./tests/mock-marketplace.py}

                  touch $out
                '';
          };
        };
    };
}
