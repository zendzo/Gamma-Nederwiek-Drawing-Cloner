Gamma to Nederwiek - Revision Migration
Portable build - nothing to install
===================================

1. BEFORE YOU START

   AutoCAD must already be installed on this PC, because the migration runs
   through its Core Console engine (accoreconsole.exe). This application does
   not install or replace AutoCAD.

   Required: a 64-bit Windows 10/11 machine with AutoCAD installed.

2. STARTING THE APPLICATION

   Run GammaNederwiek.exe from this folder.

   - Do not run it directly from a network share. Copy the whole folder to a
     local disk first, for example C:\Apps\GammaNederwiek.
   - The folder must stay writable: config\settings.json and logs\ are created
     here on first run, and your Settings tab values are stored in them.
   - If the folder is read-only (for example under Program Files), the app still
     works but keeps its settings in
     %LOCALAPPDATA%\GammaNederwiek\config\settings.json instead.

3. IF WINDOWS WILL NOT START IT

   Run the check that ships with this folder:

       powershell -ExecutionPolicy Bypass -File .\Verify-Build.ps1

   It reports whether the copy is complete, whether Windows is blocking the
   file, and whether the folder can be written. If it reports Mark-of-the-Web
   (files that came from a zip or download), fix it with:

       powershell -ExecutionPolicy Bypass -File .\Verify-Build.ps1 -Fix

   Never distribute this folder as a ZIP. Files extracted from a zip inherit
   "Mark-of-the-Web", and Windows then shows

       "Microsoft Defender SmartScreen prevented an unrecognized app from
        starting"

   even when the app is perfectly fine. Copy the unzipped folder to the share
   instead, or run Verify-Build.ps1 -Fix after extracting.

   If the dialog has no "Run anyway" option, the machine is enforcing Smart App
   Control or an AppLocker policy. No local setting can bypass that: ask IT to
   allow the publisher named in the signature. IT should use a publisher rule,
   not a file hash - the hash changes on every update.

4. FIRST RUN

   - Fill in Source, Destination root, Bundle name and accoreconsole.exe.
     The accoreconsole.exe path is detected automatically when AutoCAD is
     installed in the default location.
   - Press Save, then work through the Setup tab from top to bottom.

5. SOMETHING WENT WRONG?

   Send logs\app.log and build_info.json from this folder. Those two files
   identify the exact build and contain the error trace. If the app does not
   start at all, send the output of Verify-Build.ps1 instead.

6. FONTS AND LICENSING

   This build bundles the Plus Jakarta Sans font (SIL Open Font License 1.1,
   see _internal\resources\fonts\OFL.txt). The font is loaded for this
   application only - it is not installed into Windows.
