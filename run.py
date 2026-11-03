"""Launcher that works from any working directory (elevated processes start in System32)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if "--ui" in sys.argv[1:]:  # the clickable windows' own process (mprice_decoder/ui_child.py): nothing else loads
    from mprice_decoder import ui_child
    ui_child.run()

from mprice_decoder import startup

startup.run_splash_child()
startup.begin()
try:
    from mprice_decoder.__main__ import main
    main()
except BaseException:
    startup.finish("Startup stopped; see the error below.")
    raise
