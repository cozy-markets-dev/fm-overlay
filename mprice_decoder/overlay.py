"""Transparent, click-through, always-on-top window drawn over the game."""
import base64
import ctypes
import tkinter as tk
import tkinter.font as tkfont

from . import theme
from .alerts import mesos
from .i18n import t
from .annotate import alert_panel, hover_panel, shrink
from .render import Scaling, draw_badge, draw_labels, draw_panel, scaled_png
from .settings import HOTKEYS
from .identity import DISPLAY_NAME

KEY = "#ff00fe"  # color-keyed to fully transparent

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x80000
WS_EX_TRANSPARENT = 0x20
WS_EX_TOOLWINDOW = 0x80
WS_EX_NOACTIVATE = 0x8000000
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x10
SWP_SHOWWINDOW = 0x40


class Overlay:
    def __init__(self, root):
        self.root = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", KEY)
        root.configure(bg=KEY)
        self.canvas = tk.Canvas(root, bg=KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        theme.load_fonts()
        self.size = 1  # what it draws, this many times its own size (settings.overlay_scale)
        self._fonts = {}  # size -> {theme.FONTS name: Tk font}
        self.fonts = self.fonts_at(1)
        self.images = []  # PhotoImages on the canvas (icons)
        self.alert_images = []  # the alert cards': they outlive clear()
        self._flash_job = self._alert_job = None
        self.rect = None  # where place() last put us
        root.update_idletasks()
        self._click_through()

    def fonts_at(self, size):
        if size not in self._fonts:
            self._fonts[size] = {name: tkfont.Font(root=self.root, font=theme.tk_font(name, size)) for name in theme.FONTS}
        return self._fonts[size]

    def _click_through(self):
        user32 = ctypes.windll.user32
        self.hwnd = hwnd = user32.GetParent(self.root.winfo_id())
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        )

    def place(self, rect):
        self.rect = rect
        left, top, width, height = rect
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        # Tk's geometry() alone doesn't move an overrideredirect window here; do it natively.
        ctypes.windll.user32.SetWindowPos(
            self.hwnd, HWND_TOPMOST, left, top, width, height, SWP_NOACTIVATE | SWP_SHOWWINDOW)
        self.root.update()

    def clear(self):
        """Everything but the price alerts (top right, out of the way of a scan): they go by themselves."""
        self.canvas.delete("!alert")
        self.images = []
        self.root.update()

    def status(self, text, tone="info"):
        """Badge at the top centre: clear of the minimap (top-left) and the shop.
        `tone` is "info", "warn" or "error" (render.TONES)."""
        self.canvas.delete("status")
        p = TkPainter(self, tags=("status",))
        draw_badge(p, p.size()[0] // 2, 6, text, tone)
        self.root.update()

    def clear_status(self):
        self.canvas.delete("status")
        self.root.update()

    def flash(self, text, tone="warn", ms=2000):
        """Message in the middle of the game for `ms`, then gone."""
        if self._flash_job:
            self.root.after_cancel(self._flash_job)
        self.canvas.delete("flash")
        p = TkPainter(self, tags=("flash",))
        w, h = p.size()
        draw_badge(p, w // 2, h // 2, text, tone, font="big", center=True)
        self.root.update()
        self._flash_job = self.root.after(ms, lambda: self.canvas.delete("flash"))

    def draw(self, labels):
        draw_labels(TkPainter(self), shrink(labels, None, self.size)[0])

    def draw_panel(self, panel):
        draw_panel(TkPainter(self), shrink([], panel, self.size)[1])

    def alert_cards(self, cards, ms=12000):
        """Price alerts at the game's top right for `ms`, then gone (new ones replace them)."""
        self.clear_alerts()
        p = TkPainter(self, tags=("alert",), images=self.alert_images)
        draw_panel(p, alert_panel(cards, *p.size()))
        self.root.update()
        self._alert_job = self.root.after(ms, self.clear_alerts)

    def clear_alerts(self):
        if self._alert_job:
            self.root.after_cancel(self._alert_job)
            self._alert_job = None
        self.canvas.delete("alert")
        self.alert_images.clear()

    def hover_card(self, card=None, dot=None, scale=1):
        """The price card for the hovered item, beside its tooltip (whose dot is
        at `dot`; the game drawn `scale` times its own size): left of it, or right if there's no room. None hides it."""
        self.canvas.delete("hover")
        if card:
            draw_panel(TkPainter(self, tags=("hover",)), hover_panel(card, dot, scale, self.size))
        self.root.update()


class TkPainter(Scaling):
    """render.py painter for the overlay canvas, at the overlay's size (Overlay.size). Everything it draws gets
    `tags`, so a badge can be replaced without clearing the rest."""

    def __init__(self, overlay, tags=(), images=None):
        self.ov = overlay
        self.canvas = overlay.canvas
        self.scale = overlay.size
        self.fonts = overlay.fonts_at(self.scale)
        self.tags = tags
        self.images = overlay.images if images is None else images  # where its PhotoImages are kept

    def size(self):
        return self.units(self.canvas.winfo_width()), self.units(self.canvas.winfo_height())

    def text_width(self, text, font):
        return self.units(self.fonts[font].measure(text))

    def line_height(self, font):
        return self.units(self.fonts[font].metrics("linespace"))

    def text(self, x, y, text, color, font):
        self.canvas.create_text(self.px(x), self.px(y), text=text, fill=color, font=self.fonts[font], anchor="nw",
                                tags=self.tags)

    def round_rect(self, x0, y0, x1, y1, r, fill, outline=None, width=1):
        """A smoothed polygon: Tk has no rounded rectangle. Corner points are
        doubled so the curve hugs the corners instead of rounding the whole side."""
        x0, y0, x1, y1, r, width = (self.px(v) for v in (x0, y0, x1, y1, r, width))
        r = max(1, min(r, (x1 - x0) // 2, (y1 - y0) // 2))
        pts = [x0 + r, y0, x0 + r, y0, x1 - r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y0 + r,
               x1, y1 - r, x1, y1 - r, x1, y1, x1 - r, y1, x1 - r, y1, x0 + r, y1, x0 + r, y1,
               x0, y1, x0, y1 - r, x0, y1 - r, x0, y0 + r, x0, y0 + r, x0, y0]
        self.canvas.create_polygon(pts, smooth=True, fill=fill, outline=outline or "",
                                   width=width if outline else 0, tags=self.tags)

    def image(self, x, y, png):
        photo = tk.PhotoImage(data=base64.b64encode(scaled_png(png, self.scale)).decode("ascii"))
        self.images.append(photo)  # Tk drops images that Python stops referencing
        self.canvas.create_image(self.px(x), self.px(y), image=photo, anchor="nw", tags=self.tags)


def cta_button(parent, text, command, font=("Fredoka SemiBold", 11), pady=7, **kw):
    """The site's call-to-action button (.tr-btn): white words on its pumpkin or pink."""
    return tk.Button(parent, text=text, command=command, state="normal" if command else "disabled",
                     bg=theme.CTA, fg="#ffffff", activebackground=theme.CTA_2, activeforeground="#ffffff",
                     disabledforeground="#ffe7d1", relief="flat", bd=0, highlightthickness=0, font=font,
                     padx=12, pady=pady, cursor="hand2", **kw)


def quiet_button(parent, text, command, font=("Nunito ExtraBold", 10), pady=5, **kw):
    """A lesser action: deep purple words on lilac, like the site's quiet buttons and chips."""
    return tk.Button(parent, text=text, command=command, state="normal" if command else "disabled",
                     bg=theme.FIELD, fg=theme.INK_STRONG, activebackground="#e4d8ff",
                     activeforeground=theme.INK_STRONG, relief="flat", bd=0, highlightthickness=0, font=font,
                     padx=10, pady=pady, cursor="hand2", **kw)


class OptionsWindow:
    """The F10 window: the price switches, Cozy Markets and exit actions, and an optional Discord sign-in."""

    BG, FG, FIELD = theme.PANEL, theme.INK_STRONG, theme.FIELD
    BOLD = ("Nunito ExtraBold", 10)
    DISCORD, DISCORD_HOVER = "#5865f2", "#4752c4"

    def __init__(self, root, settings, on_change, detected_map, detected_channel, open_site=None,
                 sync_status=None, on_quit=None, signin=None, open_alerts=None, test_sound=None):
        self.root, self.settings, self.on_change = root, settings, on_change
        self.test_sound = test_sound  # the alert's chime, at the volume set
        self.open_site, self.on_quit, self.open_alerts = open_site, on_quit, open_alerts
        self.signin = signin  # auth.Login, or None when the site address can't sign in
        self.win = self.account = None
        self.switches = {}  # settings field -> its BooleanVar, while the window's open
        self.capturing = False

    def toggle(self):
        if self.win and self.win.winfo_exists():
            self.win.destroy()
            self.win = None
        else:
            self._open()

    def _open(self):
        win = self.win = tk.Toplevel(self.root)
        win.title(DISPLAY_NAME)
        win.configure(bg=self.BG, padx=16, pady=16)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self.toggle)
        win.columnconfigure(0, weight=1)

        switches = tk.Frame(win, bg=self.BG)
        switches.grid(row=0, column=0, sticky="we", pady=(0, 12))
        self.switches = {}
        for i, (field, text) in enumerate((("shop_prices", t("Prices on shops")),
                                           ("hover_prices", t("Prices when hovering an item ({key})",
                                                              key=self.settings.hover_key)),
                                           ("price_alerts", t("Price alerts from Cozy Markets")),
                                           ("alert_sound", t("Alert sound")),
                                           ("bazaar_sync", t("Sync my equips to the Bazaar")),
                                           ("bazaar_public", t("Show synced equips in the Bazaar")))):
            var = self.switches[field] = tk.BooleanVar(win, getattr(self.settings, field))
            tk.Checkbutton(switches, text=text, variable=var, command=lambda f=field: self._switch(f),
                           bg=self.BG, fg=self.FG, activebackground=self.BG, activeforeground=self.FG,
                           selectcolor=self.FIELD, highlightthickness=0, font=self.BOLD, anchor="w",
                           cursor="hand2").grid(row=i + (i > 3), column=0, sticky="w")  # row 4: the volume
        sound = tk.Frame(switches, bg=self.BG)
        sound.grid(row=4, column=0, sticky="w", padx=(26, 0))
        tk.Label(sound, text=t("Volume"), bg=self.BG, fg=theme.MUTED, font=("Nunito", 9, "bold")).grid(row=0, column=0)
        self.volume = tk.Scale(sound, from_=0, to=100, orient="horizontal", length=130, showvalue=False,
                               bg=self.BG, troughcolor=self.FIELD, activebackground=theme.PURPLE,
                               highlightthickness=0, bd=0, sliderrelief="flat", cursor="hand2")
        self.volume.set(self.settings.alert_volume)
        self.volume.bind("<ButtonRelease-1>", lambda e: self._volume())
        self.volume.grid(row=0, column=1, padx=6)
        quiet_button(sound, t("Test"), self.test_sound, font=("Nunito ExtraBold", 9), pady=1).grid(row=0, column=2)

        cta_button(win, t("Open Cozy Markets"), self.open_site, width=26).grid(row=1, column=0, sticky="we")
        quiet_button(win, t("Exit MaplePrice"), self.on_quit, pady=7).grid(row=2, column=0, sticky="we", pady=(8, 0))
        if self.signin:
            tk.Frame(win, bg=self.FIELD, height=2).grid(row=3, column=0, sticky="we", pady=(14, 12))
            self.account = tk.Frame(win, bg=self.BG)
            self.account.grid(row=4, column=0, sticky="we")
            self.account.columnconfigure(0, weight=1)
            self.refresh()
        win.focus_force()

    def _switch(self, field):
        setattr(self.settings, field, self.switches[field].get())
        self.on_change()

    def _volume(self):
        self.settings.alert_volume = int(self.volume.get())
        self.on_change()

    def show_settings(self):
        """Set the switches from the settings (one changed by its hotkey while the window's open)."""
        if self.win and self.win.winfo_exists():
            for field, var in self.switches.items():
                var.set(getattr(self.settings, field))

    def refresh(self):
        """Redraw the sign-in part for the Login's current state (called on the Tk thread)."""
        if not (self.account and self.account.winfo_exists()):
            return
        for child in self.account.winfo_children():
            child.destroy()
        login, box = self.signin, self.account
        note = lambda text, color=theme.MUTED, font=("Nunito", 9): tk.Label(
            box, text=text, bg=self.BG, fg=color, font=font, wraplength=230, justify="center")
        small = lambda parent, text, command: quiet_button(parent, text, command, font=("Nunito ExtraBold", 9), pady=2)
        user = login.user
        if user and not login.working:
            name = user.get("name") or user.get("username") or "Discord user"
            note("Signed in with Discord as").grid(row=0, column=0)
            note(name, self.FG, self.BOLD).grid(row=1, column=0)
            buttons = tk.Frame(box, bg=self.BG)
            buttons.grid(row=2, column=0, pady=(6, 0))
            if self.open_alerts:
                small(buttons, "My alerts", self.open_alerts).grid(row=0, column=0, padx=3)
            small(buttons, "Sign out", login.sign_out).grid(row=0, column=1, padx=3)
            note(f"{self.settings.alert_key} adds the item you're hovering to your price alerts.").grid(
                row=3, column=0, pady=(6, 0))
        elif login.attempt:
            note("Approve this code in your browser:").grid(row=0, column=0)
            note(login.attempt.user_code, self.FG, ("Fredoka SemiBold", 16)).grid(row=1, column=0)
            buttons = tk.Frame(box, bg=self.BG)
            buttons.grid(row=2, column=0, pady=(4, 0))
            small(buttons, "Open the page again", login.reopen).grid(row=0, column=0, padx=3)
            small(buttons, "Cancel", login.cancel).grid(row=0, column=1, padx=3)
        else:
            tk.Button(box, text=login.working or "Sign in with Discord", command=login.sign_in,
                      state="disabled" if login.working else "normal",
                      bg=self.DISCORD, fg="#ffffff", activebackground=self.DISCORD_HOVER,
                      activeforeground="#ffffff", disabledforeground="#dfe1fd", relief="flat", bd=0,
                      highlightthickness=0, font=("Fredoka SemiBold", 10), padx=10, pady=6,
                      cursor="hand2").grid(row=0, column=0, sticky="we")
            if login.message:
                note(login.message, theme.DOWN).grid(row=1, column=0, pady=(6, 0))
            elif not login.working:
                note("Optional: for your Cozy Markets price alerts here.").grid(row=1, column=0, pady=(6, 0))

    def set_key(self, field, key):
        """Keep saved hotkey changes compatible with existing settings."""
        s = self.settings
        for other, _ in HOTKEYS:
            if other != field and getattr(s, other) == key:
                setattr(s, other, getattr(s, field))
        setattr(s, field, key)
        self.on_change()


class AlertDialog:
    """Asks the most you'd pay for an item, to add it to your price alerts (alerts.py). `submit(price)` is called
    on the Tk thread and hands the request to a worker; its answer comes back through done()."""

    BG, FG, FIELD = theme.PANEL, theme.INK_STRONG, theme.FIELD
    BOLD = ("Nunito ExtraBold", 10)

    def __init__(self, root, item_name, suggested="", note="", submit=None):
        self.submit = submit
        win = self.win = tk.Toplevel(root)
        win.title(f"{DISPLAY_NAME}: add monitor")
        win.configure(bg=self.BG, padx=16, pady=14)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.columnconfigure(0, weight=1)
        label = lambda text, row, color=self.FG, font=("Nunito", 10), pady=0: tk.Label(
            win, text=text, bg=self.BG, fg=color, font=font, wraplength=280, justify="left").grid(
                row=row, column=0, sticky="w", pady=pady)
        label("Alert me when", 0, theme.MUTED)
        label(item_name, 1, theme.INK_DEEP, ("Fredoka SemiBold", 12))
        label("is listed for at most", 2, theme.MUTED)
        field = tk.Frame(win, bg=self.BG)
        field.grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.entry = tk.Entry(field, width=14, bg="#ffffff", fg=self.FG, relief="flat", bd=4, font=self.BOLD,
                              highlightthickness=2, highlightbackground=theme.BORDER, highlightcolor=theme.CTA)
        self.entry.insert(0, suggested)
        self.entry.grid(row=0, column=0)
        tk.Label(field, text=" mesos", bg=self.BG, fg=theme.MUTED, font=("Nunito", 10)).grid(row=0, column=1)
        if note:
            label(note, 4, theme.MUTED, ("Nunito", 9), (4, 0))
        self.message = tk.Label(win, text="", bg=self.BG, fg=theme.MUTED, font=("Nunito", 9, "bold"),
                                wraplength=280, justify="left")
        self.message.grid(row=5, column=0, sticky="w", pady=(6, 0))
        buttons = tk.Frame(win, bg=self.BG)
        buttons.grid(row=6, column=0, sticky="e", pady=(8, 0))
        self.add = cta_button(buttons, "Add alert", self._add, font=("Fredoka SemiBold", 10), pady=3)
        self.add.grid(row=0, column=0, padx=(0, 6))
        quiet_button(buttons, "Cancel", self.close, pady=3).grid(row=0, column=1)
        win.bind("<Return>", lambda _: self._add())
        win.bind("<Escape>", lambda _: self.close())
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.focus_force()
        self.entry.focus_set()
        self.entry.select_range(0, "end")

    def is_open(self):
        return self.win.winfo_exists()

    def _say(self, text, color):
        self.message.configure(text=text, fg=color)

    def _add(self):
        if str(self.add.cget("state")) == "disabled":
            return
        price = mesos(self.entry.get())
        if price is None:
            self._say("Type a price, like 2,500 or 2.5k.", theme.DOWN)
            return
        self.add.configure(state="disabled")
        self._say("Adding...", theme.MUTED)
        self.submit(price)

    def done(self, ok, text):
        """The answer: added (closes shortly), or why not (try again)."""
        if not self.is_open():
            return
        self._say(text, theme.UP if ok else theme.DOWN)
        if ok:
            self.win.after(1500, self.close)
        else:
            self.add.configure(state="normal")

    def close(self):
        if self.is_open():
            self.win.destroy()
