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

3. IF WINDOWS SHOWS "Windows protected your PC"

   That is the Mark-of-the-Web on a file that came from a download or e-mail,
   not a virus report. Either:

   - copy the folder from the internal file share instead, or
   - right-click GammaNederwiek.exe > Properties > tick "Unblock" > OK.

   If your PC is managed and the app is blocked by policy, ask IT to allow the
   publisher named in the signature instead of the file hash - the publisher
   rule keeps working after every update.

4. FIRST RUN

   - Fill in Source, Destination root, Bundle name and accoreconsole.exe.
     The accoreconsole.exe path is detected automatically when AutoCAD is
     installed in the default location.
   - Press Save, then work through the Setup tab from top to bottom.

5. SOMETHING WENT WRONG?

   Send the file logs\app.log together with build_info.json from this folder.
   Those two files identify the exact build and contain the error trace.

6. FONTS AND LICENSING

   This build bundles the Plus Jakarta Sans font (SIL Open Font License 1.1,
   see resources\fonts\OFL.txt inside _internal). The font is loaded for this
   application only - it is not installed into Windows.
