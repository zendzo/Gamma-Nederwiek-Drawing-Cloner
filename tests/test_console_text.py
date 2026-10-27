import unittest

from runtime.console_text import decode_console


class DecodeConsoleTests(unittest.TestCase):
    def test_none_is_empty_string(self):
        self.assertEqual(decode_console(None), "")

    def test_empty_bytes(self):
        self.assertEqual(decode_console(b""), "")

    def test_str_passes_through(self):
        self.assertEqual(decode_console("already text"), "already text")

    def test_utf16le_without_bom(self):
        # Core Console's normal output shape. The old shared guess used the
        # strict "utf-16" codec, which needs a BOM; this must still decode.
        payload = "GNI_BACKGROUND_ENGINE_0_3A_LOADED\r\n".encode("utf-16le")
        self.assertNotIn(b"\x00", payload[:1])
        self.assertEqual(
            decode_console(payload).strip(), "GNI_BACKGROUND_ENGINE_0_3A_LOADED"
        )

    def test_utf16le_with_bom(self):
        payload = "\ufeffGNI_OK".encode("utf-16le")
        self.assertTrue(payload.startswith(b"\xff\xfe"))
        self.assertEqual(decode_console(payload), "GNI_OK")

    def test_utf16be_with_bom(self):
        payload = "\ufeffGNI_OK".encode("utf-16be")
        self.assertEqual(decode_console(payload), "GNI_OK")

    def test_marker_is_matchable_in_utf16le(self):
        # The regression that matters: a marker check must survive decoding.
        marker = "GNI_STABLE_EXTERNAL_WRITER_0_4C_0_LOADED"
        decoded = decode_console(("noise\n" + marker + "\n").encode("utf-16le"))
        self.assertIn(marker, decoded)
        self.assertNotIn("\x00", decoded)

    def test_ascii_utf8(self):
        self.assertEqual(decode_console(b"plain ascii"), "plain ascii")

    def test_cp1252_fallback(self):
        # 0x96 is an en dash in cp1252 but invalid UTF-8.
        self.assertIn("\u2013", decode_console(b"a \x96 b"))

    def test_accepts_bytearray_and_memoryview(self):
        self.assertEqual(decode_console(bytearray(b"abc")), "abc")
        self.assertEqual(decode_console(memoryview(b"abc")), "abc")

    def test_mixed_pipes_decode_independently(self):
        # pdf_publisher used to guess one encoding for the concatenated buffer;
        # stdout (UTF-16LE) and stderr (ASCII) must each decode on their own.
        stdout = "GNI_PDF_PUBLISHER_OK".encode("utf-16le")
        stderr = b"warning"
        self.assertIn("GNI_PDF_PUBLISHER_OK", decode_console(stdout))
        self.assertEqual(decode_console(stderr), "warning")

    def test_never_raises_on_binary_garbage(self):
        self.assertIsInstance(decode_console(bytes(range(256))), str)


class SharedDecoderWiringTests(unittest.TestCase):
    """Guards against an engine quietly regrowing its own copy."""

    def test_every_engine_uses_the_shared_decoder(self):
        from revision.controlled_writer import ControlledRevisionWriter
        from revision.reopen_validator import StableReopenValidator
        from revision.stable_external_writer import StableExternalActionWriter
        from titleblock.discovery import TitleBlockDiscoveryEngine

        for owner in (
            StableExternalActionWriter,
            StableReopenValidator,
            TitleBlockDiscoveryEngine,
            ControlledRevisionWriter,
        ):
            with self.subTest(owner=owner.__name__):
                self.assertIs(owner._decode, decode_console)


if __name__ == "__main__":
    unittest.main()
