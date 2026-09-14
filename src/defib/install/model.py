"""Input model for the OpenIPC installer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InstallRequest:
    """Validated CLI inputs consumed by install orchestration."""

    chip: str
    firmware_path: str
    uboot_path: str = ""
    port: str = "/dev/ttyUSB0"
    power_cycle: bool = False
    poe_port_override: str = ""
    nic: str = ""
    host_ip: str = "192.168.1.10"
    device_ip: str = "192.168.1.20"
    tftp_port: int = 69
    nor_size: int = 0
    nand: bool = False
    wipe_env: bool = False
    final_reset: bool = True
    tftp_via: str = "auto"
    output: str = "human"
    debug: bool = False
