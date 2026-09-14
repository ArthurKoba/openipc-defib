"""Tests for u-boot env helpers (ethaddr default detection + rescue MAC)."""

import re

from defib.uboot_env import (
    OPENIPC_DEFAULT_ETHADDR,
    generate_locally_administered_mac,
    env_values_equivalent,
    expand_env_references,
    is_unset_or_default_ethaddr,
    parse_printenv,
    parse_printenv_value,
    select_install_ethaddr,
)

_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


class TestIsUnsetOrDefaultEthaddr:
    def test_none_is_default(self):
        assert is_unset_or_default_ethaddr(None) is True

    def test_empty_is_default(self):
        assert is_unset_or_default_ethaddr("") is True
        assert is_unset_or_default_ethaddr("   ") is True

    def test_openipc_default(self):
        assert is_unset_or_default_ethaddr("00:00:23:34:45:66") is True

    def test_openipc_default_uppercase(self):
        assert is_unset_or_default_ethaddr("00:00:23:34:45:66".upper()) is True

    def test_malformed_is_default(self):
        assert is_unset_or_default_ethaddr("not-a-mac") is True
        assert is_unset_or_default_ethaddr("00:00:23:34:45") is True
        assert is_unset_or_default_ethaddr("zz:zz:zz:zz:zz:zz") is True

    def test_real_macs_not_default(self):
        for mac in [
            "00:12:31:5e:e0:d2",   # HiSilicon OUI, real av200 MAC
            "02:ab:cd:ef:01:23",   # locally-administered
            "aa:bb:cc:dd:ee:ff",
        ]:
            assert is_unset_or_default_ethaddr(mac) is False, mac


class TestGenerateLocallyAdministeredMac:
    def test_format(self):
        mac = generate_locally_administered_mac()
        assert _MAC_RE.match(mac), f"bad format: {mac}"

    def test_locally_administered_bit_set(self):
        # Run many to be sure the bit-twiddle is correct regardless of randomness.
        for _ in range(200):
            mac = generate_locally_administered_mac()
            first = int(mac.split(":")[0], 16)
            assert first & 0x02, f"locally-administered bit not set in {mac}"

    def test_unicast_bit_clear(self):
        for _ in range(200):
            mac = generate_locally_administered_mac()
            first = int(mac.split(":")[0], 16)
            assert (first & 0x01) == 0, f"multicast bit set in {mac}"

    def test_not_default(self):
        # Should never collide with the OpenIPC default (locally-administered
        # bit makes that physically impossible — 00:00:23 has bit 1 == 0).
        for _ in range(200):
            assert generate_locally_administered_mac() != OPENIPC_DEFAULT_ETHADDR

    def test_uniqueness(self):
        macs = {generate_locally_administered_mac() for _ in range(100)}
        # 100 random MACs out of 2^46 possible → birthday collision negligible.
        assert len(macs) == 100


class TestParsePrintenvValue:
    def test_simple(self):
        assert parse_printenv_value("ethaddr=00:11:22:33:44:55\n", "ethaddr") == "00:11:22:33:44:55"

    def test_default_value(self):
        assert (
            parse_printenv_value("ethaddr=00:00:23:34:45:66\n", "ethaddr")
            == "00:00:23:34:45:66"
        )

    def test_with_prompt_around(self):
        resp = "hisilicon # printenv ethaddr\nethaddr=02:aa:bb:cc:dd:ee\nhisilicon # "
        assert parse_printenv_value(resp, "ethaddr") == "02:aa:bb:cc:dd:ee"

    def test_missing(self):
        # U-Boot reports "## Error: ..." for unset vars
        assert parse_printenv_value("## Error: \"ethaddr\" not defined\n", "ethaddr") is None

    def test_doesnt_match_substring(self):
        # 'eth' shouldn't match 'ethaddr' line
        assert parse_printenv_value("ethaddr=00:11:22:33:44:55\n", "eth") is None

    def test_multiple_vars(self):
        resp = "bootcmd=run abc\nethaddr=02:aa:bb:cc:dd:ee\nipaddr=192.168.1.10\n"
        assert parse_printenv_value(resp, "ethaddr") == "02:aa:bb:cc:dd:ee"
        assert parse_printenv_value(resp, "ipaddr") == "192.168.1.10"
        assert parse_printenv_value(resp, "bootcmd") == "run abc"


