@echo off
rem Builds dist\MaplePrice.exe: one file for Windows 10/11 with the shop overlay and
rem its React windows (F10, price alerts: mprice_decoder\assets\ui). Asks for admin,
rem needed for hotkeys while the game has focus. Exit MaplePrice from the F10 options window.
rem No UPX: compressed exes trip antivirus false positives far more often.
rem Every item's icon goes in too (mprice_decoder\assets\icons.zip); refresh it after a game patch
rem with: py -3.12 -m mprice_decoder.iconpack
if exist mprice_decoder\assets\private-upload.json (
  echo A private upload credential is present: remove it before building a public MaplePrice release.
  exit /b 1
)
if exist mprice_decoder\assets\icons.zip.partial (
  echo The icon pack's download isn't finished: run "py -3.12 -m mprice_decoder.iconpack" again first.
  exit /b 1
)
if not exist mprice_decoder\assets\icons.zip echo No icon pack: this build will download icons as it needs them.
py -3.12 -m PyInstaller --noconfirm --clean MaplePrice.spec
