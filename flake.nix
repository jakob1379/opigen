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
                  pkgs.python313
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
                    pkgs.python313
                    pkgs.uv
                  ]
                }
                exec ${pkgs.lib.getExe pkgs.uv} audit --preview-features audit --no-managed-python --python-version 3.13 --frozen
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
      mkNonRootUserFiles =
        pkgs:
        let
          uid = "65532";
          gid = "65532";
        in
        [
          (pkgs.writeTextDir "etc/passwd" ''
            root:x:0:0:root:/root:/sbin/nologin
            opigen:x:${uid}:${gid}:opigen backup service:/home/opigen:/sbin/nologin
          '')
          (pkgs.writeTextDir "etc/group" ''
            root:x:0:
            opigen:x:${gid}:
          '')
        ];
      mkBackupPackage =
        pkgs:
        pkgs.python313Packages.buildPythonApplication {
          pname = "opigen-backup";
          version = "0.1.0";
          pyproject = true;

          src = ./.;

          build-system = [
            pkgs.python313Packages.hatchling
          ];

          dependencies = [
            pkgs.python313Packages.docker
            pkgs.python313Packages.structlog
            pkgs.python313Packages.typer
          ];

          nativeCheckInputs = [
            pkgs.python313Packages.pytest
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
      mkMinimalPythonRuntime =
        pkgs: opigen-backup:
        let
          python = pkgs.python313;
          pythonMinimal = pkgs.python3Minimal;
          pythonPackages = pkgs.python313Packages;
          pythonLib = python.libPrefix;
          sitePackages = python.sitePackages;
          runtimeModules = [
            opigen-backup
          ]
          ++ pkgs.lib.filter (drv: drv != python) (
            pythonPackages.requiredPythonModules [
              pythonPackages.docker
              pythonPackages.structlog
              pythonPackages.typer
            ]
          );
          copySitePackages = pkgs.lib.concatMapStringsSep "\n" (drv: ''
            if [ -d "${drv}/${sitePackages}" ]; then
              cp -r --no-preserve=ownership "${drv}/${sitePackages}/." "$out/${sitePackages}/"
            fi
          '') runtimeModules;
        in
        pkgs.runCommand "opigen-backup-minimal-runtime"
          {
            nativeBuildInputs = [
              pkgs.patchelf
              pkgs.removeReferencesTo
            ];
            disallowedRequisites = [
              pkgs.python3Minimal
              pkgs.python313
              pkgs.python314
            ];
          }
          ''
            mkdir -p "$out"
            cp -r --no-preserve=ownership "${pythonMinimal}/." "$out/"
            chmod -R u+w "$out"

            mkdir -p "$out/${sitePackages}" "$out/lib/${pythonLib}/lib-dynload"
            patchelf --set-rpath "$out/lib:${pkgs.glibc}/lib:${pkgs.stdenv.cc.cc.lib}/lib" "$out/bin/python3.13"
            for libpython in "$out/lib"/libpython3*.so*; do
              if [ -f "$libpython" ] && [ ! -L "$libpython" ]; then
                patchelf --set-rpath "${pkgs.glibc}/lib:${pkgs.stdenv.cc.cc.lib}/lib" "$libpython"
                remove-references-to -t "${pythonMinimal}" "$libpython"
              fi
            done
            remove-references-to -t "${pythonMinimal}" "$out/bin/python3.13"

            cp "${python}/lib/${pythonLib}/lib-dynload"/zlib*.so "$out/lib/${pythonLib}/lib-dynload/"
            cp "${python}/lib/${pythonLib}/lib-dynload"/_ssl*.so "$out/lib/${pythonLib}/lib-dynload/"
            cp "${python}/lib/${pythonLib}/lib-dynload"/_hashlib*.so "$out/lib/${pythonLib}/lib-dynload/"
            ${copySitePackages}
            chmod -R u+w "$out"

            rm -rf "$out/include" "$out/lib/pkgconfig" "$out/share"
            rm -rf "$out/nix-support"
            for sysconfigData in "$out/lib/${pythonLib}"/_sysconfigdata_*.py; do
              if [ -f "$sysconfigData" ]; then
                substituteInPlace "$sysconfigData" --replace-fail "${pythonMinimal}" "$out"
              fi
            done
            find "$out" -type d -name __pycache__ -prune -exec rm -rf {} +
            find "$out" -type f -name '*.pyc' -delete

            ln -sf python3.13 "$out/bin/python"
            ln -sf python3.13 "$out/bin/python3"
            rm -f "$out/bin/pydoc" "$out/bin/pydoc3" "$out/bin/pydoc3.13"
            {
              echo "#!$out/bin/python3.13"
              echo "import sys"
              echo
              echo "from backup.cli import main"
              echo
              echo "raise SystemExit(main(sys.argv[1:]))"
            } > "$out/bin/backup"
            chmod 0555 "$out/bin/backup"

            "$out/bin/python3.13" -c 'import zlib, ssl, hashlib'
            "$out/bin/backup" --help >/dev/null
          '';
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = mkPkgs system;
          opigen-backup = mkBackupPackage pkgs;
          opigen-backup-runtime = mkMinimalPythonRuntime pkgs opigen-backup;
          restic-slim = mkSlimRestic pkgs;
        in
        {
          inherit opigen-backup opigen-backup-runtime;
          default = opigen-backup;

          dockerImage = pkgs.dockerTools.buildLayeredImage {
            name = "opigen-backup";
            tag = "latest";
            contents = [
              opigen-backup-runtime
              pkgs.cacert
              restic-slim
            ]
            ++ mkNonRootUserFiles pkgs;
            fakeRootCommands = ''
              mkdir -p ./config ./state ./tmp ./home/opigen
              chown 65532:65532 ./state ./tmp ./home/opigen
              chmod 0755 ./config ./state ./home/opigen
              chmod 1777 ./tmp
            '';
            config = {
              Cmd = [
                "backup"
                "serve"
              ];
              Env = [
                "HOME=/home/opigen"
                "PATH=${opigen-backup-runtime}/bin:${restic-slim}/bin"
                "PYTHONDONTWRITEBYTECODE=1"
                "PYTHONUNBUFFERED=1"
                "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
                "TMPDIR=/tmp"
              ];
              Labels = {
                "org.opencontainers.image.description" = "Docker volume backup orchestrator using restic";
                "org.opencontainers.image.licenses" = "MIT";
                "org.opencontainers.image.source" = "https://github.com/jakob1379/opigen";
                "org.opencontainers.image.title" = "Opigen Backup";
              };
              StopSignal = "SIGTERM";
              User = "65532:65532";
              Volumes = {
                "/config" = { };
                "/state" = { };
              };
              WorkingDir = "/";
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
          opigen-backup-runtime = mkMinimalPythonRuntime pkgs opigen-backup;
        in
        {
          inherit opigen-backup opigen-backup-runtime;

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

          minimal-runtime-smoke = pkgs.runCommand "opigen-backup-minimal-runtime-smoke" { } ''
            ${opigen-backup-runtime}/bin/backup --help >/dev/null
            ${opigen-backup-runtime}/bin/python3.13 -c 'import zlib, ssl, typer, docker, structlog, requests, urllib3, backup.cli'
            touch "$out"
          '';

          docker-image-size = pkgs.runCommand "opigen-backup-docker-image-size" { } ''
            image_size=$(stat -c %s ${self.packages.${system}.dockerImage})
            max_size=95000000
            if [ "$image_size" -gt "$max_size" ]; then
              echo "docker image is $image_size bytes, expected <= $max_size" >&2
              exit 1
            fi
            printf '%s\n' "$image_size" > "$out"
          '';
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = mkPkgs system;
          opigen-backup = mkBackupPackage pkgs;
          restic-slim = mkSlimRestic pkgs;
          python = pkgs.python313.withPackages (
            ps: with ps; [
              docker
              pytest
              structlog
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
