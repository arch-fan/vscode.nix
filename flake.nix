{
  description = "Latest reproducible VS Code and VS Code Insiders overlays for nixpkgs";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    let
      lib = nixpkgs.lib;

      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "armv7l-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];

      forAllSystems = lib.genAttrs systems;

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
          resolveExtensionHash =
            entry:
            let
              sha256 = entry.sha256;
            in
            if builtins.isAttrs sha256 then
              sha256.${pkgs.stdenv.hostPlatform.system}
                or (throw "No sha256 for system ${pkgs.stdenv.hostPlatform.system} in extension ${entry.publisher}.${entry.name}")
            else if builtins.isString sha256 then
              sha256
            else
              throw "sha256 must be a string or an attribute set of per-system hashes for extension ${entry.publisher}.${entry.name}";

          normalizeExtensions =
            extensions: map (entry: entry // { sha256 = resolveExtensionHash entry; }) extensions;

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
          lib.mapAttrs (_: exts: mkExtensions exts) value
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
        in
        {
          vscode = prev.vscode.overrideAttrs (old: {
            version = latestStable.version;
            rev = latestStable.rev;
            src = mkSrc final {
              version = latestStable.version;
              rev = latestStable.rev;
              hashes = latestStable.hashes;
              quality = "stable";
            };
            passthru = mkPassthru old {
              version = latestStable.version;
              rev = latestStable.rev;
              quality = "stable";
              serverHash = latestStable.serverHash;
            };
          });

          vscode-insiders = (prev.vscode.override { isInsiders = true; }).overrideAttrs (old: {
            version = latestInsiders.version;
            rev = latestInsiders.rev;
            meta = old.meta // {
              mainProgram = "code-insiders";
            };
            src = mkSrc final {
              version = latestInsiders.version;
              rev = latestInsiders.rev;
              hashes = latestInsiders.hashes;
              quality = "insider";
            };
            passthru = mkPassthru old {
              version = latestInsiders.version;
              rev = latestInsiders.rev;
              quality = "insider";
              serverHash = latestInsiders.serverHash;
            };
          });
        };

      mkPkgs =
        system:
        import nixpkgs {
          inherit system;
          config.allowUnfree = true;
          overlays = [ overlay ];
        };

      mkUpdateVscodeApp =
        system:
        let
          pkgs = import nixpkgs { inherit system; };
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
        };

      mkUpdateExtensionsApp =
        system:
        let
          pkgs = import nixpkgs { inherit system; };
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
        };
    in
    {
      overlays.default = overlay;

      lib = {
        inherit marketplaceExtensionsFromJSON marketplaceExtensionsFromFile;
      };

      packages = forAllSystems (
        system:
        let
          pkgs = mkPkgs system;
        in
        {
          inherit (pkgs) vscode vscode-insiders;
          default = pkgs.vscode;
        }
      );

      apps = forAllSystems (
        system:
        let
          updateVscodeApp = mkUpdateVscodeApp system;
          updateExtensionsApp = mkUpdateExtensionsApp system;
        in
        {
          update = updateVscodeApp;
          update-vscode = updateVscodeApp;
          update-extensions = updateExtensionsApp;
          default = updateVscodeApp;
        }
      );

      checks = forAllSystems (
        system:
        let
          pkgs = mkPkgs system;
          flatFixture = builtins.fromJSON (builtins.readFile ./tests/fixtures/extensions-flat.json);
          groupedFixture = builtins.fromJSON (builtins.readFile ./tests/fixtures/extensions-grouped.json);
          flatResolved = marketplaceExtensionsFromJSON pkgs flatFixture;
          groupedResolved = marketplaceExtensionsFromJSON pkgs groupedFixture;
        in
        {
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
        }
      );
    };
}
