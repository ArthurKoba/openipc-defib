"""Registry of reusable stock U-Boot bootstrap implementations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from defib.vendors.base import LogCallback, ProgressCallback, UBootBootstrap
from defib.vendors.hikvision import HikvisionUBootBootstrap

BootstrapFactory = Callable[..., UBootBootstrap]


@dataclass(frozen=True)
class StockUBootTarget:
    """Metadata needed to migrate from a vendor U-Boot console.

    ``transient_env`` contains runtime-only U-Boot settings required by the
    installer itself. They are not persistent board policy.
    """

    selector: str
    handler: str
    load_address: int
    display_name: str
    vendor: str
    stock_uboot_name: str
    transient_env: tuple[tuple[str, str], ...] = ()


_BOOTSTRAPS: dict[str, BootstrapFactory] = {
    "hikvision": HikvisionUBootBootstrap,
}

_STOCK_UBOOT_TARGETS: dict[str, StockUBootTarget] = {
    "hi3518ev100:hiwatch-ds-i203": StockUBootTarget(
        selector="hi3518ev100:hiwatch-ds-i203",
        handler="hikvision",
        load_address=0x81000000,
        display_name="HiWatch DS-I203",
        vendor="Hikvision",
        stock_uboot_name="Hikvision U-Boot 2010.06",
        # Needed only by the chainloaded OpenIPC U-Boot while Defib performs
        # TFTP. The published DDR variant deliberately keeps generic U-Boot
        # environment defaults; the device profile owns persistent PHY policy.
        transient_env=(("phyaddru", "3"),),
    ),
}


def get_stock_uboot_target(selector: str) -> StockUBootTarget | None:
    """Return migration metadata for an exact ``soc:variant`` selector."""
    return _STOCK_UBOOT_TARGETS.get(selector.lower())


def list_stock_uboot_selectors() -> list[str]:
    """List exact ``soc:variant`` selectors registered for vendor migration."""
    return sorted(target.selector for target in _STOCK_UBOOT_TARGETS.values())


def create_uboot_bootstrap(
    handler: str,
    *,
    load_address: int,
    on_log: LogCallback | None = None,
    on_progress: ProgressCallback | None = None,
) -> UBootBootstrap:
    """Instantiate a reusable stock U-Boot bootstrap implementation."""
    try:
        factory = _BOOTSTRAPS[handler]
    except KeyError as exc:
        known = ", ".join(sorted(_BOOTSTRAPS))
        raise ValueError(
            f"unknown stock U-Boot handler {handler!r}; known: {known}"
        ) from exc
    return factory(
        load_address=load_address,
        on_log=on_log,
        on_progress=on_progress,
    )
