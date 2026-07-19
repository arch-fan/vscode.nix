{ inputs, ... }:
{
  perSystem =
    { system, ... }:
    let
      nixpkgs =
        if system == "x86_64-darwin" then inputs.nixpkgs-darwin-x86 else inputs.nixpkgs;
    in
    {
      _module.args.pkgs = import nixpkgs {
        inherit system;
        config.allowUnfree = true;
      };
    };
}
