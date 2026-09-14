"""Interfaces shared by stock U-Boot bootstrap implementations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from defib.recovery.events import ProgressEvent, RecoveryResult
from defib.transport.base import Transport

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[ProgressEvent], None]


@dataclass
class UBootBootstrapResult:
    """Result of reaching a flash-capable OpenIPC U-Boot shell."""

    recovery: RecoveryResult
    preserved_env: dict[str, str] = field(default_factory=dict)


class UBootBootstrap(Protocol):
    """Reusable path from a stock U-Boot console to OpenIPC U-Boot."""

    requires_echo_verification: bool

    async def bootstrap(
        self,
        transport: Transport,
        firmware: bytes,
        *,
        filename: str,
    ) -> UBootBootstrapResult: ...
