from defib.recovery.ymodem import SOH, STX, crc16_xmodem, make_header, make_packet


def test_crc16_xmodem_reference_vector():
    assert crc16_xmodem(b"123456789") == 0x31C3


def test_make_1k_packet():
    payload = bytes(range(256)) * 4
    packet = make_packet(7, payload)
    assert packet[0] == STX
    assert packet[1:3] == bytes((7, 0xF8))
    assert len(packet) == 3 + 1024 + 2


def test_make_header_is_block_zero():
    packet = make_header("u-boot.bin", 182580)
    assert packet[0] == SOH
    assert packet[1:3] == b"\x00\xff"
    assert b"u-boot.bin\x00182580\x00" in packet
