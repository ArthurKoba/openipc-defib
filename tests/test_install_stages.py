from __future__ import annotations

import pytest

from defib.install.model import INSTALL_STAGE_ORDER, resolve_install_stages


def test_default_stage_plan_preserves_production_flow() -> None:
    assert resolve_install_stages() == INSTALL_STAGE_ORDER


def test_no_final_reset_removes_reset_from_default_plan() -> None:
    assert resolve_install_stages(final_reset=False) == INSTALL_STAGE_ORDER[:-1]


def test_explicit_stage_selection_is_exact_and_ordered() -> None:
    assert resolve_install_stages(("env", "uboot")) == ("uboot", "env")


def test_explicit_env_stage_does_not_imply_final_reset() -> None:
    assert resolve_install_stages(("env",)) == ("env",)


def test_skip_stage_subtracts_from_production_plan() -> None:
    assert resolve_install_stages(skipped=("kernel", "rootfs")) == (
        "uboot",
        "rootfs-data",
        "env",
        "reset",
    )


def test_skip_env_stage_keeps_environment_out_of_the_plan() -> None:
    plan = resolve_install_stages(skipped=("env",))
    assert "uboot" in plan
    assert "env" not in plan


def test_stage_and_skip_stage_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="cannot be used together"):
        resolve_install_stages(("env",), ("kernel",))


def test_unknown_stage_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown install stage"):
        resolve_install_stages(("factory",))


def test_explicit_reset_conflicts_with_no_final_reset() -> None:
    with pytest.raises(ValueError, match="conflicts"):
        resolve_install_stages(("env", "reset"), final_reset=False)


def test_env_only_stage_skips_tftp_and_partition_writes(monkeypatch, tmp_path) -> None:
    import asyncio
    import io
    import sys
    import tarfile
    from contextlib import asynccontextmanager
    from types import ModuleType

    from defib.install import InstallRequest
    from defib.install.layout import erased_region_crc, nor_mtdparts
    from defib.recovery.events import RecoveryResult
    from defib.transport.base import Transport, TransportTimeout
    from defib.vendors.base import UBootBootstrapResult

    firmware_tar = tmp_path / "firmware.tgz"
    with tarfile.open(firmware_tar, "w:gz") as archive:
        for name, payload in (
            ("uImage.hi3518ev100", b"K" * 1024),
            ("rootfs.squashfs.hi3518ev100", b"R" * 2048),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    uboot = tmp_path / "u-boot.bin"
    uboot.write_bytes(b"U" * 1024)

    class FakeTransport(Transport):
        def __init__(self) -> None:
            self.rx = bytearray()
            self.closed = False

        async def read(self, size: int, timeout: float | None = None) -> bytes:
            if not self.rx:
                raise TransportTimeout("no data")
            data = bytes(self.rx[:size])
            del self.rx[:size]
            return data

        async def write(self, data: bytes) -> None:
            if b"\x03" in data:
                self.rx.extend(b"OpenIPC # ")

        async def flush_input(self) -> None:
            self.rx.clear()

        async def flush_output(self) -> None:
            return None

        async def bytes_waiting(self) -> int:
            return len(self.rx)

        async def close(self) -> None:
            self.closed = True

    transport = FakeTransport()
    commands: list[str] = []
    env = {
        "ethaddr": "02:00:00:12:34:56",
        "mtdparts": nor_mtdparts(16),
    }
    expected_erased_crc = erased_region_crc(0x10000)

    async def fake_send_command(
        transport_obj, command: str, timeout: float = 0.0, **kwargs
    ) -> str:
        commands.append(command)
        if command == "sf probe 0":
            return "Block: 64KB Chip: 16MB\nOpenIPC # "
        if command == "printenv ethaddr":
            return f"ethaddr={env['ethaddr']}\nOpenIPC # "
        if command == "printenv mtdparts":
            return f"mtdparts={env['mtdparts']}\nOpenIPC # "
        if command.startswith("setenv mtdparts "):
            env["mtdparts"] = command.removeprefix("setenv mtdparts ")
            return "OpenIPC # "
        if command.startswith("setenv ethaddr "):
            env["ethaddr"] = command.removeprefix("setenv ethaddr ")
            return "OpenIPC # "
        if command == "sf erase 0x40000 0x10000":
            return "Erasing at 0x40000 -- 100% complete.\nOpenIPC # "
        if command.startswith("sf read 0x82000000 0x40000 0x10000; crc32"):
            return f"==> {expected_erased_crc:08X}\nOpenIPC # "
        if command == "reset":
            return "resetting..."
        if command == "saveenv":
            return "Saving Environment to SPI Flash... done\nOpenIPC # "
        if command == "sf read 0x82000000 0x40000 0x10000":
            return "Read OK\nOpenIPC # "
        if command == "crc32 0x82000004 0xfffc 0x82010000":
            return "CRC32 for 82000004 ... 8200ffff ==> A1B2C3D4\nOpenIPC # "
        if command == "cmp.l 0x82000000 0x82010000 1":
            return "Total of 1 word(s) were the same\nOpenIPC # "
        return "OpenIPC # "

    async def fake_create_transport(port: str):
        return transport

    serial_platform = ModuleType("defib.transport.serial_platform")
    serial_platform.create_transport = fake_create_transport
    serial_platform.normalize_port_name = lambda port: port
    monkeypatch.setitem(sys.modules, "defib.transport.serial_platform", serial_platform)

    import defib.flashdump
    import defib.network.ip_manager
    import defib.network.tftp_server
    import defib.vendors.registry

    monkeypatch.setattr(defib.flashdump, "send_command", fake_send_command)

    @asynccontextmanager
    async def forbidden_temporary_ip(*args, **kwargs):
        raise AssertionError("env-only stage must not configure host TFTP networking")
        yield

    async def forbidden_tftp(*args, **kwargs):
        raise AssertionError("env-only stage must not start a TFTP server")

    monkeypatch.setattr(defib.network.ip_manager, "temporary_ip", forbidden_temporary_ip)
    monkeypatch.setattr(defib.network.tftp_server, "start_tftp_server", forbidden_tftp)

    class ExistingOpenIPCBootstrap:
        requires_echo_verification = True

        async def bootstrap(self, *args, **kwargs):
            return UBootBootstrapResult(
                recovery=RecoveryResult(success=True),
                chainloaded=False,
            )

    monkeypatch.setattr(
        defib.vendors.registry,
        "create_uboot_bootstrap",
        lambda *args, **kwargs: ExistingOpenIPCBootstrap(),
    )

    from defib.install.orchestrator import run_install

    asyncio.run(
        run_install(
            InstallRequest(
                chip="hi3518ev100:hiwatch-ds-i203",
                firmware_path=str(firmware_tar),
                uboot_path=str(uboot),
                port="COM15",
                wipe_env=True,
                stages=("env",),
                output="json",
            )
        )
    )

    assert "saveenv" in commands
    assert "sf erase 0x40000 0x10000" in commands
    assert not any(command.startswith("tftp") for command in commands)
    assert not any(command.startswith("sf write") for command in commands)
