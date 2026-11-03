"""What to draw next to each shop listing (shared by the live overlay and --image mode).

A Label is a block of lines; each line is a list of (text, color) segments so
flags like "LOWEST" can stand out from the prices around them. For tiered items
(scrolls), the other tiers' prices go on the item's card in the detail panel.
"""
from dataclasses import dataclass, field, replace

from . import theme
from .i18n import t

NEUTRAL = theme.INK_STRONG  # numbers, as the site has them
DIM = theme.MUTED
FM = theme.GOLD_INK      # FM price, so it stands apart from the NPC price
PROFIT = theme.UP        # listing costs less than the NPC pays: buy it and vend it
LOWEST = theme.UP        # cheapest we've seen this item listed in the last 24h (the site's "lowest listed")
HIGHEST = theme.DOWN     # priciest we've seen this item listed in the last 24h
UNKNOWN = theme.WARN
DEAL = theme.PLUM        # listed well under the usual FM price
SOLD = theme.FAINT       # greyed-out row: someone already bought it

LABEL_GAP = 46       # px left of the item name: clears the item icon and the shop border
TIER_ORDER = ["Lesser", "Intermediate", "Greater", "Chaos"]
DEAL_THRESHOLD = 0.20  # flag listings at least this far below the FM price


@dataclass
class Label:
    """What's left of a shop row: only what's worth acting on there (an NPC
    profit, today's price range, SOLD). Prices live on the cards; a label with
    no lines isn't drawn."""
    right: int       # right edge x
    center_y: int
    lines: list = field(default_factory=list)  # [[(text, color), ...], ...]

    @property
    def text(self):
        return " / ".join("".join(text for text, _ in line) for line in self.lines)


@dataclass
class Version:
    item: object       # data.Item
    market: object     # data.Market (MeowDB's FM price) or None if the FM lookup failed
    cozy: object = None  # cozy_prices.CozyPrice: Cozy Markets' own price, shown first; None without one


def usual(v):
    """What the item usually goes for: Cozy Markets' price when it has one, else MeowDB's."""
    if v.cozy and v.cozy.usual:
        return v.cozy.usual
    return v.market.usual if v.market else None


def weight(v):
    """How much is behind a version's price, to show the best-known of several same-name items."""
    return ((v.cozy.listings + v.cozy.sales) if v.cozy and v.cozy.usual else -1,
            v.market.reports if v.market else -1)


def fmt(n):
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}b".replace(".0b", "b")
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m".replace(".0m", "m")
    if n >= 10_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    if n != int(n):
        return f"{n:,.2f}".rstrip("0").rstrip(".")
    return f"{int(n):,}"


def fmt_short(n):
    """Tighter than fmt for the other-tiers line: 4,999 -> 5k, 5,500 -> 5.5k."""
    if n < 1000:
        return f"{int(n)}"
    return fmt(n) if n >= 1_000_000 else f"{n / 1000:.1f}k".replace(".0k", "k")


def _common_prefix_len(names):
    first, last = min(names), max(names)
    i = 0
    while i < len(first) and first[i] == last[i]:
        i += 1
    # back up to a word boundary so "Intermediate" isn't shown as "ntermediate"
    while i > 0 and first[i - 1] not in " :":
        i -= 1
    return i


def _npc_profit(listing, item):
    """'NPC 7 ea = 35 (+15)' in green when the NPC pays more than the asking
    price (buy it and vend it), else nothing. A guessed or unreadable price never counts."""
    npc_total = item.price * listing.qty
    if listing.uncertain or listing.price is None or listing.price >= npc_total:
        return []
    npc = t("NPC {price}", price=fmt(item.price)) + (t(" ea = {total}", total=fmt(npc_total)) if listing.qty > 1 else "")
    return [(npc, PROFIT), (f" (+{fmt(npc_total - listing.price)})", PROFIT)]


def tier(name):
    """'Crossbow Attack Scroll: Lesser' -> 'Lesser' (None for untiered items)."""
    return name.rsplit(": ", 1)[1] if ": " in name else None


def other_tiers(siblings):
    """The matched item's other tiers, in tier order, as one card line:
    'Other tiers  Lesser -   Inter. ~25k'."""
    order = lambda v: (TIER_ORDER.index(tier(v.item.name)) if tier(v.item.name) in TIER_ORDER
                       else len(TIER_ORDER), v.item.name)
    line = [(t("Other tiers  "), LABEL)]
    for i, v in enumerate(sorted(siblings, key=order)):
        u = usual(v)
        line += [(("   " if i else "") + f"{_short(tier(v.item.name) or v.item.name)} ", DIM),
                 (f"~{fmt_short(u)}", FM) if u is not None else ("-", DIM)]
    return line


