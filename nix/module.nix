# NixOS module: services.seriallog
#
#   services.seriallog = {
#     enable = true;
#     ports = { usb0 = 4001; usb1 = 4002; acm0 = 4003; };   # ser2net TCP ports
#     alerts = [ { name = "crash"; pattern = "PANIC|HardFault"; ignore_case = true; } ];
#     notify = true;      # desktop notifications in graphical sessions
#     web.enable = true;  # read-only dashboard on http://127.0.0.1:8777
#   };
#
# ser2net must allow more than one client per port (max-connections), or the
# logger and an interactive session will kick each other off.
#
# The dashboard has no authentication: leave web.bind on localhost and reach
# it over an SSH port forward or Tailscale, or bind it to a trusted network.

{ config, lib, pkgs, ... }:

let
  cfg = config.services.seriallog;
  toml = pkgs.formats.toml { };
  configFile = toml.generate "seriallog.toml" ({
    log_dir = cfg.logDir;
    retention_days = cfg.retentionDays;
    flush_after = cfg.flushAfter;
    port = lib.mapAttrsToList (name: port: { inherit name port; host = cfg.host; }) cfg.ports;
  } // lib.optionalAttrs (cfg.alerts != [ ]) {
    alert = map (a: lib.filterAttrs (_: v: v != null) a) cfg.alerts;
  });
  # Both units need nothing but TCP and the log folder, which the daemon writes
  # and the dashboard only reads (ProtectSystem=strict keeps it read-only
  # unless listed in ReadWritePaths).
  hardening = {
    NoNewPrivileges = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    PrivateTmp = true;
    PrivateDevices = true;
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectControlGroups = true;
    RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
    RestrictNamespaces = true;
    LockPersonality = true;
    MemoryDenyWriteExecute = true;
    SystemCallArchitectures = "native";
    CapabilityBoundingSet = "";
  };
in
{
  options.services.seriallog = {
    enable = lib.mkEnableOption "seriallog, timestamped logs of ser2net serial ports";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "seriallog from its flake";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Where ser2net listens.";
    };

    ports = lib.mkOption {
      type = lib.types.attrsOf lib.types.port;
      default = { };
      example = { usb0 = 4001; acm0 = 4003; };
      description = "Port name (used in file names and on the command line) to ser2net TCP port.";
    };

    alerts = lib.mkOption {
      type = lib.types.listOf (lib.types.submodule {
        options = {
          name = lib.mkOption { type = lib.types.str; };
          pattern = lib.mkOption { type = lib.types.str; description = "Python regular expression."; };
          ignore_case = lib.mkOption { type = lib.types.bool; default = false; };
          ports = lib.mkOption { type = lib.types.nullOr (lib.types.listOf lib.types.str); default = null; };
          cooldown = lib.mkOption { type = lib.types.int; default = 60; description = "Seconds."; };
        };
      });
      default = [ ];
    };

    logDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/seriallog";
    };

    retentionDays = lib.mkOption {
      type = lib.types.ints.positive;
      default = 90;
    };

    flushAfter = lib.mkOption {
      type = lib.types.number;
      default = 1.0;
      description = "Seconds of silence before a line without a newline is written.";
    };

    notify = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Start `seriallog notify` in graphical sessions (XDG autostart).";
    };

    web.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Run `seriallog web`, a read-only dashboard over the log folder, as the
        separate service seriallog-web. It has no authentication.
      '';
    };

    web.bind = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1:8777";
      description = ''
        HOST:PORT the dashboard listens on. Keep it on localhost (reach it over an
        SSH port forward or Tailscale) or bind it to a trusted network only.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    environment.etc."seriallog.toml".source = configFile;
    environment.systemPackages = [ cfg.package ] ++ lib.optional cfg.notify pkgs.libnotify;

    users.users.seriallog = {
      isSystemUser = true;
      group = "seriallog";
      description = "seriallog daemon";
    };
    users.groups.seriallog = { };

    systemd.services.seriallog = {
      description = "Timestamped logs of ser2net serial ports";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" "ser2net.service" ];
      restartTriggers = [ configFile ];
      serviceConfig = hardening // {
        ExecStart = "${lib.getExe cfg.package} daemon";
        User = "seriallog";
        Group = "seriallog";
        Restart = "always";
        RestartSec = 5;
        StateDirectory = lib.mkIf (cfg.logDir == "/var/lib/seriallog") "seriallog";
        StateDirectoryMode = "0755";      # logs are readable by everyone on the box
        ReadWritePaths = [ cfg.logDir ];
      };
    };

    # The dashboard only reads the (world-readable) log folder, so it runs as a
    # throwaway user with no write access anywhere.
    systemd.services.seriallog-web = lib.mkIf cfg.web.enable {
      description = "seriallog web dashboard (read-only, no authentication)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" "seriallog.service" ];
      restartTriggers = [ configFile ];
      serviceConfig = hardening // {
        ExecStart = "${lib.getExe cfg.package} web --bind ${lib.escapeShellArg cfg.web.bind}";
        DynamicUser = true;
        Restart = "always";
        RestartSec = 5;
      };
    };

    environment.etc."xdg/autostart/seriallog-notify.desktop" = lib.mkIf cfg.notify {
      text = ''
        [Desktop Entry]
        Type=Application
        Name=seriallog alerts
        Comment=Desktop notification for each serial-port alert
        Exec=${lib.getExe cfg.package} notify
        NoDisplay=true
      '';
    };
  };
}
