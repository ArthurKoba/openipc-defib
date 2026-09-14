"""Helpers for U-Boot env handling during install.

The OpenIPC u-boot binaries ship with a compiled-in default env that
contains ``ethaddr=00:00:23:34:45:66``. When a camera boots with an
empty NAND env partition, u-boot loads that default into RAM. If anyone
then runs ``saveenv``, the bogus MAC is persisted to flash and from
then on every boot reads the same MAC. Multiple cameras converging on
``00:00:23:34:45:66`` is the visible symptom.

The mitigation here: detect the default (or missing) ``ethaddr`` and
replace it with a locally-administered random MAC before ``saveenv``.
"""

from __future__ import annotations

import re
import secrets

# CONFIG_ETHADDR baked into OpenIPC u-boot's default env. Found in the
# LZMA-compressed payload of u-boot-*-universal.bin (e.g. hi3516av200,
# hi3516cv300). Cameras whose env partition was empty when u-boot first
# saw them all converge on this MAC after the first saveenv.
OPENIPC_DEFAULT_ETHADDR = "00:00:23:34:45:66"

_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def is_unset_or_default_ethaddr(value: str | None) -> bool:
    """True if `value` is missing, blank, malformed, or the OpenIPC default."""
    if value is None:
        return True
    v = value.strip().lower()
    if not v:
        return True
    if not _MAC_RE.match(v):
        return True
    return v == OPENIPC_DEFAULT_ETHADDR.lower()


def generate_locally_administered_mac() -> str:
    """Generate a random unicast, locally-administered MAC.

    First octet has the locally-administered bit (bit 1) set and the
    multicast bit (bit 0) cleared, per IEEE 802. The remaining five
    octets are random. Always returns lowercase ``xx:xx:xx:xx:xx:xx``.
    """
    raw = bytearray(secrets.token_bytes(6))
    # Bit 0 (LSB of first octet): 0 = unicast.
    # Bit 1 (LSB+1):              1 = locally administered.
    raw[0] = (raw[0] & 0xFC) | 0x02
    return ":".join(f"{b:02x}" for b in raw)


def parse_printenv_value(response: str, var: str) -> str | None:
    """Pull the value of `var` out of a ``printenv VAR`` response.

    U-Boot prints lines like ``ethaddr=00:00:23:34:45:66`` (no quotes,
    one var per line). May be preceded/followed by prompt characters or
    download-mode framing. Returns the value or None if not found.
    """
    pattern = re.compile(rf"(?m)^\s*{re.escape(var)}=(.+?)\s*$")
    m = pattern.search(response)
    return m.group(1).strip() if m else None


def parse_printenv(response: str) -> dict[str, str]:
    """Parse a full ``printenv`` response into an environment mapping.

    Values are kept intact apart from surrounding whitespace, so command
    strings containing semicolons, ``${var}`` expansions, spaces, or extra
    equals signs remain valid. Non-assignment lines are ignored.
    """
    env: dict[str, str] = {}
    for raw_line in response.splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        env[key] = value.strip()
    return env

def expand_env_references(value: str, env: dict[str, str], *, max_passes: int = 8) -> str:
    """Expand U-Boot ``${name}`` references using a captured env snapshot.

    Verification needs semantic rather than byte-for-byte comparison because
    legacy U-Boot may expand variables while executing ``setenv``. Unknown
    references are intentionally left untouched so a missing variable cannot
    accidentally verify as an empty string. Expansion is bounded to avoid
    cycles in malformed environments.
    """
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
    result = value
    for _ in range(max_passes):
        changed = False

        def repl(match: re.Match[str]) -> str:
            nonlocal changed
            name = match.group(1)
            if name not in env:
                return match.group(0)
            changed = True
            return env[name]

        new_result = pattern.sub(repl, result)
        result = new_result
        if not changed:
            break
    return result


def env_values_equivalent(expected: str, actual: str | None, env: dict[str, str]) -> bool:
    """Compare environment values after resolving known U-Boot references.

    This keeps verification strict while accepting semantically identical
    representations such as ``${baseaddr}`` and ``0x82000000`` when the saved
    snapshot itself proves ``baseaddr=0x82000000``.
    """
    if actual is None:
        return False
    return expand_env_references(expected, env) == expand_env_references(actual, env)


def select_install_ethaddr(
    current: str | None,
    preserved: str | None,
    *,
    allow_generate: bool,
) -> tuple[str | None, str]:
    """Choose the MAC address that may be persisted by an install.

    A valid factory MAC captured before a vendor chainload takes precedence.
    Otherwise a valid current address is retained.  Generic boot-ROM installs
    may generate a rescue MAC; vendor-bootstrap installs can disable that so a
    missing factory identity fails safely instead of being replaced.
    """
    if not is_unset_or_default_ethaddr(preserved):
        assert preserved is not None
        return preserved.strip().lower(), "preserved"
    if not is_unset_or_default_ethaddr(current):
        assert current is not None
        return current.strip().lower(), "current"
    if allow_generate:
        return generate_locally_administered_mac(), "generated"
    return None, "missing"
