{
  description = "Read-only SQL against Oracle, MySQL, Postgres, SQL Server, SQLite or libsql, with agent-friendly output";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "aarch64-darwin"
        "x86_64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
    in
    {
      packages = forAllSystems (pkgs: rec {
        dbq = pkgs.python3Packages.buildPythonApplication {
          pname = "dbq";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python3Packages.flit-core ];
          # No driver needs a native client library: oracledb runs in thin
          # mode, pymysql, pg8000 and python-tds are pure Python.
          # sqlite uses stdlib — no extra dependency needed.
          # libsql-client is not yet in nixpkgs; install via pip for libsql/Turso.
          dependencies = with pkgs.python3Packages; [
            oracledb
            pymysql
            pg8000
            python-tds
            python-dotenv
          ];
          # Ship the agent skill so consumers can link it into their skills
          # dir straight out of the store instead of vendoring a copy.
          postInstall = ''
            mkdir -p $out/share/dbq
            cp -r $src/skills $out/share/dbq/
          '';
        };
        default = dbq;
      });
    };
}
