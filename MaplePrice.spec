# Public Windows release: native Cozy loading screen and no console window.
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
sys.path.insert(0, str(root / 'tools'))
from splash_resource import splash_script

ocr_data, ocr_binaries, ocr_imports = collect_all('rapidocr_onnxruntime')
# F10 and adding an alert: pywebview (Edge WebView2 through pythonnet) shows mprice_decoder/assets/ui (ui_child.py)
web_data, web_binaries, web_imports = collect_all('webview')
hidden = [
    'pyi_splash',
    'argparse', 'copy', 'csv', 'ctypes', 'datetime', 'hashlib', 'queue', 'signal',
    'socket', 'sqlite3', 'subprocess', 'urllib.error', 'urllib.parse', 'urllib.request', 'uuid',
]
a = Analysis(
    [str(root / 'run.py')], pathex=[],
    binaries=ocr_binaries + web_binaries, hiddenimports=hidden + ocr_imports + web_imports + ['clr'],
    datas=ocr_data + web_data + [
        (str(root / 'mprice_decoder/assets'), 'mprice_decoder/assets'),
    ],
)
pyz = PYZ(a.pure)
splash = Splash(str(root / 'mprice_decoder/assets/splash.png'), a.binaries, a.datas,
                always_on_top=False, minify_script=False, max_img_size=(560, 350))
splash.script = splash_script()
splash.assemble()
exe = EXE(pyz, a.scripts, splash, splash.binaries, a.binaries, a.datas, [],
          name='MaplePrice', console=False, debug=False, upx=False, uac_admin=True,
          icon=str(root / 'mprice_decoder/assets/mapleprice.ico'))
