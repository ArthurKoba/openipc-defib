import asyncio
import zlib

import pytest

from defib.install.layout import (
    align_up,
    detect_nor_size_mb,
    erased_region_crc,
    nor_bootargs,
    nor_layout,
    nor_mtdparts,
    set_uboot_env_verified,
    uboot_flash_command_error,
)


def test_align_up_for_nand_page_write():
    assert align_up(0x12345, 2048) == 0x12800


def test_align_up_rejects_invalid_alignment():
    with pytest.raises(ValueError):
        align_up(1, 0)




def test_detect_nor_size_from_hisilicon_sf_probe_formats():
    assert detect_nor_size_mb('Spi(cs1): Block:64KB Chip:16MB Name:"GD25Q128"') == 16
    assert detect_nor_size_mb("spi size: 16MB") == 16
    assert detect_nor_size_mb("16384 KiB hi_sfc at 0:0 is now current device") == 16
    assert detect_nor_size_mb("unhelpful output") is None


def test_standard_nor_layout_is_selected_from_detected_capacity():
    assert nor_layout(8)["kernel"] == (0x050000, 0x200000)
    assert nor_layout(16)["rootfs"] == (0x350000, 0xA00000)
    assert nor_layout(32)["rootfs"] == (0x350000, 0x1800000)

def test_standard_openipc_nor_mtdparts():
    assert nor_mtdparts(8) == (
        "hi_sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)"
    )
    assert nor_mtdparts(16) == (
        "hi_sfc:256k(boot),64k(env),3072k(kernel),10240k(rootfs),-(rootfs_data)"
    )
    assert nor_mtdparts(32) == (
        "hi_sfc:256k(boot),64k(env),3072k(kernel),24576k(rootfs),-(rootfs_data)"
    )



def test_standard_openipc_nor_bootargs_keeps_osmem_symbolic():
    bootargs = nor_bootargs()
    assert "mem=${osmem}" in bootargs
    assert "root=/dev/mtdblock3" in bootargs
    assert "mtdparts=${mtdparts}" in bootargs
    assert "${extras}" in bootargs
    assert "32M" not in bootargs
    assert "256M" not in bootargs

def test_uboot_flash_error_detects_hisilicon_alignment_error():
    response = "ERROR: erase length is not block aligned!\n\nOpenIPC # "
    assert uboot_flash_command_error(response) is not None


def test_uboot_flash_error_detects_spi_write_failure():
    response = "SPI flash write failed\nOpenIPC # "
    assert uboot_flash_command_error(response) is not None


def test_uboot_flash_error_accepts_successful_progress():
    response = "Erasing at 0x240000 -- 100% complete.\nOpenIPC # "
    assert uboot_flash_command_error(response) is None


def test_erased_region_crc_matches_direct_crc():
    for size in (0, 1, 0x10000, 0x2B0000):
        assert erased_region_crc(size) == (zlib.crc32(b"\xff" * size) & 0xFFFFFFFF)


def test_runtime_tftp_env_is_read_back_and_retried_until_exact():
    calls: list[str] = []
    reads = iter(
        [
            "serverip=192.168.1.254\nOpenIPC # ",
            "serverip=192.168.1.11\nOpenIPC # ",
        ]
    )

    async def cmd(command: str, timeout: float = 0.0) -> str:
        calls.append(command)
        if command.startswith("printenv serverip"):
            return next(reads)
        return "OpenIPC # "

    asyncio.run(set_uboot_env_verified(cmd, "serverip", "192.168.1.11"))
    assert calls == [
        "setenv serverip 192.168.1.11",
        "printenv serverip",
        "setenv serverip 192.168.1.11",
        "printenv serverip",
    ]


def test_runtime_tftp_env_refuses_stale_serverip():
    async def cmd(command: str, timeout: float = 0.0) -> str:
        if command.startswith("printenv serverip"):
            return "serverip=192.168.1.254\nOpenIPC # "
        return "OpenIPC # "

    with pytest.raises(RuntimeError, match="serverip"):
        asyncio.run(
            set_uboot_env_verified(
                cmd, "serverip", "192.168.1.11", attempts=2
            )
        )
