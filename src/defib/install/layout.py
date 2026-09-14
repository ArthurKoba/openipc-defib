"""Flash layouts and U-Boot helpers shared by install orchestration."""

from __future__ import annotations

import asyncio
import re
import zlib
from collections.abc import Awaitable, Callable

from defib.uboot_env import parse_printenv_value

Command = Callable[..., Awaitable[str]]


NOR8M_LAYOUT = {
    "boot": (0x000000, 0x40000),
    "env": (0x040000, 0x10000),
    "kernel": (0x050000, 0x200000),
    "rootfs": (0x250000, 0x500000),
}

NOR16M_LAYOUT = {
    "boot": (0x000000, 0x40000),
    "env": (0x040000, 0x10000),
    "kernel": (0x050000, 0x300000),
    "rootfs": (0x350000, 0xA00000),
}

NOR32M_LAYOUT = {
    "boot": (0x000000, 0x40000),
    "env": (0x040000, 0x10000),
    "kernel": (0x050000, 0x300000),
    "rootfs": (0x350000, 0x1800000),
}

NAND_LAYOUT = {
    "boot": (0x000000, 0x100000),
    "env": (0x100000, 0x100000),
    "kernel": (0x200000, 0x800000),
    "rootfs": (0xA00000, 0x7600000),
}


def align_up(value: int, alignment: int) -> int:
    if value < 0:
        raise ValueError("value must be non-negative")
    if alignment <= 0:
        raise ValueError("alignment must be positive")
    return ((value + alignment - 1) // alignment) * alignment


def erased_region_crc(size: int) -> int:
    if size < 0:
        raise ValueError("size must be non-negative")
    crc = 0
    chunk = b"\xff" * min(size, 0x10000)
    remaining = size
    while remaining:
        piece = chunk if remaining >= len(chunk) else chunk[:remaining]
        crc = zlib.crc32(piece, crc)
        remaining -= len(piece)
    return crc & 0xFFFFFFFF


def uboot_flash_command_error(response: str) -> str | None:
    text = response.lower()
    markers = (
        "error:",
        " failed",
        "failed",
        "failure",
        "out of range",
        "not block aligned",
        "unknown command",
        "usage: sf ",
        "usage: nand ",
    )
    for marker in markers:
        if marker in text:
            return marker.strip()
    return None


async def set_uboot_env_verified(
    cmd: Command,
    key: str,
    value: str,
    *,
    attempts: int = 3,
) -> None:
    """Set a transient U-Boot value and verify it from the live shell."""
    last_actual: str | None = None
    last_response = ""
    for attempt in range(max(1, attempts)):
        set_response = await cmd(f"setenv {key} {value}", timeout=3.0)
        last_response = set_response
        if uboot_flash_command_error(set_response) is None:
            verify_response = await cmd(f"printenv {key}", timeout=3.0)
            last_response = verify_response
            last_actual = parse_printenv_value(verify_response, key)
            if last_actual == value:
                return
        if attempt + 1 < max(1, attempts):
            await asyncio.sleep(0.05)

    raise RuntimeError(
        f"U-Boot runtime environment verify failed for {key}: "
        f"expected={value!r} got={last_actual!r}; "
        f"response={last_response.strip()[-160:]!r}"
    )


def nor_mtdparts(nor_size: int) -> str:
    if nor_size >= 32:
        return (
            "hi_sfc:256k(boot),64k(env),3072k(kernel),"
            "24576k(rootfs),-(rootfs_data)"
        )
    if nor_size >= 16:
        return (
            "hi_sfc:256k(boot),64k(env),3072k(kernel),"
            "10240k(rootfs),-(rootfs_data)"
        )
    return (
        "hi_sfc:256k(boot),64k(env),2048k(kernel),"
        "5120k(rootfs),-(rootfs_data)"
    )


def nor_bootargs() -> str:
    return (
        "mem=${osmem} console=ttyAMA0,115200 panic=20 "
        "root=/dev/mtdblock3 rootfstype=squashfs init=/init "
        "mtdparts=${mtdparts} ${extras}"
    )


def nand_bootargs(rootfs_is_ubi: bool) -> str:
    base = (
        "mem=256M console=ttyAMA0,115200 panic=20 ubi.mtd=3,2048 "
        "mtdparts=hinand:1024k(boot),1024k(env),8192k(kernel),-(ubi)"
    )
    if rootfs_is_ubi:
        return f"root=ubi0:rootfs rootfstype=ubifs {base}"
    return (
        "root=/dev/ubiblock0_0 rootfstype=squashfs ubi.block=0,0 "
        f"init=/init {base}"
    )


def parse_uboot_crc32(response: str) -> int | None:
    """Return the CRC printed by U-Boot, or ``None`` if it is missing."""
    match = re.search(r"==>\s*([0-9a-fA-F]{8})", response)
    return int(match.group(1), 16) if match else None


def detect_nor_size_mb(response: str) -> int | None:
    """Parse SPI NOR capacity from common HiSilicon U-Boot ``sf probe`` output."""
    patterns = (
        (r"\bChip:\s*(\d+)\s*MB\b", 1),
        (r"\bspi\s+size:\s*(\d+)\s*MB\b", 1),
        (r"\b(?:spi\s+nor\s+)?total\s+size:\s*(\d+)\s*MB\b", 1),
        (r"\bSF:[^\n]*\btotal\s+(\d+)\s*MB\b", 1),
        (r"\b(\d+)\s+MiB\b[^\n]*(?:hi_sfc|spi)", 1),
        (r"\b(\d+)\s+KiB\b[^\n]*(?:hi_sfc|spi)", 1024),
    )
    for pattern, divisor in patterns:
        match = re.search(pattern, response, re.IGNORECASE)
        if match:
            value = int(match.group(1))
            if divisor == 1:
                return value
            if value % divisor == 0:
                return value // divisor
    return None


def nor_layout(nor_size_mb: int) -> dict[str, tuple[int, int]]:
    """Return the standard OpenIPC NOR partition layout for a flash size."""
    if nor_size_mb >= 32:
        return NOR32M_LAYOUT
    if nor_size_mb >= 16:
        return NOR16M_LAYOUT
    return NOR8M_LAYOUT
