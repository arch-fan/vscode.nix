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

      mkUpdateApp =
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
    in
    {
      overlays.default = overlay;

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
          updateApp = mkUpdateApp system;
        in
        {
          update = updateApp;
          default = updateApp;
        }
      );
    };
}
