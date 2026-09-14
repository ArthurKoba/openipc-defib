"""Final DS-I203 migration contracts.

The tests protect both the reusable Hikvision bootstrap outcomes and the full
stock-to-OpenIPC NOR install sequence around the published DDR U-Boot variant.
"""

from __future__ import annotations

import io
import sys
import tarfile
from dataclasses import dataclass
from types import ModuleType

import pytest

from defib.firmware import asset_name
from defib.install import InstallRequest
from defib.transport.base import Transport, TransportTimeout
from defib.uboot_env import select_install_ethaddr
from defib.vendors.hikvision import (
    HIKVISION_DEFAULT_TIMING,
    HikvisionUBootBootstrap,
    HikvisionUBootTiming,
)
from defib.vendors.registry import (
    create_uboot_bootstrap,
    get_stock_uboot_target,
    list_stock_uboot_variants,
)

FACTORY_MAC = "02:00:00:12:34:56"
SELECTOR = "hi3518ev100:hiwatch-ds-i203"


class ScriptedTransport(Transport):
    """Reactive UART that models the supported Hikvision/OpenIPC entry states."""

    def __init__(self, mode: str, *, mac: str | None = FACTORY_MAC) -> None:
        self.mode = mode
        self.mac = mac
        self.rx = bytearray()
        self.tx: list[bytes] = []
        self.closed = False
        self.break_seen = False
        self.ctrl_u_seen = False

    def feed(self, data: bytes) -> None:
        self.rx.extend(data)

    async def read(self, size: int, timeout: float | None = None) -> bytes:
        if self.closed:
            return b""
        if not self.rx:
            raise TransportTimeout("scripted transport has no data")
        data = bytes(self.rx[:size])
        del self.rx[:size]
        return data

    async def write(self, data: bytes) -> None:
        self.tx.append(bytes(data))
        if data == b"\x03\r":
            if self.mode == "openipc":
                self.feed(b"OpenIPC #")
            elif self.mode in {"stock", "stock-no-mac", "ymodem-fail", "go-fail"}:
                self.feed(b"HKVS #")
        elif data == b"\x03" and self.mode == "cold-stock" and not self.break_seen:
            self.break_seen = True
            self.feed(b"Hit Ctrl+u to stop autoboot:  3")
        elif data == b"\x15" and self.mode == "cold-stock" and not self.ctrl_u_seen:
            self.ctrl_u_seen = True
            self.feed(b"\r\nHKVS #")
        elif data == b"printenv ethaddr\r":
            if self.mac is None:
                self.feed(b"## Error: ethaddr not defined\r\nHKVS #")
            else:
                self.feed(f"ethaddr={self.mac}\r\nHKVS #".encode())
        elif data.startswith(b"go ") and self.mode != "go-fail":
            self.feed(b"\r\nOpenIPC #")

    async def flush_input(self) -> None:
        self.rx.clear()

    async def flush_output(self) -> None:
        pass

    async def bytes_waiting(self) -> int:
        return len(self.rx)

    async def unread(self, data: bytes) -> None:
        self.rx = bytearray(data) + self.rx

    async def close(self) -> None:
        self.closed = True

    @property
    def all_tx(self) -> bytes:
        return b"".join(self.tx)


@dataclass
class FakeStats:
    bytes_sent: int
    data_packets: int = 1
    retries: int = 0


