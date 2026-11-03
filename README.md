# MaplePrice

Windows overlay for MapleStory Classic that prices Free Market shop listings and hovered items by reading the
screen (OCR), with prices from Cozy Markets and MeowDB.

This repo is the OCR-only app: the packet reader (FM Shop Listener) is not included.

- Run from source (Windows, Python 3.12, as admin for hotkeys): `py -3.12 -m pip install -r requirements.txt`, then `py -3.12 run.py`
- Tests: `py -3.12 -m pytest tests -q`
- Build: `build.bat` (PyInstaller, output `dist\MaplePrice.exe`)
