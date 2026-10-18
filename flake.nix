{
  description = "seriallog: timestamped logs of serial ports shared by ser2net";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs: { default = pkgs.callPackage ./nix/package.nix { }; });

      # The package build runs the test suite.
      checks = forAll (pkgs: { default = self.packages.${pkgs.system}.default; });

      nixosModules.default = { pkgs, ... }: {
        imports = [ ./nix/module.nix ];
        services.seriallog.package = nixpkgs.lib.mkDefault self.packages.${pkgs.system}.default;
      };

      devShells = forAll (pkgs: {
        default = pkgs.mkShell { packages = [ pkgs.python3 ]; };
      });
    };
}
