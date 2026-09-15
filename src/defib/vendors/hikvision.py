"""Hikvision legacy U-Boot bootstrap used by Hikvision cameras."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum

from defib.flashdump import write_uboot_line_with_echo_verify
from defib.recovery.events import ProgressEvent, RecoveryResult, Stage
from defib.recovery.ymodem import YModemError, YModemSender
from defib.transport.base import Transport, TransportTimeout
from defib.uboot_env import is_unset_or_default_ethaddr, parse_printenv_value
from defib.vendors.base import LogCallback, ProgressCallback, UBootBootstrapResult


@dataclass(frozen=True)
class HikvisionUBootTiming:
    """Timing policy for the legacy Hikvision console handshake.

    Production uses :data:`HIKVISION_DEFAULT_TIMING`. Tests can pass a scaled
    copy so they exercise the identical state machine without waiting for
    hardware-sized UART windows.
    """

    warm_probe_timeout: float = 1.0
    boot_timeout: float = 30.0
    prompt_timeout: float = 8.0
    break_interval: float = 0.1
    read_timeout: float = 0.1
    interrupt_interval: float = 0.05
    interrupt_duration: float = 1.0
    ymodem_control_timeout: float = 5.0
    ymodem_start_timeout: float = 15.0
    openipc_timeout: float = 15.0
    openipc_poll_interval: float = 0.05
    ymodem_retries: int = 32

    def scaled(self, factor: float) -> HikvisionUBootTiming:
        """Return the same policy with wall-clock durations scaled by *factor*."""
        if factor <= 0:
            raise ValueError("timing scale must be greater than zero")
        return HikvisionUBootTiming(
            warm_probe_timeout=self.warm_probe_timeout * factor,
            boot_timeout=self.boot_timeout * factor,
            prompt_timeout=self.prompt_timeout * factor,
            break_interval=self.break_interval * factor,
            read_timeout=self.read_timeout * factor,
            interrupt_interval=self.interrupt_interval * factor,
            interrupt_duration=self.interrupt_duration * factor,
            ymodem_control_timeout=self.ymodem_control_timeout * factor,
            ymodem_start_timeout=self.ymodem_start_timeout * factor,
            openipc_timeout=self.openipc_timeout * factor,
            openipc_poll_interval=self.openipc_poll_interval * factor,
            ymodem_retries=self.ymodem_retries,
        )


HIKVISION_DEFAULT_TIMING = HikvisionUBootTiming()


class _ConsoleMode(Enum):
    OPENIPC = "openipc"
    STOCK = "stock"


class HikvisionUBootBootstrap:
    """Reach OpenIPC U-Boot through Hikvision's stock ``HKVS #`` console.

    Protocol details live here rather than in device profiles. A target only
    selects this handler and supplies a safe RAM load address.
    """

    requires_echo_verification = True

    _STOCK_PROMPT = b"HKVS #"
    _AUTOBOOT_PROMPT = b"hit ctrl+u to stop autoboot"
    _INTERRUPT = b"\x15"  # Ctrl+U
    _OPENIPC_PROMPTS = (b"OpenIPC #", b"hisilicon #", b"\n=> ")

    def __init__(
        self,
        load_address: int,
        *,
        on_log: LogCallback | None = None,
        on_progress: ProgressCallback | None = None,
        timing: HikvisionUBootTiming | None = None,
    ) -> None:
        self.load_address = load_address
        self.on_log = on_log
        self.on_progress = on_progress
        self.timing = timing or HIKVISION_DEFAULT_TIMING

    def _log(self, message: str) -> None:
        if self.on_log is not None:
            self.on_log(message)

    def _progress(self, sent: int, total: int, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(
                ProgressEvent(
                    stage=Stage.UBOOT,
                    bytes_sent=sent,
                    bytes_total=total,
                    message=message,
                )
            )

    async def _read_until(
        self,
        transport: Transport,
        needles: tuple[bytes, ...],
        timeout: float,
        *,
        capture: bytearray | None = None,
    ) -> bytes:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        buf = bytearray()
        while loop.time() < deadline:
            try:
                chunk = await transport.read(
                    256,
                    timeout=min(
                        self.timing.read_timeout, max(0.0, deadline - loop.time())
                    ),
                )
            except TransportTimeout:
                await asyncio.sleep(
                    min(
                        self.timing.openipc_poll_interval,
                        max(0.0, deadline - loop.time()),
                    )
                )
                continue
            if not chunk:
                continue
            buf.extend(chunk)
            if capture is not None:
                capture.extend(chunk)
            tail = bytes(buf[-4096:])
            if any(needle in tail for needle in needles):
                return bytes(buf)
        names = " / ".join(needle.decode("ascii", "replace") for needle in needles)
        raise TimeoutError(f"timed out waiting for {names}")

    @classmethod
    def _openipc_present(cls, data: bytes) -> bool:
        return any(prompt in data for prompt in cls._OPENIPC_PROMPTS)

    async def _detect_console(
        self, transport: Transport
    ) -> tuple[_ConsoleMode, bytes]:
        """Return the active U-Boot console after stopping autoboot."""
        capture = bytearray()

        # A reopened UART does not replay the prompt. Ask the current shell to
        # print one before waiting for a fresh boot banner.
        try:
            await transport.write(b"\x03\r")
            await transport.flush_output()
            warm = await self._read_until(
                transport,
                (self._STOCK_PROMPT, *self._OPENIPC_PROMPTS),
                self.timing.warm_probe_timeout,
                capture=capture,
            )
            if self._openipc_present(warm):
                self._log("Existing OpenIPC U-Boot prompt detected; skipping stock chainload")
                return _ConsoleMode.OPENIPC, bytes(capture)
            if self._STOCK_PROMPT in warm:
                self._log("Hikvision U-Boot prompt detected: HKVS # (warm attach)")
                return _ConsoleMode.STOCK, bytes(capture)
        except TimeoutError:
            pass

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timing.boot_timeout
        window = bytearray()
        last_break = 0.0

        while loop.time() < deadline:
            # Ctrl-C catches an already-migrated OpenIPC autoboot. Hikvision's
            # own autoboot gate ignores it and waits for Ctrl+U instead.
            now = loop.time()
            if now - last_break >= self.timing.break_interval:
                await transport.write(b"\x03")
                await transport.flush_output()
                last_break = now

            try:
                chunk = await transport.read(256, timeout=self.timing.read_timeout)
            except TransportTimeout:
                await asyncio.sleep(self.timing.openipc_poll_interval)
                continue
            if not chunk:
                continue

            capture.extend(chunk)
            window.extend(chunk)
            if len(window) > 4096:
                del window[:-4096]
            tail = bytes(window)

            if self._openipc_present(tail):
                self._log("Existing OpenIPC U-Boot prompt detected; skipping stock chainload")
                return _ConsoleMode.OPENIPC, bytes(capture)
            if self._STOCK_PROMPT in tail:
                self._log("Hikvision U-Boot prompt detected: HKVS #")
                return _ConsoleMode.STOCK, bytes(capture)
            if self._AUTOBOOT_PROMPT in tail.lower():
                self._log("Hikvision autoboot detected; sending Ctrl+U")
                end = loop.time() + self.timing.interrupt_duration
                while loop.time() < end:
                    await transport.write(self._INTERRUPT)
                    await transport.flush_output()
                    await asyncio.sleep(self.timing.interrupt_interval)
                stopped = await self._read_until(
                    transport,
                    (self._STOCK_PROMPT, *self._OPENIPC_PROMPTS),
                    self.timing.prompt_timeout,
                    capture=capture,
                )
                if self._openipc_present(stopped):
                    self._log(
                        "Existing OpenIPC U-Boot prompt detected; skipping stock chainload"
                    )
                    return _ConsoleMode.OPENIPC, bytes(capture)
                self._log("Hikvision U-Boot prompt detected: HKVS #")
                return _ConsoleMode.STOCK, bytes(capture)

        raise TimeoutError(
            "neither OpenIPC nor Hikvision U-Boot was detected; start with the "
            "camera off, then power it on"
        )

    async def _run_stock_command(
        self, transport: Transport, command: str, *, timeout: float | None = None
    ) -> str:
        await write_uboot_line_with_echo_verify(transport, command)
        response = await self._read_until(
            transport,
            (self._STOCK_PROMPT,),
            timeout or self.timing.prompt_timeout,
        )
        return response.decode("ascii", errors="replace")

    async def _chainload(
        self,
        transport: Transport,
        firmware: bytes,
        *,
        filename: str,
    ) -> RecoveryResult:
        start = time.monotonic()
        address_text = f"0x{self.load_address:08x}"
        load_cmd = f"loady {address_text}"
        self._log(f"Hikvision U-Boot: {load_cmd}")
        await write_uboot_line_with_echo_verify(transport, load_cmd)

        # Drop the echoed command / loady banner before waiting for YMODEM's
        # periodic CRC request.  Otherwise an unrelated uppercase 'C' in that
        # text can be mistaken for the 0x43 protocol byte.
        await transport.flush_input()

        sender = YModemSender(
            transport,
            control_timeout=self.timing.ymodem_control_timeout,
            start_timeout=self.timing.ymodem_start_timeout,
            packet_retries=self.timing.ymodem_retries,
            on_log=self._log,
        )
        try:
            stats = await sender.send(
                firmware,
                filename=filename,
                on_progress=lambda sent, total: self._progress(
                    sent, total, f"YMODEM {sent}/{total}"
                ),
            )
        except YModemError as exc:
            return RecoveryResult(
                success=False,
                error=f"Hikvision U-Boot YMODEM failed: {exc}",
                elapsed_ms=(time.monotonic() - start) * 1000,
            )

        self._log(
            f"YMODEM complete: {stats.bytes_sent} bytes, "
            f"{stats.data_packets} packets, {stats.retries} retries"
        )

        try:
            await self._read_until(
                transport, (self._STOCK_PROMPT,), self.timing.prompt_timeout
            )
        except TimeoutError as exc:
            return RecoveryResult(
                success=False,
                error=f"Hikvision prompt did not return after YMODEM: {exc}",
                elapsed_ms=(time.monotonic() - start) * 1000,
            )

        start_cmd = f"go {address_text}"
        self._log(f"Hikvision U-Boot: {start_cmd}")
        await write_uboot_line_with_echo_verify(transport, start_cmd)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timing.openipc_timeout
        capture = bytearray()
        while loop.time() < deadline:
            await transport.write(b"\x03")
            await transport.flush_output()
            try:
                chunk = await transport.read(256, timeout=self.timing.read_timeout)
            except TransportTimeout:
                await asyncio.sleep(self.timing.openipc_poll_interval)
                continue
            if chunk:
                capture.extend(chunk)
                if self._openipc_present(bytes(capture[-4096:])):
                    elapsed = (time.monotonic() - start) * 1000
                    return RecoveryResult(
                        success=True,
                        stages_completed=[Stage.UBOOT, Stage.COMPLETE],
                        elapsed_ms=elapsed,
                        post_burn_buffer=bytes(capture),
                    )
            await asyncio.sleep(self.timing.openipc_poll_interval)

        return RecoveryResult(
            success=False,
            error="OpenIPC U-Boot prompt not detected after go",
            elapsed_ms=(time.monotonic() - start) * 1000,
            post_burn_buffer=bytes(capture),
        )

    async def bootstrap(
        self,
        transport: Transport,
        firmware: bytes,
        *,
        filename: str,
    ) -> UBootBootstrapResult:
        """Attach to OpenIPC or chainload it from stock Hikvision U-Boot."""
        mode, capture = await self._detect_console(transport)
        if mode is _ConsoleMode.OPENIPC:
            return UBootBootstrapResult(
                recovery=RecoveryResult(
                    success=True,
                    elapsed_ms=0.0,
                    post_burn_buffer=capture,
                ),
                chainloaded=False,
            )

        preserved_env: dict[str, str] = {}
        reply = await self._run_stock_command(transport, "printenv ethaddr")
        ethaddr = parse_printenv_value(reply, "ethaddr")
        if ethaddr and not is_unset_or_default_ethaddr(ethaddr):
            preserved_env["ethaddr"] = ethaddr.lower()
            self._log(f"Preserved stock ethaddr={ethaddr.lower()}")
        else:
            self._log(
                "Stock ethaddr is missing or invalid; install will refuse to "
                "invent a factory MAC"
            )

        recovery = await self._chainload(transport, firmware, filename=filename)
        return UBootBootstrapResult(
            recovery=recovery,
            preserved_env=preserved_env,
            chainloaded=True,
        )
