{
  description = "opigen backup service";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
        "x86_64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      mkPkgs = system: nixpkgs.legacyPackages.${system};
      mkSlimRestic =
        pkgs:
        pkgs.restic.overrideAttrs {
          postInstall = "";
        };
      mkBackupPackage =
        pkgs:
        pkgs.python314Packages.buildPythonApplication {
          pname = "opigen-backup";
          version = "0.1.0";
          pyproject = true;

          src = ./.;

          build-system = [
            pkgs.python314Packages.hatchling
          ];

          dependencies = [
            pkgs.python314Packages.docker
            pkgs.python314Packages.typer
          ];

          nativeCheckInputs = [
            pkgs.python314Packages.pytest
          ];

          pythonImportsCheck = [
            "backup"
            "backup.cli"
          ];

          checkPhase = ''
            runHook preCheck
            pytest -q
            runHook postCheck
          '';
        };
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = mkPkgs system;
          opigen-backup = mkBackupPackage pkgs;
          restic-slim = mkSlimRestic pkgs;
        in
        {
          inherit opigen-backup;
          default = opigen-backup;

          dockerImage = pkgs.dockerTools.buildLayeredImage {
            name = "opigen-backup";
            tag = "latest";
            contents = [
              opigen-backup
              pkgs.cacert
              restic-slim
            ];
            config = {
              Cmd = [
                "backup"
                "serve"
              ];
              Env = [
                "PATH=${opigen-backup}/bin:${restic-slim}/bin"
                "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              ];
            };
          };

          testFixtureImage = pkgs.dockerTools.buildLayeredImage {
            name = "opigen-backup-test-fixture";
            tag = "latest";
            contents = [
              pkgs.busybox
            ];
            config = {
              Cmd = [
                "sh"
              ];
              Env = [
                "PATH=${pkgs.busybox}/bin"
              ];
            };
          };
        }
      );

      apps = forAllSystems (
        system:
        let
          opigen-backup = mkBackupPackage (mkPkgs system);
        in
        {
          default = {
            type = "app";
            program = "${opigen-backup}/bin/backup";
            meta.description = "Run the opigen backup orchestrator";
          };
          backup = {
            type = "app";
            program = "${opigen-backup}/bin/backup";
            meta.description = "Run the opigen backup orchestrator";
          };
        }
      );

      checks = forAllSystems (
        system:
        let
          pkgs = mkPkgs system;
          opigen-backup = mkBackupPackage pkgs;
        in
        {
          inherit opigen-backup;

          ruff = pkgs.runCommand "opigen-backup-ruff" { } ''
            cd ${./.}
            ${pkgs.ruff}/bin/ruff check --no-cache .
            touch "$out"
          '';
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = mkPkgs system;
          opigen-backup = mkBackupPackage pkgs;
          restic-slim = mkSlimRestic pkgs;
          python = pkgs.python314.withPackages (
            ps: with ps; [
              docker
              pytest
              typer
            ]
          );
        in
        {
          default = pkgs.mkShell {
            inputsFrom = [
              opigen-backup
            ];
            packages = [
              python
              pkgs.ruff
              restic-slim
            ];
          };
        }
      );

      formatter = forAllSystems (
        system:
        let
          pkgs = mkPkgs system;
        in
        pkgs.writeShellApplication {
          name = "opigen-format";
          runtimeInputs = [
            pkgs.nixfmt
          ];
          text = ''
            if [ "$#" -eq 0 ]; then
              set -- flake.nix
            fi
            exec nixfmt "$@"
          '';
        }
      );
    };
}
