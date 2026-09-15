"""Focused tests for local serial transport TX-drain semantics."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

import pytest

from defib.transport.base import TransportTimeout
from defib.transport.serial import SerialTransport


@pytest.mark.asyncio
async def test_flush_output_waits_until_tx_queue_is_empty() -> None:
    port = MagicMock()
    values = iter((3, 1, 0))
    type(port).out_waiting = PropertyMock(side_effect=lambda: next(values))
    transport = SerialTransport(port)
    transport._OUTPUT_DRAIN_POLL = 0

    await transport.flush_output()

    port.reset_output_buffer.assert_not_called()
    port.flush.assert_not_called()


@pytest.mark.asyncio
async def test_flush_output_times_out_without_purging_tx_queue() -> None:
    port = MagicMock()
    type(port).out_waiting = PropertyMock(return_value=7)
    transport = SerialTransport(port)
    transport._OUTPUT_DRAIN_TIMEOUT = 0
    transport._OUTPUT_DRAIN_POLL = 0

    with pytest.raises(TransportTimeout, match="7 byte"):
        await transport.flush_output()

    port.reset_output_buffer.assert_not_called()
    port.flush.assert_not_called()