def test_default_const_is_what_we_observed():
    # Pin the constant to what we actually decompressed out of OpenIPC u-boot
    # binaries (hi3516av200 + hi3516cv300). If OpenIPC ever changes the
    # baked-in default, this test breaks loudly so we know to update.
    assert OPENIPC_DEFAULT_ETHADDR == "00:00:23:34:45:66"


def test_parse_full_printenv_preserves_shell_commands():
    response = (
        "OpenIPC # printenv\n"
        "bootcmd=sf probe 0; sf read ${baseaddr} 0x70000 0x380000; bootm ${baseaddr}\n"
        "bootargs=mem=128M console=ttyAMA0,115200 root=/dev/mtdblock5 rootfstype=squashfs ro\n"
        "ethaddr=18:68:cb:6b:e6:64\n"
        "Environment size: 1234/65532 bytes\n"
        "OpenIPC # \n"
    )
    env = parse_printenv(response)
    assert env["bootcmd"] == (
        "sf probe 0; sf read ${baseaddr} 0x70000 0x380000; bootm ${baseaddr}"
    )
    assert env["ethaddr"] == "18:68:cb:6b:e6:64"
    assert "mdio_intf" not in env


def test_parse_full_printenv_ignores_non_assignments_and_keeps_first_equals_in_value():
    response = "prompt\nfoo=a=b=c\n## Error: nope\nbar= baz qux \n"
    assert parse_printenv(response) == {"foo": "a=b=c", "bar": "baz qux"}


def test_expand_env_references_uses_captured_snapshot():
    env = {"baseaddr": "0x82000000"}
    assert expand_env_references(
        "sf probe 0; sf read ${baseaddr} 0x70000 0x380000; bootm ${baseaddr}",
        env,
    ) == "sf probe 0; sf read 0x82000000 0x70000 0x380000; bootm 0x82000000"


def test_env_values_equivalent_accepts_legacy_setenv_expansion():
    env = {"baseaddr": "0x82000000"}
    expected = "sf probe 0; sf read ${baseaddr} 0x70000 0x380000; bootm ${baseaddr}"
    actual = "sf probe 0; sf read 0x82000000 0x70000 0x380000; bootm 0x82000000"
    assert env_values_equivalent(expected, actual, env)


def test_env_values_equivalent_rejects_wrong_expanded_address():
    env = {"baseaddr": "0x82000000"}
    expected = "bootm ${baseaddr}"
    assert not env_values_equivalent(expected, "bootm 0x83000000", env)


def test_env_values_equivalent_does_not_hide_missing_reference():
    env = {}
    assert not env_values_equivalent("bootm ${baseaddr}", "bootm 0x82000000", env)


class TestSelectInstallEthaddr:
    def test_preserved_factory_mac_wins(self):
        value, source = select_install_ethaddr(
            "02:aa:bb:cc:dd:ee", "18:68:cb:6b:e6:64", allow_generate=False
        )
        assert value == "18:68:cb:6b:e6:64"
        assert source == "preserved"

    def test_current_valid_mac_is_kept_without_factory_value(self):
        value, source = select_install_ethaddr(
            "02:aa:bb:cc:dd:ee", None, allow_generate=False
        )
        assert value == "02:aa:bb:cc:dd:ee"
        assert source == "current"

    def test_vendor_install_refuses_to_invent_missing_identity(self):
        value, source = select_install_ethaddr(
            OPENIPC_DEFAULT_ETHADDR, None, allow_generate=False
        )
        assert value is None
        assert source == "missing"

    def test_generic_install_can_generate_rescue_mac(self):
        value, source = select_install_ethaddr(None, None, allow_generate=True)
        assert value is not None
        assert _MAC_RE.match(value)
        assert source == "generated"