@pytest.mark.asyncio
async def test_ds_i203_final_migration_contract_all_uboot_outcomes(
    monkeypatch, tmp_path
):
    target = get_stock_uboot_target(SELECTOR)
    assert target is not None

    # The selector chooses the vendor bootstrap and the compatible DDR U-Boot.
    # Camera runtime policy stays outside the release U-Boot, while Defib still
    # owns install-time transport and the flash layout it actually writes.
    assert get_stock_uboot_target("hi3518ev100") is None
    assert list_stock_uboot_variants("hi3518ev100") == ["hiwatch-ds-i203"]
    assert target.selector == SELECTOR
    assert target.display_name == "HiWatch DS-I203"
    assert target.vendor == "Hikvision"
    assert target.stock_uboot_name == "Hikvision U-Boot 2010.06"
    assert target.handler == "hikvision"
    assert target.load_address == 0x81000000

    artifact = asset_name(SELECTOR)
    assert artifact == "u-boot-hi3518ev100-ddr3-256m-universal.bin"
    assert target.transient_env == (("phyaddru", "3"),)

    bootstrap = create_uboot_bootstrap(
        target.handler,
        load_address=target.load_address,
    )
    assert isinstance(bootstrap, HikvisionUBootBootstrap)
    assert bootstrap.requires_echo_verification is True
    assert bootstrap.timing == HIKVISION_DEFAULT_TIMING
    assert HIKVISION_DEFAULT_TIMING.boot_timeout == 30.0
    assert HIKVISION_DEFAULT_TIMING.interrupt_duration == 1.0
    assert HIKVISION_DEFAULT_TIMING.openipc_timeout == 15.0

    fast_timing = HIKVISION_DEFAULT_TIMING.scaled(0.001)
    assert isinstance(fast_timing, HikvisionUBootTiming)
    assert fast_timing.ymodem_retries == HIKVISION_DEFAULT_TIMING.ymodem_retries

    def test_bootstrap() -> HikvisionUBootBootstrap:
        return HikvisionUBootBootstrap(
            load_address=target.load_address,
            timing=fast_timing,
        )

    # Exercise the actual install entry point far enough to prove the selector
    # chooses the vendor-U-Boot path without any board-profile subsystem.
    from defib.install import orchestrator

    firmware_tar = tmp_path / "openipc.hi3516cv100-nor-lite.tgz"
    with tarfile.open(firmware_tar, "w:gz") as archive:
        for name, payload in (
            ("uImage.hi3516cv100", b"kernel"),
            ("rootfs.squashfs.hi3516cv100", b"rootfs"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    uboot_override = tmp_path / artifact
    # Release assets are raw U-Boot binaries; the installer pads them to the
    # fixed 256 KiB boot partition before stock chainload and flashing.
    uboot_override.write_bytes(b"U" * 182580)

    class ReachedTransport(RuntimeError):
        pass

    async def stop_at_transport(port):
        raise ReachedTransport(port)

    serial_platform = ModuleType("defib.transport.serial_platform")
    serial_platform.create_transport = stop_at_transport
    serial_platform.normalize_port_name = lambda port: port
    monkeypatch.setitem(sys.modules, "defib.transport.serial_platform", serial_platform)
    with pytest.raises(ReachedTransport):
        await orchestrator.run_install(
            InstallRequest(
                chip=SELECTOR,
                firmware_path=str(firmware_tar),
                uboot_path=str(uboot_override),
                port="COM15",
                host_ip="192.168.1.11",
                device_ip="192.168.1.64",
                tftp_via="host",
                output="json",
            )
        )

    from defib.vendors import hikvision

    async def fake_send(self, firmware, *, filename, on_progress=None):
        self._transport.feed(b"\r\nHKVS #")
        if on_progress is not None:
            on_progress(len(firmware), len(firmware))
        return FakeStats(bytes_sent=len(firmware))

    async def fail_send(self, firmware, *, filename, on_progress=None):
        raise hikvision.YModemError("synthetic transfer failure")

    monkeypatch.setattr(hikvision.YModemSender, "send", fake_send)
    firmware = b"release-uboot" * 32

    async def check_existing_openipc() -> None:
        transport = ScriptedTransport("openipc")
        result = await test_bootstrap().bootstrap(
            transport, firmware, filename=artifact
        )
        assert result.recovery.success is True
        assert result.preserved_env == {}
        assert b"loady " not in transport.all_tx
        assert b"\x03\r" in transport.all_tx

    async def check_warm_stock() -> None:
        transport = ScriptedTransport("stock")
        result = await test_bootstrap().bootstrap(
            transport, firmware, filename=artifact
        )
        assert result.recovery.success is True
        assert result.preserved_env == {"ethaddr": FACTORY_MAC}
        assert b"printenv ethaddr\r" in transport.all_tx
        assert b"loady 0x81000000\r" in transport.all_tx
        assert b"go 0x81000000\r" in transport.all_tx

    async def check_cold_stock() -> None:
        transport = ScriptedTransport("cold-stock")
        result = await test_bootstrap().bootstrap(
            transport, firmware, filename=artifact
        )
        assert result.recovery.success is True
        assert transport.ctrl_u_seen is True
        assert b"\x15" in transport.all_tx
        assert result.preserved_env == {"ethaddr": FACTORY_MAC}

    async def check_missing_factory_mac() -> None:
        transport = ScriptedTransport("stock-no-mac", mac=None)
        result = await test_bootstrap().bootstrap(
            transport, firmware, filename=artifact
        )
        assert result.recovery.success is True
        assert result.preserved_env == {}
        selected, source = select_install_ethaddr(None, None, allow_generate=False)
        assert selected is None
        assert source == "missing"

    async def check_ymodem_failure() -> None:
        monkeypatch.setattr(hikvision.YModemSender, "send", fail_send)
        transport = ScriptedTransport("ymodem-fail")
        result = await test_bootstrap().bootstrap(
            transport, firmware, filename=artifact
        )
        assert result.recovery.success is False
        assert "YMODEM failed" in (result.recovery.error or "")
        monkeypatch.setattr(hikvision.YModemSender, "send", fake_send)

    async def check_go_failure() -> None:
        transport = ScriptedTransport("go-fail")
        result = await test_bootstrap().bootstrap(
            transport, firmware, filename=artifact
        )
        assert result.recovery.success is False
        assert result.recovery.error == "OpenIPC U-Boot prompt not detected after go"

    async def check_dead_console() -> None:
        transport = ScriptedTransport("dead")
        with pytest.raises(TimeoutError, match="neither OpenIPC nor Hikvision"):
            await test_bootstrap().bootstrap(
                transport, firmware, filename=artifact
            )

    await check_existing_openipc()
    await check_warm_stock()
    await check_cold_stock()
    await check_missing_factory_mac()
    await check_ymodem_failure()
    await check_go_failure()
    await check_dead_console()


@pytest.mark.asyncio
async def test_ds_i203_stock_install_persists_detected_layout_but_not_camera_policy(
    monkeypatch, tmp_path
):
    """Exercise the complete stock->OpenIPC NOR install contract.

    The published DDR variant is a raw U-Boot with generic environment defaults.
    Defib must pad it, use PHY3 only transiently for its own TFTP session, flash
    the detected 16 MiB layout, erase the old persistent environment so the new
    U-Boot loads its compiled defaults, and persist the layout it actually wrote
    plus the factory MAC. Camera policy such as osmem,
    Linux extras and sensor selection must remain outside Defib.
    """
    import zlib
    from contextlib import asynccontextmanager

    from defib.install import orchestrator
    from defib.install.layout import nor_mtdparts
    from defib.recovery.events import RecoveryResult
    from defib.vendors.base import UBootBootstrapResult

    artifact = asset_name(SELECTOR)
    assert artifact == "u-boot-hi3518ev100-ddr3-256m-universal.bin"

    kernel = b"K" * 0x18000
    rootfs = b"R" * 0x28000
    firmware_tar = tmp_path / "openipc.hi3516cv100-nor-lite.tgz"
    with tarfile.open(firmware_tar, "w:gz") as archive:
        for name, payload in (
            ("uImage.hi3516cv100", kernel),
            ("rootfs.squashfs.hi3516cv100", rootfs),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    raw_uboot = b"U" * 182580
    uboot_override = tmp_path / artifact
    uboot_override.write_bytes(raw_uboot)

    class InstallTransport(Transport):
        def __init__(self) -> None:
            self.rx = bytearray()
            self.closed = False

        async def read(self, size: int, timeout: float | None = None) -> bytes:
            if not self.rx:
                raise TransportTimeout("no scripted UART data")
            data = bytes(self.rx[:size])
            del self.rx[:size]
            return data

        async def write(self, data: bytes) -> None:
            if data == b"\x03":
                self.rx.extend(b"OpenIPC #")

        async def flush_input(self) -> None:
            self.rx.clear()

        async def flush_output(self) -> None:
            pass

        async def bytes_waiting(self) -> int:
            return len(self.rx)

        async def unread(self, data: bytes) -> None:
            self.rx = bytearray(data) + self.rx

        async def close(self) -> None:
            self.closed = True

    transport = InstallTransport()

    class FakeBootstrap:
        requires_echo_verification = False

        async def bootstrap(self, uart, firmware, *, filename):
            assert uart is transport
            assert filename == artifact
            assert len(firmware) == 0x40000
            assert firmware[: len(raw_uboot)] == raw_uboot
            assert firmware[len(raw_uboot):] == b"\xff" * (0x40000 - len(raw_uboot))
            return UBootBootstrapResult(
                recovery=RecoveryResult(success=True),
                preserved_env={"ethaddr": FACTORY_MAC},
            )

    async def fake_create_transport(port: str):
        assert port == "COM15"
        return transport

    @asynccontextmanager
    async def fake_temporary_ip(interface: str, ip: str, netmask: str):
        assert interface == "Ethernet"
        assert ip == "192.168.1.11"
        yield

    class FakeTFTPTransport:
        def close(self) -> None:
            pass

    class FakeTFTPProtocol:
        def __init__(self, files):
            self._files = dict(files)

    tftp_files: dict[str, bytes] = {}

    async def fake_start_tftp_server(*, files, bind_addr, port, done_count):
        assert bind_addr == "192.168.1.11"
        assert done_count == 3
        tftp_files.clear()
        tftp_files.update(files)
        return FakeTFTPTransport(), FakeTFTPProtocol(files)

    env: dict[str, str] = {"ethaddr": FACTORY_MAC}
    saved_env: dict[str, str] = {}
    commands: list[str] = []
    flash = bytearray(b"\xa5" * 0x1000000)
    ram = b""
    env_erased = False

    generic_defaults = {
        "osmem": "32M",
        "mtdparts": "hi_sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)",
        "bootcmd": "${bootcmdnor}",
    }

    async def fake_send_command(
        uart, command: str, timeout: float = 60.0, **kwargs
    ) -> str:
        nonlocal ram, env_erased, saved_env, env
        assert uart is transport
        commands.append(command)

        if command == "sf probe 0":
            return 'Spi(cs1): Block:64KB Chip:16MB Name:"GD25Q128"\nOpenIPC # '

        if command.startswith("setenv "):
            parts = command.split(" ", 2)
            key = parts[1]
            if len(parts) == 2 or parts[2] == "":
                env.pop(key, None)
            else:
                env[key] = parts[2]
            return "OpenIPC # "

        if command.startswith("printenv "):
            key = command.split(" ", 1)[1]
            if key in env:
                return f"{key}={env[key]}\nOpenIPC # "
            return f"## Error: {key} not defined\nOpenIPC # "

        if command.startswith(("tftpboot ", "tftp ")):
            name = command.split()[-1]
            ram = tftp_files[name]
            return f"Bytes transferred = {len(ram)}\nOpenIPC # "

        if command.startswith("sf erase "):
            _, _, off_text, size_text = command.split()
            off = int(off_text, 16)
            size = int(size_text, 16)
            flash[off:off + size] = b"\xff" * size
            if off == 0x40000 and size == 0x10000:
                env_erased = True
            return "Erasing at 0x0 -- 100% complete.\nOpenIPC # "

        if command.startswith("sf write "):
            _, _, _addr, off_text, size_text = command.split()
            off = int(off_text, 16)
            size = int(size_text, 16)
            flash[off:off + size] = ram[:size]
            return "Writing at 0x0 -- 100% complete.\nOpenIPC # "

        if command.startswith("sf read ") and ";" not in command:
            _, _, _addr, off_text, size_text = command.split()
            off = int(off_text, 16)
            size = int(size_text, 16)
            ram = bytes(flash[off:off + size])
            return "Read OK\nOpenIPC # "

        if command.startswith("sf read ") and "; crc32 " in command:
            read_part, crc_part = command.split(";", 1)
            _, _, _addr, off_text, size_text = read_part.split()
            off = int(off_text, 16)
            size = int(size_text, 16)
            ram = bytes(flash[off:off + size])
            crc_size = int(crc_part.split()[-1], 16)
            crc = zlib.crc32(ram[:crc_size]) & 0xFFFFFFFF
            return f"==> {crc:08X}\nOpenIPC # "

        if command.startswith("crc32 "):
            size = int(command.split()[-1], 16)
            crc = zlib.crc32(ram[:size]) & 0xFFFFFFFF
            return f"==> {crc:08X}\nOpenIPC # "

        if command == "saveenv":
            saved_env = dict(env)
            return "Saving Environment to SPI Flash... done\nOpenIPC # "

        if command == "reset":
            if env_erased:
                env = dict(generic_defaults)
                env_erased = False
            return "resetting..."

        return "OpenIPC # "

    import defib.flashdump
    import defib.network.ip_manager
    import defib.network.tftp_server
    import defib.vendors.registry

    serial_platform = ModuleType("defib.transport.serial_platform")
    serial_platform.create_transport = fake_create_transport
    serial_platform.normalize_port_name = lambda port: port
    monkeypatch.setitem(sys.modules, "defib.transport.serial_platform", serial_platform)

    monkeypatch.setattr(defib.flashdump, "send_command", fake_send_command)
    monkeypatch.setattr(defib.network.ip_manager, "temporary_ip", fake_temporary_ip)
    monkeypatch.setattr(
        defib.network.tftp_server, "start_tftp_server", fake_start_tftp_server
    )
    monkeypatch.setattr(
        defib.vendors.registry,
        "create_uboot_bootstrap",
        lambda *args, **kwargs: FakeBootstrap(),
    )

    await orchestrator.run_install(
        InstallRequest(
            chip=SELECTOR,
            firmware_path=str(firmware_tar),
            uboot_path=str(uboot_override),
            port="COM15",
            nic="Ethernet",
            host_ip="192.168.1.11",
            device_ip="192.168.1.64",
            tftp_via="host",
            output="json",
        )
    )

    expected_mtdparts = nor_mtdparts(16)

    # Installer-only PHY override must be established before the first TFTP.
    assert commands.index("setenv phyaddru 3") < commands.index("tftpboot u")
    assert "printenv phyaddru" in commands

    # The standard 16 MiB offsets selected from sf probe are what was written.
    assert flash[: len(raw_uboot)] == raw_uboot
    assert flash[len(raw_uboot):0x40000] == b"\xff" * (0x40000 - len(raw_uboot))
    assert flash[0x50000:0x50000 + len(kernel)] == kernel
    assert flash[0x350000:0x350000 + len(rootfs)] == rootfs
    assert flash[0xD50000:] == b"\xff" * (0x1000000 - 0xD50000)

    # Erasing the persistent env makes the freshly flashed U-Boot materialize
    # its generic compiled defaults. Defib then persists only the layout it
    # actually flashed and the factory identity; device policy stays generic
    # until the firmware profile customizer applies it in Linux.
    assert saved_env["ethaddr"] == FACTORY_MAC
    assert saved_env["mtdparts"] == expected_mtdparts
    assert saved_env["osmem"] == "32M"
    assert "phyaddru" not in saved_env
    assert "extras" not in saved_env
    assert "sensor" not in saved_env

    assert "sf erase 0x40000 0x10000" in commands
    assert any(
        cmd.startswith("sf read ") and " 0x40000 0x10000; crc32 " in cmd
        for cmd in commands
    )
    assert "setenv restore y" not in commands
    assert f"setenv mtdparts {expected_mtdparts}" in commands
    assert "printenv mtdparts" in commands
    assert not any(cmd.startswith("setenv osmem ") for cmd in commands)
    assert not any(cmd.startswith("setenv extras ") for cmd in commands)
    assert not any(cmd.startswith("setenv sensor ") for cmd in commands)
