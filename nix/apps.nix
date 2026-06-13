{ inputs, ... }:
{
  perSystem = { pkgs, ... }: {
    apps.update-vscode =
      let
        updater = pkgs.writeShellApplication {
          name = "update-vscode-metadata";
          runtimeInputs = with pkgs; [
            coreutils
            curl
            jq
            nix
          ];
          text = builtins.readFile (inputs.self + /scripts/update-vscode-metadata.sh);
        };
      in
      {
        type = "app";
        program = "${updater}/bin/update-vscode-metadata";
        meta.description = "Refresh pinned VS Code metadata";
      };

    apps.update-extensions =
      let
        script = builtins.path {
          path = inputs.self + /scripts/update-vscode-extensions.py;
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
  };
}
