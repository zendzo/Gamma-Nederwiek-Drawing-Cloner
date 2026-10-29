"""Packaging assets must stay wired to each other.

The portable build fails in ways the Python suite cannot see - a spec that stops
bundling ``lisp/``, a build script that stops shipping ``Verify-Build.ps1``, or a
PowerShell script that reaches for a cmdlet a domain PC hides. These are cheap
static checks that catch exactly those regressions.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
SPEC = PACKAGING / "GammaNederwiek.spec"
BUILD = PACKAGING / "build_portable.ps1"
VERIFY = PACKAGING / "Verify-Build.ps1"
README = PACKAGING / "README-FIRST.txt"


class PackagingAssetTests(unittest.TestCase):
    def test_all_packaging_assets_exist(self):
        for path in (SPEC, BUILD, VERIFY, README):
            with self.subTest(asset=path.name):
                self.assertTrue(path.is_file(), f"missing packaging asset: {path}")

    def test_spec_bundles_every_runtime_data_folder(self):
        text = SPEC.read_text(encoding="utf-8")
        for required in ('"resources"', '"lisp"', '"VERSION"', '"revision"'):
            with self.subTest(data=required):
                self.assertIn(required, text)

    def test_spec_keeps_upx_off_and_the_app_windowed(self):
        text = SPEC.read_text(encoding="utf-8")
        self.assertIn("upx=False", text, "UPX is a top antivirus false-positive trigger")
        self.assertIn("console=False", text, "a console window must not appear")

    def test_build_ships_the_verifier_with_the_app(self):
        self.assertIn("Verify-Build.ps1", BUILD.read_text(encoding="utf-8"))

    def test_build_scans_the_finished_folder_before_zipping(self):
        text = BUILD.read_text(encoding="utf-8")
        self.assertIn("MpCmdRun", text, "a flagged build must never reach the share")
        self.assertIn("-DisableRemediation", text, "the scan must not delete the build")


class PowerShellRobustnessTests(unittest.TestCase):
    """Windows PowerShell 5.1 can hide the Utility/Security modules.

    On a machine whose ``PSModulePath`` lists the PowerShell 7 module tree first,
    ``Get-FileHash`` and ``Get-AuthenticodeSignature`` fail with
    CommandNotFoundException. Both scripts must avoid them.
    """

    @staticmethod
    def _code_without_comments(path):
        # The comments explain *why* those cmdlets are avoided; only real code
        # matters here.
        import re

        text = path.read_text(encoding="utf-8")
        text = re.sub(r"<#.*?#>", "", text, flags=re.S)
        return "\n".join(re.sub(r"#.*$", "", line) for line in text.splitlines())

    def test_scripts_do_not_call_hidden_cmdlets(self):
        for path in (BUILD, VERIFY):
            with self.subTest(script=path.name):
                code = self._code_without_comments(path)
                self.assertNotRegex(code, r"\bGet-FileHash\b")
                self.assertNotRegex(code, r"\bGet-AuthenticodeSignature\b")

    def test_build_hashes_with_dotnet_instead(self):
        self.assertIn("System.Security.Cryptography.SHA256", BUILD.read_text(encoding="utf-8"))

    def test_verify_script_can_unblock_and_knows_smart_app_control(self):
        text = VERIFY.read_text(encoding="utf-8")
        self.assertIn("Unblock-File", text)
        self.assertIn("VerifiedAndReputablePolicyState", text)

    def test_hash_helpers_survive_a_locked_executable(self):
        # A freshly unblocked exe is often held by Defender's real-time scan; a
        # plain File.OpenRead fails there with UnauthorizedAccessException.
        for path in (BUILD, VERIFY):
            with self.subTest(script=path.name):
                code = self._code_without_comments(path)
                self.assertIn("FileShare", code)
                self.assertIn("Start-Sleep", code, "the read must retry, not fail once")

    def test_verify_reports_a_lock_as_a_warning_not_a_mismatch(self):
        text = VERIFY.read_text(encoding="utf-8")
        self.assertIn("could not read GammaNederwiek.exe", text)
        self.assertIn("INCONCLUSIVE", text)

    def test_motw_helper_always_returns_a_countable_array(self):
        # PowerShell unrolls a returned collection, so `return ,@($list)` and
        # `return ,@()` double-wrap it: the caller sees ONE empty element and a
        # clean folder reports "1 file(s) carry Mark-of-the-Web". The helper
        # must return its list behind a single comma, and every call site must
        # pipe through ForEach-Object so @() yields the real file count.
        text = VERIFY.read_text(encoding="utf-8")
        code = self._code_without_comments(VERIFY)
        self.assertIn("return ,([string[]]$marked)", code)
        self.assertNotIn("return ,@($marked)", code)
        self.assertNotIn("return ,@()", code)

        calls = re.findall(r"@\(\s*Get-MotwFiles\s+\$\w+\s*\)", code)
        self.assertEqual(
            calls, [],
            "wrap Get-MotwFiles call sites as @(Get-MotwFiles $x | ForEach-Object { $_ })",
        )
        self.assertEqual(code.count("@(Get-MotwFiles $Path | ForEach-Object { $_ })"), 2)
        self.assertIn("function Get-MotwFiles", text)

    def test_helper_avoids_the_ambient_files_variable(self):
        # `$files` is an automatic [float] variable on some hosts; assigning an
        # array to it yields "0" (count 1), so a clean folder reported a block.
        text = VERIFY.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"\$files\s*=")
        self.assertIn("$candidates", text)

    def test_motw_detection_reads_the_stream_directly(self):
        # Get-Item -Stream cannot distinguish "absent" from "present" (it reports
        # absence via the error stream yet still returns an object), and
        # File.OpenRead("file:Zone.Identifier") throws "format is not supported",
        # so the check must go through FindFirstStreamW.
        text = VERIFY.read_text(encoding="utf-8")
        self.assertIn("function IsMarked", text)
        self.assertIn("FindFirstStreamW", text)
        self.assertIn("':Zone.Identifier*'", text)
        self.assertNotIn('OpenRead($FilePath +', text)
        self.assertNotIn("-Stream Zone.Identifier -ErrorAction Stop", text)

    def test_predicate_helpers_avoid_verb_noun_names(self):
        # PowerShell declares any function named verb-noun (e.g. Test-Foo) as a
        # *filter*, so `if (Test-Foo $x)` evaluates to the filter object - always
        # true - instead of calling it. Only functions whose result is used in a
        # condition must avoid that shape (output helpers like Write-Pass are
        # always called as bare statements, so they are fine).
        text = VERIFY.read_text(encoding="utf-8")
        names = re.findall(r"^function\s+([A-Za-z0-9_-]+)", text, flags=re.MULTILINE)
        verby = [n for n in names if "-" in n]
        self.assertTrue(verby, "expected at least one verb-noun helper")
        for name in verby:
            with self.subTest(function=name):
                used_in_condition = re.search(
                    r"(?m)^\s*(if|elseif|while)\s*\(\s*(?:-not\s+)?" + re.escape(name) + r"\b"
                    r"|\|\s*" + re.escape(name) + r"\b",
                    text,
                )
                self.assertIsNone(
                    used_in_condition,
                    f"{name} is verb-noun and used as a condition; rename it (IsMarked-style)",
                )

    def test_scripts_have_no_utf8_bom(self):
        # A BOM turns the leading `#Requires -Version 3.0` into an invalid
        # parameter name; Windows PowerShell then aborts the top-level script
        # silently at that line and every check after it never runs.
        for path in (BUILD, VERIFY):
            with self.subTest(script=path.name):
                head = path.read_bytes()[:3]
                self.assertNotEqual(head, b"\xef\xbb\xbf", f"{path.name} starts with a UTF-8 BOM")

    def test_scripts_do_not_concatenate_strings_as_command_arguments(self):
        # Write-Info ("text" + $x) binds as ONE array argument, so the line prints
        # blank and the value wraps onto the next line.
        for path in (BUILD, VERIFY):
            with self.subTest(script=path.name):
                code = self._code_without_comments(path)
                # A parenthesised *compound* expression is fine ("-" * 40);
                # a parenthesised concatenation is the trap.
                self.assertNotRegex(
                    code,
                    r"Write-(?:Info|Pass|Warn|Fail|Host)\s*\(\s*\"[^\"]*\"\s*\+",
                    'use "text $($value)" instead of Write-* ("text" + $value)',
                )

    def test_log_folder_comparison_is_normalised(self):
        # Comparing a logged `_internal` path with the folder by string prefix is
        # true even when the log came from another machine; compare normalised
        # full paths instead.
        text = VERIFY.read_text(encoding="utf-8")
        self.assertIn("function Test-SameFolder", text)
        self.assertIn("Test-SameFolder $logged $internal", text)
        # -Tail 1 is not the startup line: a newer selfcheck JSON line follows it,
        # so the folder comparison must search for the startup entry instead.
        code = self._code_without_comments(VERIFY)
        self.assertNotIn("-Tail 1", code)
        self.assertIn("startup version=", code)

    def test_verify_warns_about_one_cloud_synced_folder(self):
        text = VERIFY.read_text(encoding="utf-8")
        self.assertIn("inside OneDrive", text)


class ReadmeGuidanceTests(unittest.TestCase):
    def test_readme_warns_about_zip_mark_of_the_web(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("Verify-Build.ps1", text)
        self.assertIn("Mark-of-the-Web", text)
        self.assertIn("Never distribute this folder as a ZIP", text)

    def test_readme_keeps_the_autocad_and_font_notices(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("accoreconsole.exe", text)
        self.assertIn("OFL.txt", text)


if __name__ == "__main__":
    unittest.main()
