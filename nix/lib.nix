let
  vscodePlatforms = {
    x86_64-linux = "linux-x64";
    aarch64-linux = "linux-arm64";
    armv7l-linux = "linux-armhf";
    x86_64-darwin = "darwin";
    aarch64-darwin = "darwin-arm64";
  };

  vscodePlatformForSystem =
    system: vscodePlatforms.${system} or (throw "Unsupported system: ${system}");

  marketplaceExtensionsFromJSON =
    { pkgs, value }:
    let
      resolveExtension =
        entry:
        let
          system = pkgs.stdenv.hostPlatform.system;
          sha256Value = entry.sha256;
          targetPlatform = vscodePlatformForSystem system;
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
      builtins.mapAttrs (_: exts: mkExtensions exts) value
    else
      throw "Expected a Marketplace extension lock file to contain either a list or an attribute set of lists.";
in
{
  flake.lib = {
    inherit
      marketplaceExtensionsFromJSON
      vscodePlatformForSystem
      vscodePlatforms
      ;

    marketplaceExtensionsFromFile =
      { pkgs, path }:
      marketplaceExtensionsFromJSON {
        inherit pkgs;
        value = builtins.fromJSON (builtins.readFile path);
      };
  };
}
