{ inputs, ... }:
let
  inherit (inputs.self.lib) vscodePlatformForSystem;

  versions = builtins.fromJSON (builtins.readFile (inputs.self + /versions.json));
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
      platform = vscodePlatformForSystem system;
      archiveFmt = if pkgs.stdenv.hostPlatform.isDarwin then "zip" else "tar.gz";
      hash = hashes.${system} or (throw "Unsupported system: ${system}");
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
in
{
  perSystem =
    { pkgs, final, ... }:
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

      mkVscodeAttrs =
        release: quality: old:
        {
          version = release.version;
          rev = release.rev;
          src = mkSrc final {
            inherit (release) version rev;
            hashes = release.hashes;
            inherit quality;
          };
          # Newer VS Code bundles extra native modules that link against libraries
          # the nixpkgs derivation does not ship. Add them so autoPatchelfHook can
          # resolve the new dependencies on Linux.
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
        }
        // final.lib.optionalAttrs final.stdenv.hostPlatform.isLinux {
          unpackCmd = ''
            tar xf "$curSrc" --mode=+w --warning=no-timestamp --no-same-permissions
          '';
        };

      vscode = pkgs.vscode.overrideAttrs (mkVscodeAttrs latestStable "stable");

      vscode-insiders = (pkgs.vscode.override { isInsiders = true; }).overrideAttrs (
        old:
        (mkVscodeAttrs latestInsiders "insider" old)
        // {
          meta = old.meta // {
            mainProgram = "code-insiders";
          };
        }
      );
    in
    {
      packages = {
        inherit vscode vscode-insiders;
        default = vscode;
      };
    };
}
