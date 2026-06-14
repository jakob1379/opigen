{
  description = "opigen backup service";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    git-hooks = {
      url = "github:cachix/git-hooks.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      git-hooks,
      ...
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
        "x86_64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      mkPkgs = system: nixpkgs.legacyPackages.${system};
      hookConfig =
        pkgs:
        let
          uvxHook =
            name: command:
            pkgs.writeShellScript name ''
              if [ -n "''${NIX_BUILD_TOP:-}" ]; then
                echo "Skipping ${name} in the Nix build sandbox; uvx hook requires network/cache access."
                exit 0
              fi

              export UV_PYTHON=${pkgs.python313}/bin/python3.13
              export PATH=${
                pkgs.lib.makeBinPath [
                  pkgs.coreutils
                  pkgs.python313
                  pkgs.uv
                ]
              }
              exec ${command} "$@"
            '';
        in
        {
          default_stages = [
            "pre-commit"
            "commit-msg"
            "pre-push"
          ];
          excludes = [ "^(\\.cruft\\.json|\\.copier-answers\\.yml)$" ];

          hooks = {
            check-added-large-files.enable = true;
            check-case-conflicts.enable = true;
            check-merge-conflicts.enable = true;
            check-symlinks.enable = true;
            check-toml.enable = true;
            check-yaml = {
              enable = true;
              args = [ "--unsafe" ];
            };
            python-debug-statements.enable = true;
            detect-private-keys.enable = true;
            end-of-file-fixer.enable = true;
            fix-byte-order-marker.enable = true;
            mixed-line-endings = {
              enable = true;
              args = [ "--fix=auto" ];
            };
            nixfmt.enable = true;
            trim-trailing-whitespace.enable = true;

            ruff = {
              enable = true;
              args = [ "--exit-non-zero-on-fix" ];
              types_or = [
                "python"
                "pyi"
              ];
              before = [ "ruff-format" ];
            };
            ruff-format = {
              enable = true;
              types_or = [
                "python"
                "pyi"
              ];
            };

            prettier = {
              enable = true;
              types_or = [
                "markdown"
                "html"
                "css"
                "scss"
                "javascript"
                "json"
              ];
              excludes = [ "^docs/.*\\.md$" ];
              settings.prose-wrap = "always";
            };

            codespell = {
              enable = true;
              package = pkgs.codespell;
              entry = "${pkgs.lib.getExe pkgs.codespell} --write-changes";
            };

            yamlfix = {
              enable = true;
              package = pkgs.yamlfix;
              entry = pkgs.lib.getExe pkgs.yamlfix;
              types = [ "yaml" ];
            };

            toml-sort-fix = {
              enable = true;
              name = "toml-sort-fix";
              package = pkgs.toml-sort;
              entry = "${pkgs.lib.getExe pkgs.toml-sort} --in-place";
              files = "\\.toml$";
              types = [ "toml" ];
            };

            betterleaks = {
              enable = true;
              package = pkgs.betterleaks;
              entry = "${pkgs.lib.getExe pkgs.betterleaks} git --pre-commit --staged --baseline-path=betterleaks-report.json";
              pass_filenames = false;
            };

            check-github-workflows = {
              enable = true;
              name = "check-github-workflows";
              package = pkgs.check-jsonschema;
              entry = "${pkgs.lib.getExe pkgs.check-jsonschema} --builtin-schema vendor.github-workflows";
              files = "^\\.github/workflows/.*\\.ya?ml$";
              types = [ "yaml" ];
            };

            check-dependabot = {
              enable = true;
              name = "check-dependabot";
              package = pkgs.check-jsonschema;
              entry = "${pkgs.lib.getExe pkgs.check-jsonschema} --builtin-schema vendor.dependabot";
              files = "^\\.github/dependabot\\.ya?ml$";
              types = [ "yaml" ];
            };

            shellcheck = {
              enable = true;
              excludes = [ "^\\.envrc$" ];
              types = [ "shell" ];
            };

            validate-pyproject = {
              enable = true;
              name = "validate-pyproject";
              package = pkgs.uv;
              entry = "${uvxHook "validate-pyproject-hook" "${pkgs.lib.getExe pkgs.uv}x --from validate-pyproject --with validate-pyproject-schema-store validate-pyproject pyproject.toml"}";
              pass_filenames = false;
              files = "^pyproject\\.toml$";
            };

            complexipy = {
              enable = true;
              name = "complexipy";
              package = pkgs.uv;
              entry = "${uvxHook "complexipy-hook" "${pkgs.lib.getExe pkgs.uv}x complexipy"}";
              types = [ "python" ];
            };

            deadcode = {
              enable = true;
              name = "deadcode";
              package = pkgs.uv;
              entry = "${uvxHook "deadcode-hook" "${pkgs.lib.getExe pkgs.uv}x deadcode"}";
              types = [ "python" ];
            };

            bandit = {
              enable = true;
              package = pkgs.bandit;
              entry = "${pkgs.lib.getExe' pkgs.bandit "bandit"} -c pyproject.toml";
              types = [ "python" ];
            };

            uv-audit = {
              enable = true;
              name = "uv audit";
              description = "Run 'uv audit' to check uv.lock dependencies for known vulnerabilities";
              package = pkgs.symlinkJoin {
                name = "uv-audit-env";
                paths = [
                  pkgs.python314
                  pkgs.uv
                ];
              };
              entry = "${pkgs.writeShellScript "uv-audit-hook" ''
                if [ -n "''${NIX_BUILD_TOP:-}" ]; then
                  echo "Skipping uv audit in the Nix build sandbox; OSV audit requires network."
                  exit 0
                fi

                export PATH=${
                  pkgs.lib.makeBinPath [
                    pkgs.python314
                    pkgs.uv
                  ]
                }
                exec ${pkgs.lib.getExe pkgs.uv} audit --preview-features audit --no-managed-python --python-version 3.14 --frozen
              ''}";
              pass_filenames = false;
              files = "^uv\\.lock$";
            };
          };
        };
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

          pre-commit-check = git-hooks.lib.${system}.run (
            {
              src = ./.;
            }
            // hookConfig pkgs
          );

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
            ]
            ++ self.checks.${system}.pre-commit-check.enabledPackages;
            shellHook = self.checks.${system}.pre-commit-check.shellHook;
          };
        }
      );

      formatter = forAllSystems (
        system:
        let
          pkgs = mkPkgs system;
        in
        let
          config = self.checks.${system}.pre-commit-check.config;
        in
        pkgs.writeShellApplication {
          name = "opigen-format";
          runtimeInputs = [ config.package ];
          text = ''
            exec pre-commit run --all-files --config ${config.configFile}
          '';
        }
      );
    };
}
