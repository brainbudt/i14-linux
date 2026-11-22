"""Offline shim tests, including real local AArch64 assembly/disassembly."""
import hashlib
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build-dtb-shim.py"
spec = importlib.util.spec_from_file_location("dtb_shim", SCRIPT)
shim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shim)


def dtb(start=0x44000000, end=0x44038E53, bootargs=None, display=False, reservation=True):
    args = shim.DTB_BOOTARGS if bootargs is None else bootargs
    chosen = [("linux,initrd-start", struct.pack(">Q", start)),
              ("linux,initrd-end", struct.pack(">Q", end)),
              ("bootargs", args.encode() + b"\0"),
              ("zimage64,cmdline-override", args.encode() + b"\0")]
    frame = [("compatible", b"simple-framebuffer\0"),
             ("status", b"okay\0"), ("reg", struct.pack(">4I", 0, 0xbf48e000, 0, 0x465000)),
             ("width", struct.pack(">I", 720)), ("height", struct.pack(">I", 1600)),
             ("stride", struct.pack(">I", 2880)), ("format", b"a8r8g8b8\0")]
    children = [("chosen", chosen, [("framebuffer@bf48e000", frame, [])] if display else [])]
    if display and reservation:
        children.append(("reserved-memory", [], [("framebuffer@bf010000",
            [("reg", struct.pack(">4I", 0, 0xbf010000, 0, 0xfb0000)), ("no-map", b"")], [])]))
    strings, offsets = bytearray(), {}
    def node(name, props, kids):
        data = bytearray(struct.pack(">I", 1) + name.encode() + b"\0")
        data.extend(b"\0" * (-len(data) % 4))
        for name, value in props:
            if name not in offsets:
                offsets[name] = len(strings)
                strings.extend(name.encode() + b"\0")
            data.extend(struct.pack(">III", 3, len(value), offsets[name]) + value)
            data.extend(b"\0" * (-len(data) % 4))
        for child in kids:
            data.extend(node(*child))
        data.extend(struct.pack(">I", 2))
        return data
    structure = node("", [], children) + struct.pack(">I", 9)
    total = 56 + len(structure) + len(strings)
    return (struct.pack(">10I", 0xD00DFEED, total, 56, 56 + len(structure), 40,
                        17, 16, 0, len(strings), len(structure))
            + b"\0" * 16 + structure + strings)


