"""Packaging assets must stay wired to each other.

The portable build fails in ways the Python suite cannot see - a spec that stops
bundling ``lisp/``, a build script that stops shipping ``Verify-Build.ps1``, or a
PowerShell script that reaches for a cmdlet a domain PC hides. These are cheap
static checks that catch exactly those regressions.
"""

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
