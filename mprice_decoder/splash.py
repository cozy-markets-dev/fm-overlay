"""A small Tk loading screen, run separately so library imports cannot freeze it."""
import json
import queue
import sys
import threading
from pathlib import Path


def run():
    import tkinter as tk

    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    root.withdraw()
    root.title("MaplePrice")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    picture = tk.PhotoImage(file=str(Path(__file__).with_name("assets") / "splash.png"))
    width, height = picture.width(), picture.height()
    root.geometry(f"{width}x{height}+{max(0, (root.winfo_screenwidth() - width) // 2)}"
                  f"+{max(0, (root.winfo_screenheight() - height) // 2)}")
    # Its rounded corners are see-through (splash.png, tools/splash_edges.py): so is this colour behind them.
    see_through = "#ff00fe"
    if sys.platform == "win32":
        root.attributes("-transparentcolor", see_through)
    canvas = tk.Canvas(root, width=width, height=height, highlightthickness=0, bg=see_through)
    canvas.pack()
    canvas.create_image(0, 0, anchor="nw", image=picture)
    status = canvas.create_text(36, 302, anchor="w", text="Getting MaplePrice ready...",
                                fill="#795c48", font=("Segoe UI", 10))
    canvas.create_rectangle(408, 298, 512, 306, fill="#eddcca", outline="")
    bar = canvas.create_rectangle(408, 298, 438, 306, fill="#ef8d46", outline="")
    messages = queue.Queue()

    def read():
        try:
            for line in sys.stdin:
                try:
                    messages.put(json.loads(line))
                except (ValueError, TypeError):
                    continue
        finally:
            messages.put(None)  # also close if the parent crashes or is terminated

    threading.Thread(target=read, daemon=True, name="splash-input").start()
    step = 0

    def tick():
        nonlocal step
        while True:
            try:
                message = messages.get_nowait()
            except queue.Empty:
                break
            if message is None:
                root.destroy()
                return
            if isinstance(message, str):
                canvas.itemconfigure(status, text=message[:100])
        step = (step + 2) % 148
        left = 408 + min(step, 148 - step)
        canvas.coords(bar, left, 298, left + 30, 306)
        root.after(30, tick)

    root.deiconify()
    tick()
    root.mainloop()


if __name__ == "__main__":
    run()
