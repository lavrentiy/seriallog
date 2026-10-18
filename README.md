# seriallog

Timestamped logs of every serial port on a bench machine, with search, alerts and a
NixOS module.

The serial ports are already shared over TCP by [ser2net](https://github.com/cminyard/ser2net).
seriallog connects to each one as a silent extra client, so everything a device prints
is logged while you still use `tio` or `picocom` on the same port as usual. It never
writes to a device.

## What it does

| | |
|---|---|
| **Logs** | One file per port per day, millisecond timestamps in local time with UTC offset. Older days are gzipped and deleted after `retentionDays`. |
| **Devices coming and going** | Polls ports with nothing plugged in and logs `device connected` / `device disconnected`. ser2net being unreachable, and coming back, is logged too, so a gap in a log explains itself. |
| **Partial lines** | A prompt without a newline (`login: `) is written after `flushAfter` seconds of silence. |
| **Alerts** | Regex per alert, optional port filter and cooldown. Recorded in `alerts.jsonl`; `seriallog notify` turns them into desktop notifications. |
| **CLI** | `ports`, `tail [-f]`, `grep`, `alerts`; colour in a terminal, `--json` for scripts. |

Standard library only (Python 3.11+).

## Command line

```bash
seriallog ports                          # state of each port and its last line
seriallog tail usb0 -f                   # follow a port
seriallog grep 'HardFault|PANIC' --since 2h
seriallog grep -F 'fw 1.4' -p acm0 --since all
seriallog alerts --since 3d
```

`--since` and `--until` take `30m`, `2h`, `3d`, `today`, `yesterday`, `2026-10-18`,
`2026-10-18T19:30` or `all`.

## Log format

```
/var/lib/seriallog/usb0/2026-10-18.log
2026-10-18T19:40:00.871+02:00 ! device connected
2026-10-18T19:40:01.123+02:00 | boot ok, fw 1.4.2
2026-10-18T19:40:01.240+02:00 | login: 
```

`|` is a line from the device, `!` an event from the logger: a device connected or
disconnected, ser2net unreachable or reachable again. Timestamps are local time with
the UTC offset, so order survives a clock change and logs from different machines
line up. ANSI colour codes are removed; invalid UTF-8 and control characters are
written as `\xNN`. Plain text, so `grep`, `zgrep` and `less` work on it too.

## NixOS

```nix
# flake.nix
inputs.seriallog.url = "github:lavrentiy/seriallog";
inputs.seriallog.inputs.nixpkgs.follows = "nixpkgs";

# in the system's modules
seriallog.nixosModules.default
{
  services.seriallog = {
    enable = true;
    ports = { usb0 = 4001; usb1 = 4002; acm0 = 4003; };
    alerts = [
      { name = "crash"; pattern = "PANIC|HardFault|Guru Meditation|assert(ion)? failed"; ignore_case = true; }
    ];
    notify = true;
  };
}
```

ser2net has to accept more than one client per port, or the logger and an interactive
session kick each other off. In ser2net 4's YAML, per connection:

```yaml
options:
  max-connections: 4
```

The service runs as its own `seriallog` user with a hardened systemd unit. It needs
nothing but TCP to ser2net and its log folder.

## Development

```bash
nix develop                                   # or any Python 3.11+
PYTHONPATH=src python -m unittest discover -s tests -v
nix build                                     # builds and runs the tests
```

The tests include the daemon against a fake ser2net (open failures, a session with
alerts and a partial line, ser2net being down and coming back).