def make_label(listing, match, versions, seen=None, siblings=()):
    """`seen` is the data.Seen range for this item, only when it matched exactly one item.
    `siblings` are the matched item's other tiers (Versions); their prices go on the card."""
    label = Label(listing.x - LABEL_GAP, (listing.y + listing.bottom) // 2)
    line = _label_line(listing, match, versions, seen, siblings)
    if listing.sold:  # its range still shows what it went for, but nothing to act on
        line = [seg for seg in line if seg[1] not in (LOWEST, HIGHEST, PROFIT)]
        line = [(t("SOLD"), SOLD)] + ([("  ", SOLD)] + line if line else [])
    if line:
        label.lines.append(line)
    return label


def _label_line(listing, match, versions, seen, siblings):
    if not match.found:
        return [(t("not found"), UNKNOWN)]
    if len(versions) != 1:  # name cut off, item undecided: the card lists what it could be
        return []
    v = versions[0]
    line = _npc_profit(listing, v.item)
    if seen and seen.count >= 2:
        unit = listing.price / listing.qty
        line.append(((" " * 2 if line else "") + t("24h {low}-{high}", low=fmt(seen.low), high=fmt(seen.high)), DIM))
        if unit <= seen.low:
            line.append(("  " + t("LOWEST"), LOWEST))
        elif unit >= seen.high:
            line.append(("  " + t("HIGHEST"), HIGHEST))
    if line and siblings:  # say which tier we decided on; the card lists the others
        line.insert(0, (f"{_short(tier(v.item.name))}: ", DIM))
    return line


def _short(name):
    return name if len(name) <= 8 else name[:5] + "."


# ---- Detail panel (right of the shop window) ---------------------------------
# One card per listing: icon, name, what the item does, then a plain-English
# verdict on the asking price. Rendering lives in render.py.

LABEL = theme.LABEL_INK  # "Asking", "Cozy", "NPC" captions: the site's small purple labels
FAIR = theme.NEUTRAL    # accent for a fair price / no opinion
STATS = [  # (key in MeowDB stats, label)
    ("watk", "W.ATK"), ("matk", "M.ATK"), ("str", "STR"), ("dex", "DEX"), ("int", "INT"),
    ("luk", "LUK"), ("acc", "ACC"), ("avoid", "AVOID"), ("speed", "SPEED"), ("jump", "JUMP"),
    ("hp", "HP"), ("mp", "MP"), ("wdef", "W.DEF"), ("mdef", "M.DEF"), ("critRate", "CRIT"), ("critDamage", "CRIT DMG"),
]
OVERPRICED = 1.2      # asking >= 120% of the FM price
MIN_SANE_LISTING = 0.3  # ignore MeowDB listings under 30% of the FM price (per-piece or junk)


@dataclass
class Pill:
    """A chip, like the site's tags: white words on `color`; `quiet`, `color` words on a lilac chip (a note
    rather than a verdict: where Cozy saw a cheaper one)."""
    text: str
    color: str
    quiet: bool = False


@dataclass
class Card:
    title: str
    subtitle: str                                 # what the item is / does
    price: list                                   # [(text, color), ...]
    pills: list = field(default_factory=list)     # [Pill, ...] verdicts, most important first
    extra: list = field(default_factory=list)     # more [(text, color), ...] lines (cut-off names)
    accent: str = FAIR                            # colour of the card's edge: the verdict at a glance
    icon: bytes | None = None                     # PNG from MeowDB
    count: int = 1                                # identical listings merged into this card

    def key(self):
        return self.title, "".join(text for text, _ in self.price)


@dataclass
class Panel:
    """Cards go in the box (left, top)-(right, bottom): right of the shop window.
    The panel starts level with the shop's top and may slide up to `min_top`."""
    left: int
    top: int
    right: int
    bottom: int
    min_top: int = 8
    cards: list = field(default_factory=list)


def describe_stats(info):
    """'Lv 15 Bowman · W.DEF +12  HP +5 · 5 slots' / '60% success · W.ATK +2' / ''."""
    if not info:
        return ""
    stats = info.get("stats") or {}
    parts = []
    req = " ".join(p for p in (t("Lv {n}", n=info["levelReq"]) if info.get("levelReq") else "",
                               info.get("reqJob") or "") if p)
    if req:
        parts.append(req)
    if stats.get("successRate"):
        parts.append(t("{n}% success", n=stats["successRate"]))
    bonuses = "  ".join(f"{label} +{stats[k]}" for k, label in STATS if stats.get(k))
    if bonuses:
        parts.append(bonuses)
    if stats.get("slots"):
        parts.append(t("{n} slots", n=stats["slots"]))
    return "  \u00b7  ".join(parts)


def _asking(listing):
    if listing.price is None:
        return [(t("Asking "), LABEL), ("?", UNKNOWN)]
    segs = [(t("Asking "), LABEL), (fmt(listing.price), NEUTRAL)]
    if listing.qty > 1:
        segs.append((t(" for {qty} ({each} ea)", qty=fmt(listing.qty), each=fmt(listing.price / listing.qty)), DIM))
    return segs


def _fm(v):
    """'Cozy ~6.7k (23 sold)' from Cozy Markets' scans, else MeowDB's 'FM ~21.5k (3)', else nothing."""
    c, m = v.cozy, v.market
    if c and c.usual:
        count = t("{n} sold", n=c.sales) if c.basis == "sold" else t("{n} listed", n=c.listings)
        return [("    Cozy ", LABEL), (f"~{fmt(c.usual)}", theme.FM_PILL), (f" ({count})", DIM)]
    if m is None or m.usual is None:
        return []
    return [(t("    FM "), LABEL), (f"~{fmt(m.usual)}", theme.FM_PILL), (f" ({m.reports})", DIM)]


def _place(channel, room, map_name):
    """'Ch 7 · FM 10', 'Ch 7 · Henesys Market', 'Ch 7' or '', as the site writes them (whereOf)."""
    if not channel:
        return ""
    return " \u00b7 ".join([t("Ch {n}", n=channel)] + ([t("FM {room}", room=room)] if room else [map_name] if map_name else []))


def _where(c):
    """Where Cozy Markets saw its cheapest one."""
    return _place(c.low_channel, c.low_fm_room, c.low_map)


def _same_lines(same, cheapest=False):
    """What other shops listed an equip for with exactly its stats (cozy_prices.SameStats): 'Same stats  3 on
    Cozy  ~5k   low 3.5k   sold 4k', or that there were none. With `cheapest` (the narrower hover card), one
    thing a line: how many and their usual price, the cheapest and where, the last sale."""
    if not same.listings:
        return [[(t("Same stats  "), LABEL), (t("none on Cozy in 2 weeks"), DIM)]]
    line = [(t("Same stats  "), LABEL), (t("{n} on Cozy  ", n=same.listings), DIM), (f"~{fmt(same.median)}", NEUTRAL)]
    sold = [(t("last sold ") if cheapest else t("   sold "), DIM), (fmt(same.last_sold), NEUTRAL)] if same.last_sold else []
    if not cheapest:
        if same.low != same.median:
            line += [(t("   low "), DIM), (fmt(same.low), NEUTRAL)]
        return [line + sold]
    lines = [line]
    c = same.cheapest
    if c:
        where = _place(c["channel"], c["fm_room"], c["map"])
        lines.append([(t("Same stats  "), LABEL), (t("cheapest "), DIM), (fmt(c["price"]), NEUTRAL)]
                     + ([(f"  {where}", DIM)] if where else []))
    if sold:
        lines.append([(t("Same stats  "), LABEL)] + sold)
    return lines


def _npc_price(item, qty):
    if not item.price:
        return []
    return [("    NPC ", LABEL), (fmt(item.price) + (t(" ea") if qty > 1 else ""), NEUTRAL)]


def verdict(listing, price):
    """(Pill or None, accent colour) comparing the asking price with what the item usually goes for (`price`)."""
    if listing.price is None or listing.uncertain:
        return Pill(t("PRICE UNCLEAR"), UNKNOWN), UNKNOWN
    if not price or listing.qty != 1:
        return None, FAIR
    ratio = listing.price / price
    if ratio <= 1 - DEAL_THRESHOLD:
        return Pill(t("DEAL  {pct} UNDER FM", pct=f"{1 - ratio:.0%}"), DEAL), DEAL
    if ratio >= OVERPRICED:
        over = t("{x}x FM PRICE", x=f"{ratio:.1f}") if ratio >= 2 else t("{pct} OVER FM", pct=f"{ratio - 1:.0%}")
        return Pill(over, HIGHEST), HIGHEST
    return Pill(t("FAIR PRICE"), theme.MUTED), FAIR


def make_card(listing, match, versions, seen=None, info=None, siblings=(), same=None):
    """The detail-panel card for one shop listing. `siblings`: the item's other
    tiers (scrolls), listed with their FM prices. `same`: for an equip whose stats
    the decoder read, what other shops listed it for with exactly those stats."""
    if not match.found:
        return Card(listing.name, t("Not found on MeowDB"), _asking(listing), accent=UNKNOWN)

    names = {v.item.name for v in versions}
    if len(names) == 1:  # different items sharing one name (e.g. two "Beret"s): show it once
        versions = [max(versions, key=weight)]
    if len(versions) > 1:
        cut = _common_prefix_len(sorted(names))
        card = Card(listing.name, t("Name is cut off. Could be:"), _asking(listing), accent=UNKNOWN)
        seen_names = set()
        for v in versions:
            short = v.item.name[cut:].strip() or v.item.name
            if short not in seen_names:
                seen_names.add(short)
                card.extra.append([(short, NEUTRAL)] + (_fm(v) or [(t("    no FM data"), DIM)]))
        return card

    v = versions[0]
    item, m, c, u = v.item, v.market, v.cozy, usual(v)
    price = _asking(listing) + _fm(v) + _npc_price(item, listing.qty)
    pill, accent = verdict(listing, u)
    card = Card(item.name, describe_stats(info), price, accent=accent)
    if siblings:
        card.extra.append(other_tiers(siblings))
    if same is not None:
        card.extra += _same_lines(same)
    if pill:
        card.pills.append(pill)
    certain = listing.price is not None and not listing.uncertain
    npc_total = item.price * listing.qty
    if certain and listing.price < npc_total:
        card.pills.append(Pill(t("NPC PAYS {n} MORE", n=fmt(npc_total - listing.price)), PROFIT))
    if certain and seen and seen.count >= 2:
        unit = listing.price / listing.qty
        if unit <= seen.low:
            card.pills.append(Pill(t("CHEAPEST YOU'VE SEEN TODAY"), LOWEST))
        elif unit >= seen.high:
            card.pills.append(Pill(t("PRICIEST YOU'VE SEEN TODAY"), HIGHEST))
    unit = listing.price / listing.qty if certain else None
    if same is not None:  # an equip's stats are known: only one with the same stats is a cheaper one of it
        if certain and same.cheapest and same.cheapest["price"] < unit:
            where = _place(same.cheapest["channel"], same.cheapest["fm_room"], same.cheapest["map"])
            card.pills.append(Pill(t("SAME STATS FOR {price}", price=fmt(same.cheapest["price"])) + (f"  {where.upper()}" if where else ""),
                                   theme.INK_STRONG, quiet=True))
    elif certain and c and c.low and c.low < unit and (not u or c.low >= MIN_SANE_LISTING * u):
        where = _where(c)
        card.pills.append(Pill(t("COZY SAW ONE FOR {price}", price=fmt(c.low)) + (f"  {where.upper()}" if where else ""),
                               theme.INK_STRONG, quiet=True))
    elif (certain and m and m.cheapest_sell and m.cheapest_sell < unit
            and (not m.usual or m.cheapest_sell >= MIN_SANE_LISTING * m.usual)):
        card.pills.append(Pill(t("MEOWDB HAS ONE FOR {price}", price=fmt(m.cheapest_sell)), theme.INK_STRONG, quiet=True))
    return card


def _cozy_lines(c):
    """'Cozy  cheapest 4.4k  Ch 7 FM 10' and 'Cozy  last sold 7k': what Cozy Markets has seen lately."""
    lines = []
    if c and c.low:
        where = _where(c)
        lines.append([("Cozy  ", LABEL), (t("cheapest "), DIM), (fmt(c.low), NEUTRAL)] + ([(f"  {where}", DIM)] if where else []))
    if c and c.last_sold:
        lines.append([("Cozy  ", LABEL), (t("last sold "), DIM), (fmt(c.last_sold), NEUTRAL)])
    return lines


def _meowdb_line(m):
    """'MeowDB  selling from 12k   buying at 9k', or None without live listings."""
    line = []
    if m and m.cheapest_sell:
        line += [("MeowDB  ", LABEL), (t("selling from "), DIM), (fmt(m.cheapest_sell), NEUTRAL)]
    if m and m.best_buy:
        line += [("   " if line else "MeowDB  ", LABEL), (t("buying at "), DIM), (fmt(m.best_buy), NEUTRAL)]
    return line or None


def make_price_card(name, match, versions, info=None, siblings=(), same=None):
    """The card for an item you're hovering over (inventory, NPC shop): what
    it's worth, with no asking price to judge. `same` as for make_card."""
    if not match.found:
        return Card(name, t("Not found on MeowDB"), [(t("Can't price this one"), DIM)], accent=UNKNOWN)
    best = max(versions, key=weight)  # same-name items: show one
    item, m = best.item, best.market
    fm = _fm(best)
    price = ([(fm[0][0].strip() + " ", LABEL)] + fm[1:] if fm else [(t("No FM price yet"), DIM)]) + _npc_price(item, 1)
    card = Card(item.name, describe_stats(info), price, accent=FM if fm else FAIR)
    cozy = _cozy_lines(best.cozy)
    meowdb = _meowdb_line(m)
    card.extra += cozy or ([meowdb] if meowdb else [])
    if same is not None:
        card.extra += _same_lines(same, cheapest=True)
    if siblings:
        card.extra.append(other_tiers(siblings))
    return card


def make_alert_card(hit):
    """A price alert (alerts.py: a watched item found at or under the most you'd pay) as a card, worded like the
    website's pop-up: 'Blue Pole-Feather Hat for 2,500', 'under your 3,000 · Ch 7 · FM 8', and the shop."""
    each = t(" ea") if (hit.get("qty") or 1) > 1 else ""
    where = _place(hit.get("channel"), hit.get("fmRoom"), hit.get("mapName"))
    under = t("under your {price}", price=fmt(hit["maxPrice"])) + (f" \u00b7 {where}" if where else "")
    shop = [(t("Shop "), LABEL), (hit["shop"], NEUTRAL)] if hit.get("shop") else []
    return Card(t("{item} for {price}{each}", item=hit.get("itemName") or t("Item"), price=fmt(hit["price"]), each=each),
                under, shop, accent=theme.GOLD)


ALERT_WIDTH = 380          # alert cards, top right of the game


def alert_panel(cards, width, height):
    """Where alert cards go: the game's top right, under the window's title strip."""
    return Panel(width - 12 - ALERT_WIDTH, 48, width - 12, height - 12, cards=cards)


HOVER_WIDTH = 420          # the hover price card: Cozy's, MeowDB's and same-stats prices
HOVER_TOOLTIP_WIDTH = 300  # room left for the game's tooltip when the card goes right of it


def scale_up(labels, panel, scale):
    """Labels and a panel placed on the game at its own size (ocr.game_view), placed on a screen that shows it
    `scale` times bigger. The cards keep their size: only where they go changes."""
    if scale == 1:
        return labels, panel
    at = lambda v: int(round(v * scale))
    labels = [replace(l, right=at(l.right), center_y=at(l.center_y)) for l in labels]
    if panel is not None:
        panel = replace(panel, left=at(panel.left), top=at(panel.top), bottom=at(panel.bottom),
                        right=panel.right if panel.right >= 10_000 else at(panel.right))
    return labels, panel


def hover_panel(card, dot, scale=1, size=1):
    """Where the hover card goes: left of the game's tooltip (whose dot is at
    `dot`), level with its top, or right of it when there's no room. The game draws
    the tooltip `scale` times its own size (ocr.game_view); the overlay draws its cards `size` times
    (settings.overlay_scale): the panel is in the overlay's units (render.py)."""
    at = (lambda v: v) if size == 1 else (lambda v: v / size)
    left = at(dot[0] - int(14 * scale)) - HOVER_WIDTH
    if left < 4:
        left = at(dot[0] + int(HOVER_TOOLTIP_WIDTH * scale))
    top = at(dot[1] - int(14 * scale))
    return Panel(left, top, left + HOVER_WIDTH, top + 400, cards=[card])


def shrink(labels, panel, size):
    """Labels and a panel placed on the screen, in the overlay's own units at `size` (settings.overlay_scale): the
    painter draws them `size` times bigger from the same spots (a label grows left of its row, a panel right and
    down from its corner)."""
    if size == 1:
        return labels, panel
    labels = [replace(l, right=l.right / size, center_y=l.center_y / size) for l in labels]
    if panel is not None:
        panel = replace(panel, left=panel.left / size, top=panel.top / size, bottom=panel.bottom / size,
                        right=panel.right if panel.right >= 10_000 else panel.right / size)
    return labels, panel
