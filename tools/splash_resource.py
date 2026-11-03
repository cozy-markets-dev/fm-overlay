"""Shared native loading-screen resources for the public Windows builds."""
from pathlib import Path

from PyInstaller.building.splash_templates import ipc_script, image_script


def splash_script():
    return ipc_script + image_script + r'''
package require Tk
wm withdraw .
wm title . MaplePrice
set width [image width splash_image]
set height [image height splash_image]
canvas .loading -width $width -height $height -highlightthickness 0 -borderwidth 0 -background magenta
.loading create image 0 0 -image splash_image -anchor nw
.loading create text 36 302 -anchor w -fill #8a674e -font {Segoe\ UI 10} -tags status
.loading create line 408 302 510 302 -fill #efdfc8 -width 5 -capstyle round
.loading create line 408 302 435 302 -fill #ee8a32 -width 5 -capstyle round -tags progress
proc show_status {args} {
    global status_text
    set allowed {
        {Getting MaplePrice ready...}
        {Preparing your price helper...}
        {Loading your saved prices...}
        {Preparing your market dashboard...}
        {Opening MaplePrice...}
        {Ready!}
    }
    if {$status_text in $allowed} {set text $status_text} else {set text {Getting MaplePrice ready...}}
    .loading itemconfigure status -text $text
}
trace add variable status_text write show_status
set status_text {Getting MaplePrice ready...}
set tick 0
proc animate_loading {} {
    global tick
    if {![winfo exists .loading]} {return}
    set left [expr {408 + 37 * (1 + sin($tick * 0.08))}]
    .loading coords progress $left 302 [expr {$left + 27}] 302
    incr tick
    after 33 animate_loading
}
pack .loading
wm overrideredirect . 1
wm attributes . -transparentcolor magenta -topmost 0
set x [expr {int(([winfo screenwidth .] - $width) / 2)}]
set y [expr {int(([winfo screenheight .] - $height) / 2)}]
wm geometry . +${x}+${y}
wm deiconify .
raise .
after 33 animate_loading
'''


def write_resource(destination, image, available):
    """Use PyInstaller's native splash format with the Windows runtime's Tcl/Tk."""
    from PyInstaller.archive.writers import SplashWriter

    requirements = [
        'tcl86t.dll', 'tk86t.dll', '_tcl_data\\init.tcl',
        '_tk_data\\license.terms', '_tk_data\\text.tcl', '_tk_data\\tk.tcl',
        '_tk_data\\ttk\\ttk.tcl', '_tk_data\\ttk\\fonts.tcl',
        '_tk_data\\ttk\\cursors.tcl', '_tk_data\\ttk\\utils.tcl',
    ]
    requirements.extend(name for name in ('vcruntime140.dll', 'vcruntime140_1.dll') if name in available)
    missing = set(requirements) - set(available)
    if missing:
        raise ValueError(f'Native loading-screen requirements are missing: {sorted(missing)}')
    SplashWriter(str(destination), requirements, 'tcl86t.dll', 'tk86t.dll',
                 '_tcl_data', '_tk_data', str(image), splash_script(), 0)
    return Path(destination).read_bytes()
