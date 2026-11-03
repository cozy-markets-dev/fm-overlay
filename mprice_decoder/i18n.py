"""What MaplePrice says over the game, in English or Simplified Chinese (settings.language, F10's 中/EN button).
Each text is looked up by its English: t("Asking ") is "售价 " in Chinese, and anything not translated stays
English. Item names, job names and stat names stay as the game has them (it's in English). The React windows have
their own table (cozy-markets/frontend/src/mapleprice/strings.ts).

    t("{n} sold", n=23)  ->  "23 sold" / "已售 23"
"""
LANGUAGES = ("en", "zh")

ZH = {
    # shop labels (annotate.make_label)
    "not found": "未找到",
    "SOLD": "已售",
    "LOWEST": "最低",
    "HIGHEST": "最高",
    "24h {low}-{high}": "24小时 {low}-{high}",
    "NPC {price}": "NPC {price}",
    " ea = {total}": " 每个，共 {total}",
    # cards (annotate.make_card, make_price_card, make_alert_card)
    "Other tiers  ": "其他等级  ",
    "Lv {n}": "等级 {n}",
    "{n}% success": "成功率 {n}%",
    "{n} slots": "可升级 {n} 次",
    "Asking ": "售价 ",
    " for {qty} ({each} ea)": " 共 {qty} 个（每个 {each}）",
    "{n} sold": "已售 {n}",
    "{n} listed": "在售 {n}",
    "    FM ": "    市场 ",
    "FM {room}": "自由市场 {room}",
    "Ch {n}": "{n} 频道",
    "Same stats  ": "同属性  ",
    "none on Cozy in 2 weeks": "Cozy 两周内没有",
    "{n} on Cozy  ": "Cozy 上 {n} 件  ",
    "last sold ": "最近成交 ",
    "   sold ": "   成交 ",
    "   low ": "   最低 ",
    "cheapest ": "最便宜 ",
    " ea": " 每个",
    "PRICE UNCLEAR": "价格不明",
    "DEAL  {pct} UNDER FM": "划算  低于市价 {pct}",
    "{x}x FM PRICE": "市价的 {x} 倍",
    "{pct} OVER FM": "高于市价 {pct}",
    "FAIR PRICE": "价格合理",
    "Not found on MeowDB": "MeowDB 上找不到",
    "Name is cut off. Could be:": "名字被截断，可能是：",
    "    no FM data": "    没有市价",
    "NPC PAYS {n} MORE": "卖给 NPC 多赚 {n}",
    "CHEAPEST YOU'VE SEEN TODAY": "今天见过最便宜",
    "PRICIEST YOU'VE SEEN TODAY": "今天见过最贵",
    "SAME STATS FOR {price}": "同属性只要 {price}",
    "COZY SAW ONE FOR {price}": "COZY 见过 {price}",
    "MEOWDB HAS ONE FOR {price}": "MEOWDB 有 {price}",
    "selling from ": "最低卖价 ",
    "buying at ": "收购价 ",
    "Can't price this one": "无法估价",
    "No FM price yet": "暂无市价",
    "under your {price}": "低于你的 {price}",
    "Shop ": "商店 ",
    "Item": "物品",
    "{item} for {price}{each}": "{item} 售 {price}{each}",
    # the overlay's badges and messages (__main__)
    "Please expand the minimap": "请展开小地图",
    "Can't read the map": "无法识别地图",
    "Channel unknown: open Change Channel once": "频道未知：请打开一次换频道窗口",
    "not saved": "未保存",
    "not saved: {why}": "未保存：{why}",
    "{where}  ·  {n} items  |  {keys}": "{where}  ·  {n} 件物品  |  {keys}",
    "{where}  ·  no shop listings found  |  {keys}": "{where}  ·  没有找到商店物品  |  {keys}",
    "{clear} clear · {options} options": "{clear} 清除 · {options} 设置",
    "Scanning...": "扫描中...",
    "Scan failed: {error}": "扫描失败：{error}",
    "No shop window found": "没有找到商店窗口",
    "{n} more price alerts: {key} > My alerts": "还有 {n} 条价格提醒：{key} > 我的提醒",
    "Shop prices are off: switch them on in {key}": "商店价格已关闭：在 {key} 打开",
    "{n} unread price alerts: {key} > My alerts": "{n} 条未读价格提醒：{key} > 我的提醒",
    "1 unread price alert: {key} > My alerts": "1 条未读价格提醒：{key} > 我的提醒",
    "Sign in with Discord ({key}) to add price alerts": "用 Discord 登录（{key}）才能添加价格提醒",
    "Hover price check on: rest the cursor on an item  ·  {key} off": "悬停查价已开启：把鼠标停在物品上  ·  {key} 关闭",
    "Hover price check off": "悬停查价已关闭",
    "Updated to version {version}": "已更新到 {version} 版",
    # the Tk options window (overlay.OptionsWindow, when the React one can't start)
    "Prices on shops": "商店价格",
    "Prices when hovering an item ({key})": "悬停查价（{key}）",
    "Price alerts from Cozy Markets": "Cozy Markets 价格提醒",
    "Alert sound": "提醒音效",
    "Volume": "音量",
    "Test": "试听",
    "Sync my equips to the Bazaar": "把我的装备同步到集市",
    "Show synced equips in the Bazaar": "在集市展示同步的装备",
    "Open Cozy Markets": "打开 Cozy Markets",
    "Exit MaplePrice": "退出 MaplePrice",
    # the windows' answers (ui_host, __main__.add_alert)
    "Type a price, like 2,500 or 2.5k.": "请输入价格，例如 2,500 或 2.5k。",
    "Added: you'll get an alert when it's {price} or less.": "已添加：价格不高于 {price} 时会提醒你。",
    "Couldn't add the alert. Please try again.": "添加提醒失败，请再试一次。",
    # the Bazaar sync's line in F10 (bazaar.py)
    "{who}: {n} equips in the Bazaar": "{who}：集市里有 {n} 件装备",
    "{n} new": "新增 {n}",
    "{n} gone": "移除 {n}",
}

TABLES = {"zh": ZH}
language = "en"


def set_language(code):
    """"en" or "zh" (settings.language): anything else is English."""
    global language
    language = code if code in LANGUAGES else "en"


def t(text, **values):
    """`text` (English, maybe a "{name}" template) in the language set, filled in with `values`."""
    out = TABLES.get(language, {}).get(text, text)
    return out.format(**values) if values else out