class DtbShimTests(unittest.TestCase):
    def test_display_profile_rejects_missing_framebuffer_or_reservation(self):
        for display, reservation in ((False, False), (True, False)):
            data = dtb(end=0x4404BB41, bootargs=shim.DTB_BOOTARGS + " consoleblank=0",
                       display=display, reservation=reservation)
            with self.subTest(display=display, reservation=reservation), self.assertRaises(shim.base.ImageError):
                shim.validate_diagnostic_dtb(data, hashlib.sha256(data).hexdigest(), display_ui=True)

    def test_display_userspace_requires_explicit_matching_initrd_profile(self):
        data = dtb(end=0x4404BB41, bootargs=shim.DTB_BOOTARGS + " consoleblank=0", display=True)
        digest = hashlib.sha256(data).hexdigest()
        with self.assertRaises(shim.base.ImageError):
            shim.validate_diagnostic_dtb(data, digest)
        result = shim.validate_diagnostic_dtb(data, digest, display_ui=True)
        self.assertEqual(result["initrd_end"], 0x4404BB41)
        blankable = dtb(end=0x4404BB41, display=True)
        with self.assertRaises(shim.base.ImageError):
            shim.validate_diagnostic_dtb(blankable, hashlib.sha256(blankable).hexdigest(), display_ui=True)
        old = dtb()
        with self.assertRaises(shim.base.ImageError):
            shim.validate_diagnostic_dtb(old, hashlib.sha256(old).hexdigest(), display_ui=True)

    def test_dtb_hash_and_exact_initrd_and_bootargs_are_required(self):
        good = dtb()
        result = shim.validate_diagnostic_dtb(good, hashlib.sha256(good).hexdigest())
        self.assertEqual(result["initrd_start"], 0x44000000)
        self.assertEqual(result["initrd_end"], 0x44038E53)
        with self.assertRaisesRegex(shim.base.ImageError, "hash mismatch"):
            shim.validate_diagnostic_dtb(good, "0" * 64)
        for bad in (dtb(start=0x43F00000), dtb(end=0x44038E54), dtb(bootargs="old arguments"), good[:-1]):
            with self.subTest(data=bad[-40:]), self.assertRaises(shim.base.ImageError):
                shim.validate_diagnostic_dtb(bad, hashlib.sha256(bad).hexdigest())

    def test_dtb_cannot_overlap_kernel_inside_fixed_prefix(self):
        with self.assertRaisesRegex(shim.base.ImageError, "prefix"):
            shim.check_embedded_size(shim.PREFIX_SIZE - shim.DTB_OFFSET + 1)
        shim.check_embedded_size(shim.PREFIX_SIZE - shim.DTB_OFFSET)

    def test_timer_optout_requires_explicit_persistent_build_mode(self):
        trial = dtb()
        persistent = dtb(bootargs=shim.DTB_BOOTARGS + " i14.trial_reboot=off")
        digest = lambda value: hashlib.sha256(value).hexdigest()
        with self.assertRaises(shim.base.ImageError):
            shim.validate_diagnostic_dtb(persistent, digest(persistent))
        with self.assertRaises(shim.base.ImageError):
            shim.validate_diagnostic_dtb(trial, digest(trial), persistent=True)
        result = shim.validate_diagnostic_dtb(persistent, digest(persistent), persistent=True)
        self.assertEqual(result["trial_reboot"], "explicitly disabled")

    def test_real_assembly_has_correct_header_data_offset_and_cache_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "test.dtb"
            data = dtb()
            path.write_bytes(data)
            info = {"image_size": 0x1810000, "file_bytes": 23844872}
            prefix, disassembly = shim.assemble_prefix(root, path, data, info)
            self.assertEqual(struct.unpack_from("<I", prefix)[0], 0x14000010)
            self.assertEqual(prefix[56:60], b"ARM\x64")
            self.assertEqual(struct.unpack_from("<QQQ", prefix, 8), (0, 0x1A10000, 10))
            self.assertEqual(prefix[shim.DTB_OFFSET:], data)
            for instruction in ("dc\tcivac", "ic\tiallu", "dsb\tsy", "isb", "br\tx4", "CurrentEL", "SCTLR_EL1", "SCTLR_EL2"):
                self.assertIn(instruction, disassembly)

    def test_assembly_with_unresolved_relocation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bad.S"
            source.write_text('.text\n.quad unknown_symbol\n')
            target = root / "bad.o"
            shim.base.run([shim.CLANG, "--target=aarch64-none-elf", "-c", source, "-o", target])
            with self.assertRaisesRegex(shim.base.ImageError, "relocation"):
                shim.require_no_relocations(target.read_bytes())

    def test_outer_image_rejects_changed_inner_image_or_placement(self):
        prefix = bytearray(shim.DTB_OFFSET + 73)
        struct.pack_into("<I", prefix, 0, 0x14000010)
        struct.pack_into("<QQQ", prefix, 8, 0, shim.PREFIX_SIZE + 128, 10)
        prefix[56:60] = b"ARM\x64"
        own_dtb = b"d" * 73
        prefix[shim.DTB_OFFSET:] = own_dtb
        inner = b"i" * 128
        outer = shim.combine_outer(bytes(prefix), inner, own_dtb, 128)
        self.assertEqual(outer[shim.PREFIX_SIZE:], inner)
        self.assertEqual(len(outer), shim.PREFIX_SIZE + 128)
        with self.assertRaises(shim.base.ImageError):
            shim.combine_outer(bytes(prefix), inner, b"x" * 73, 128)
        with self.assertRaises(shim.base.ImageError):
            shim.combine_outer(bytes(prefix), inner, own_dtb, 64)

    def test_existing_output_refused_before_any_source_read(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(shim.base.ImageError, "existing output"):
                shim.build(output, Path("absent.dtb"), "0" * 64)
            self.assertEqual(list(output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
