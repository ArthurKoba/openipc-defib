# HiWatch DS-I203 install path

`hi3518ev100:hiwatch-ds-i203` selects the compatible OpenIPC DDR3/256 MiB
U-Boot artifact and the reusable Hikvision stock-U-Boot bootstrap. It is not a
HiSilicon boot-ROM profile and it does not carry a private flash layout.

A stock camera exposes Hikvision U-Boot 2010.06. Defib stops that console,
chainloads the selected OpenIPC U-Boot with YMODEM, then performs the normal
TFTP flash/install sequence. An already-running OpenIPC U-Boot is detected and
the stock chainload step is skipped.

## Ownership boundary

The U-Boot release owns only boot-critical hardware initialization for this
variant:

- the DDR3/256 MiB cold-init register table embedded at offset `0x40`;
- a 256 MiB RAM probe ceiling so U-Boot can discover the full physical RAM.

The variant deliberately keeps the normal generic OpenIPC environment defaults.
It does not embed DS-I203 `osmem`, flash layout, PHY or sensor policy.

Defib resolves the release asset for the explicit selector:

```text
hi3518ev100:hiwatch-ds-i203
    -> u-boot-hi3518ev100-ddr3-256m-universal.bin
```

The published artifact is a raw U-Boot binary. Defib pads it with `0xFF` to the
fixed `0x40000` boot partition before both stock-U-Boot chainload and flashing.
`--uboot` remains available as an explicit local override for development or
recovery.

Defib owns migration mechanics and install invariants:

- `defib.vendors.hikvision` implements Ctrl+U / `HKVS #`, factory-MAC capture,
  `loady`, YMODEM, `go`, existing-OpenIPC detection and prompt confirmation;
- the vendor registry maps the DS-I203 selector to the Hikvision bootstrap,
  safe `0x81000000` chainload address and transient `phyaddru=3` required by the
  chainloaded U-Boot for installer TFTP;
- `defib.firmware` maps the selector to the published DDR3/256 MiB asset;
- `defib.install` owns runtime flash detection, standard layout selection,
  TFTP, flashing, CRC/readback verification, environment migration and reboot.

The transient PHY override is not saved as device policy. After flashing,
Defib erases the old persistent U-Boot environment before rebooting the newly
flashed loader. OpenIPC U-Boot then materializes its compiled defaults from the
erased environment, after which Defib persists only the install invariants it
must guarantee immediately (`mtdparts`) plus the factory `ethaddr`.

## Flash layout

DS-I203 uses the standard OpenIPC 16 MiB NOR layout; there is no private
DS-I203 layout in Defib:

```text
256k(boot),64k(env),3072k(kernel),10240k(rootfs),-(rootfs_data)
```

The installer obtains NOR capacity from `sf probe`, selects the standard
8/16/32 MiB layout, writes kernel/rootfs at those offsets, and saves the
matching `mtdparts` value before reboot. That persistence is required for the
first Linux boot because the generic U-Boot defaults describe the generic
8 MiB layout. `--nor-size` remains a manual override and is validated against
a detected capacity when both are available.

## Environment migration

Persistent environment from stock or an older OpenIPC U-Boot can shadow the
compiled defaults of a newly flashed U-Boot. For registered stock-U-Boot
migrations Defib therefore:

1. captures the factory `ethaddr`;
2. applies transient installer settings such as `phyaddru=3`;
3. flashes the detected standard NOR layout;
4. erases the old persistent U-Boot environment and resets into the newly
   flashed loader, which recreates its compiled defaults;
5. saves the detected-layout `mtdparts` and factory `ethaddr`.

Defib intentionally does not persist `osmem`, Linux `extras`, sensor selection
or other camera runtime policy. The DS-I203 firmware/device profile owns those
settings. With the generic U-Boot defaults, the first Linux boot therefore uses
`osmem=32M`; the profile can set `osmem=128M` and the remaining camera settings,
which take full effect after the subsequent reboot.

## Hardware acceptance

Validated end-to-end on a physical HiWatch DS-I203 using the DDR3/256 MiB
U-Boot variant and the matching DS-I203 firmware profile.

The tested migration path was:

    Hikvision U-Boot
      -> Ctrl+U / HKVS
      -> YMODEM temporary OpenIPC U-Boot
      -> TFTP flash with CRC/readback verification
      -> environment migration
      -> first OpenIPC boot
      -> device customizer
      -> automatic reboot
      -> final OpenIPC boot

The final boot detects 256 MiB physical RAM, runs Linux with 128 MiB and
128 MiB MMZ, uses the standard 16 MiB MTD layout, preserves the factory MAC,
brings up Ethernet on PHY address 3 / MDIO interface 0, selects IMX122, and
starts Majestic successfully.
