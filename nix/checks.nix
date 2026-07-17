{ inputs, ... }:
{
  perSystem = { pkgs, ... }: {
    checks =
      let
        flatFixture = builtins.fromJSON (
          builtins.readFile (inputs.self + /tests/fixtures/extensions-flat.json)
        );
        groupedFixture = builtins.fromJSON (
          builtins.readFile (inputs.self + /tests/fixtures/extensions-grouped.json)
        );
        flatResolved = inputs.self.lib.marketplaceExtensionsFromJSON {
          inherit pkgs;
          value = flatFixture;
        };
        groupedResolved = inputs.self.lib.marketplaceExtensionsFromJSON {
          inherit pkgs;
          value = groupedFixture;
        };
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
                (python3.withPackages (ps: [
                  ps.click
                  ps.voluptuous
                ]))
              ];
            }
            ''
              bash ${inputs.self + /tests/test-update-extensions.sh} \
                ${inputs.self + /scripts/update-vscode-extensions.py} \
                ${inputs.self + /tests/fixtures} \
                ${inputs.self + /tests/mock-marketplace.py}

              touch $out
            '';

        ruff-lint =
          pkgs.runCommand "ruff-lint"
            {
              nativeBuildInputs = [ pkgs.ruff ];
            }
            ''
              cd ${inputs.self}
              ruff check --no-cache \
                scripts/update-vscode-extensions.py \
                tests/mock-marketplace.py
              touch $out
            '';
      };
  };
}
