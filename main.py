#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات اطلاع‌رسانی کدال -> تلگرام

هر بار اجرا:
  1. آخرین اطلاعیه‌های کدال را می‌گیرد (بدون فیلتر محتوایی، همه نمادها/انواع).
  2. اطلاعیه‌های تازه (بر اساس TracingNo) را که قبلا ارسال نشده‌اند پیدا می‌کند.
  3. برای هرکدام یک خبر کوتاه فارسی (با کمک Gemini، اختیاری) و یک تصویر خبری می‌سازد.
  4. به تلگرام می‌فرستد و فقط در صورت موفقیت، آن را در فایل وضعیت ثبت می‌کند.

طراحی‌شده برای اجرا هر ۱۰ دقیقه از طریق GitHub Actions (رایگان، بدون نیاز به سرور شخصی).
همه‌ی تنظیمات از طریق متغیرهای محیطی کنترل می‌شوند؛ به .env.example نگاه کنید.
"""

import html
import json
import os
import sys
import time
import traceback
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, features

# Pillow >= 9.2 ships with libraqm bundled in its Linux wheels, which does
# correct Arabic/Persian shaping + bidi reordering natively when you pass
# direction="rtl" to draw.text()/textbbox(). We prefer that path. Only if
# raqm is missing (older Pillow build / stripped-down environment) do we
# fall back to manually reshaping + reordering the text ourselves — mixing
# both approaches double-processes the text and renders it backwards.
RAQM_OK = features.check("raqm")

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    RESHAPE_AVAILABLE = True
except ImportError:
    RESHAPE_AVAILABLE = False

# --------------------------------------------------------------------------
# تنظیمات (از Environment Variables خوانده می‌شود؛ رجوع کنید به .env.example)
# --------------------------------------------------------------------------
#
# توجه: در GitHub Actions، وقتی یک ${{ vars.X }} تعریف‌نشده به env پاس داده
# می‌شود، مقدارش رشته‌ی خالی "" می‌شود، نه این‌که اصلاً وجود نداشته باشد.
# پس os.environ.get(name, default) به‌تنهایی کافی نیست — باید رشته‌ی خالی را
# هم «تنظیم‌نشده» حساب کنیم. تابع‌های زیر همین کار را می‌کنند.


def env_str(name, default):
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def env_int(name, default):
    val = os.environ.get(name)
    if val in (None, ""):
        return default
    return int(val)


def env_bool(name, default):
    val = os.environ.get(name)
    if val in (None, ""):
        return default
    return val.strip().lower() == "true"


CODAL_API_BASE = env_str("CODAL_API_BASE", "https://search.codal.ir/api/search/v2/q")
# اگر روزی search.codal.ir از داخل GitHub Actions در دسترس نبود، بدون تغییر کد
# می‌توانید این متغیر را به آدرس یک پراکسی/آینه دیگر تغییر دهید (پروکسی باید
# همان ساختار JSON کدال را برگرداند).

TELEGRAM_BOT_TOKEN = env_str("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = env_str("TELEGRAM_CHAT_ID", "")

GEMINI_API_KEY = env_str("GEMINI_API_KEY", "")
GEMINI_MODEL = env_str("GEMINI_MODEL", "gemini-3.1-flash-lite")
# نام مدل‌های Gemini مدام تغییر می‌کند؛ اگر خطای «model not found» گرفتید،
# از https://ai.google.dev/gemini-api/docs/models نام مدل رایگانِ فعلی را
# بگذارید در Variable مربوطه در گیت‌هاب، بدون نیاز به دست‌زدن به کد.

MAX_ITEMS_PER_RUN = env_int("MAX_ITEMS_PER_RUN", 25)
SEND_IMAGE = env_bool("SEND_IMAGE", True)
SYMBOLS_FILTER = [
    s.strip() for s in env_str("SYMBOLS_FILTER", "").split(",") if s.strip()
]  # خالی = بدون فیلتر (همه‌ی نمادها)، طبق خواسته‌ی اولیه

STATE_FILE = Path(env_str("STATE_FILE", "state/seen.json"))
MAX_STATE_ITEMS = 4000  # فایل وضعیت را کوچک نگه می‌داریم

SEED_ONLY = env_bool("SEED_ONLY", False)
# در اولین اجرا True کنید تا بک‌لاگ قدیمی فقط علامت‌گذاری شود و اسپم نشود.

FONT_DIR = Path(__file__).parent / "fonts"
FONT_REGULAR = FONT_DIR / "Vazirmatn-Regular.ttf"
FONT_BOLD = FONT_DIR / "Vazirmatn-Bold.ttf"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://www.codal.ir/",
    "Accept": "application/json, text/plain, */*",
}

CODAL_BASE_SITE = "https://www.codal.ir"


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# وضعیت (جلوگیری از ارسال تکراری)
# --------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return set(str(x) for x in data.get("tracing_nos", []))
        except Exception:
            log("هشدار: فایل وضعیت خراب بود، از صفر شروع می‌شود.")
    return set()


def save_state(seen_set):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    trimmed = list(seen_set)[-MAX_STATE_ITEMS:]
    STATE_FILE.write_text(
        json.dumps({"tracing_nos": trimmed, "updated_at": datetime.now(timezone.utc).isoformat()},
                    ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# دریافت اطلاعیه‌های کدال
# --------------------------------------------------------------------------

def fetch_codal_letters():
    """
    آخرین اطلاعیه‌های کدال را برمی‌گرداند (جدیدترین اول).
    ساختار پاسخ کدال گاهی تغییر می‌کند؛ به همین دلیل چند نام فیلد احتمالی
    را امتحان می‌کنیم و در صورت شکست کامل، خام پاسخ را لاگ می‌کنیم تا بشود
    به‌سرعت این تابع را با نام فیلدهای واقعی تطبیق داد.
    """
    params = {
        "PageNumber": 1,
        "Symbol": "",
        "Audited": "false",
        "NotAudited": "false",
        "AuditorRef": -1,
        "CompanyState": -1,
        "Category": -1,
        "CompanyType": -1,
        "Publisher": "false",
        "LetterType": -1,
        "Length": 100,
    }
    resp = requests.get(CODAL_API_BASE, params=params, headers=HEADERS, timeout=25)
    resp.raise_for_status()
    data = resp.json()

    letters = data.get("Letters") or data.get("letters") or data.get("Data") or []
    if not letters:
        log("پاسخ کدال بدون فیلد Letters بود؛ کلیدهای موجود: " + ", ".join(data.keys()))
    return letters


def normalize_letter(raw):
    """یک آیتم خام کدال را به یک دیکشنری ساده و قابل‌اعتماد تبدیل می‌کند."""

    def pick(*keys, default=""):
        for k in keys:
            if k in raw and raw[k] not in (None, ""):
                return raw[k]
        return default

    tracing_no = str(pick("TracingNo", "TracingNO", "tracingNo", default=""))
    symbol = pick("Symbol", "symbol")
    company = pick("CompanyName", "Title1", "company", default=symbol)
    title = pick("Title", "title")
    letter_code = pick("LetterCode", "letterCode")
    publish_dt = pick("PublishDateTime", "SentDateTime", "publishDateTime")
    rel_url = pick("Url", "url", "PDFUrl")

    if rel_url and rel_url.startswith("/"):
        full_url = CODAL_BASE_SITE + rel_url
    elif rel_url:
        full_url = rel_url
    else:
        full_url = (
            f"{CODAL_BASE_SITE}/Reports/Decision.aspx?LetterSerial="
            f"&Sender={urllib.parse.quote(symbol)}"
        )

    return {
        "tracing_no": tracing_no,
        "symbol": symbol,
        "company": company,
        "title": title,
        "letter_code": letter_code,
        "publish_dt": publish_dt,
        "url": full_url,
    }


# --------------------------------------------------------------------------
# تولید متن خبر (Gemini اختیاری، با محدودیتِ عدم جعل اطلاعات)
# --------------------------------------------------------------------------

GEMINI_SYSTEM_PROMPT = """تو یک ویراستار خبر بازار سرمایه ایران هستی. بر اساس اطلاعات رسمی
اطلاعیه کدال زیر، یک خبر کوتاه فارسی (حداکثر ۳ جمله، حدود ۴۵۰ کاراکتر) بنویس
که لحنش شبیه خبرهای کوتاه سایت‌های بورسی باشد: مستقیم، بدون مقدمه‌چینی،
اسم شرکت/نماد در ابتدای جمله.
قوانین سخت‌گیرانه:
- هیچ عدد، درصد، یا رقم مالی که در اطلاعات داده‌شده نیامده را اختراع نکن.
- اگر اطلاعات کافی نیست، فقط همان عنوان رسمی را به زبان روان‌تر بازنویسی کن،
  حدس نزن و جزئیات نساز.
- از کلمات تبلیغاتی، ایموجی، و جملات اضافه خودداری کن.
- فقط متن خبر را برگردان، بدون هیچ توضیح یا برچسب اضافه."""


def rewrite_with_gemini(letter):
    if not GEMINI_API_KEY:
        return None

    user_content = (
        f"نماد: {letter['symbol']}\n"
        f"نام شرکت: {letter['company']}\n"
        f"عنوان اطلاعیه: {letter['title']}\n"
        f"نوع نامه: {letter['letter_code']}\n"
        f"زمان انتشار: {letter['publish_dt']}"
    )

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "systemInstruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_content}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 300},
    }
    try:
        r = requests.post(url, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return text if text else None
    except Exception as e:
        log(f"Gemini با خطا مواجه شد ({letter['tracing_no']}): {e}")
        return None


def build_news_text(letter):
    generated = rewrite_with_gemini(letter)
    if generated:
        return generated
    # حالت پشتیبان: بدون هیچ جعل اطلاعات، فقط فیلدهای واقعی کدال
    parts = []
    if letter["company"] and letter["symbol"]:
        parts.append(f"{letter['company']} ({letter['symbol']})")
    elif letter["company"] or letter["symbol"]:
        parts.append(letter["company"] or letter["symbol"])
    if letter["title"]:
        parts.append(letter["title"])
    return " — ".join(parts) if parts else "اطلاعیه جدید کدال"


# --------------------------------------------------------------------------
# تولید تصویر خبری (فارسی، راست‌به‌چپ)
# --------------------------------------------------------------------------

CARD_W, CARD_H = 1080, 1080
COLOR_BG = (16, 22, 34)
COLOR_ACCENT = (0, 168, 132)      # سبز-فیروزه‌ای، حس مالی/بورسی
COLOR_TEXT = (240, 242, 245)
COLOR_SUBTEXT = (160, 170, 185)


def render_text(text):
    """متنی که واقعاً باید به draw.text() داده شود، به‌همراه kwargs لازم."""
    if RAQM_OK:
        return text, {"direction": "rtl"}
    if RESHAPE_AVAILABLE:
        return get_display(arabic_reshaper.reshape(text)), {}
    return text, {}  # آخرین راه‌حل: بدون شکل‌دهی درست (فقط انگلیسی/اعداد درست دیده می‌شود)


def measure(draw, text, font):
    rtext, kwargs = render_text(text)
    return draw.textbbox((0, 0), rtext, font=font, **kwargs)


def wrap_logical(text, font, max_width, draw):
    """بر اساس ترتیب منطقی کلمات (نه شکل بصری) خط‌شکنی می‌کنیم."""
    words = text.split()
    lines, current = [], ""
    for w in words:
        trial = (current + " " + w).strip()
        bbox = measure(draw, trial, font)
        if bbox[2] - bbox[0] <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def draw_centered_multiline(draw, lines, font, y, max_width, fill, line_spacing=14):
    for line in lines:
        rtext, kwargs = render_text(line)
        bbox = draw.textbbox((0, 0), rtext, font=font, **kwargs)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        x = (CARD_W - w) / 2 - bbox[0]
        draw.text((x, y), rtext, font=font, fill=fill, **kwargs)
        y += h + line_spacing
    return y


def make_news_image(letter, news_text, out_path):
    img = Image.new("RGB", (CARD_W, CARD_H), COLOR_BG)
    draw = ImageDraw.Draw(img)

    # نوار رنگی بالا
    draw.rectangle([0, 0, CARD_W, 10], fill=COLOR_ACCENT)

    font_label = ImageFont.truetype(str(FONT_BOLD), 34)
    font_symbol = ImageFont.truetype(str(FONT_BOLD), 58)
    font_body = ImageFont.truetype(str(FONT_REGULAR), 40)
    font_meta = ImageFont.truetype(str(FONT_REGULAR), 30)

    y = 70
    label_text, label_kwargs = render_text("اطلاعیه جدید کدال")
    bbox = draw.textbbox((0, 0), label_text, font=font_label, **label_kwargs)
    draw.text(((CARD_W - (bbox[2] - bbox[0])) / 2 - bbox[0], y), label_text,
              font=font_label, fill=COLOR_ACCENT, **label_kwargs)
    y += (bbox[3] - bbox[1]) + 40

    header = f"{letter['company']}" + (f" ({letter['symbol']})" if letter["symbol"] else "")
    header_lines = wrap_logical(header.strip(), font_symbol, CARD_W - 120, draw)
    y = draw_centered_multiline(draw, header_lines, font_symbol, y, CARD_W - 120, COLOR_TEXT, 16)
    y += 30

    draw.line([(90, y), (CARD_W - 90, y)], fill=(50, 58, 72), width=2)
    y += 40

    body_lines = wrap_logical(news_text.strip(), font_body, CARD_W - 160, draw)
    y = draw_centered_multiline(draw, body_lines, font_body, y, CARD_W - 160, COLOR_TEXT, 14)

    # پانویس
    meta = f"{letter['publish_dt']}  •  کد پیگیری {letter['tracing_no']}  •  منبع: codal.ir"
    meta_text, meta_kwargs = render_text(meta)
    bbox = draw.textbbox((0, 0), meta_text, font=font_meta, **meta_kwargs)
    draw.text(((CARD_W - (bbox[2] - bbox[0])) / 2 - bbox[0], CARD_H - 80), meta_text,
              font=font_meta, fill=COLOR_SUBTEXT, **meta_kwargs)

    img.save(out_path, "PNG")
    return out_path


# --------------------------------------------------------------------------
# ارسال به تلگرام
# --------------------------------------------------------------------------

def send_telegram(letter, news_text, image_path=None):
    caption = (
        f"<b>{html.escape(letter['company'])}"
        f"{' (' + html.escape(letter['symbol']) + ')' if letter['symbol'] else ''}</b>\n\n"
        f"{html.escape(news_text)}\n\n"
        f"🔗 <a href=\"{html.escape(letter['url'])}\">مشاهده در کدال</a>"
    )
    if len(caption) > 1024:
        caption = caption[:1000] + "…"

    if image_path and SEND_IMAGE:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        with open(image_path, "rb") as f:
            files = {"photo": f}
            data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
            r = requests.post(url, data=data, files=files, timeout=30)
    else:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        r = requests.post(url, data=data, timeout=30)

    if r.status_code != 200:
        raise RuntimeError(f"ارسال تلگرام شکست خورد ({r.status_code}): {r.text[:300]}")


# --------------------------------------------------------------------------
# اجرای اصلی
# --------------------------------------------------------------------------

def main():
    missing = [n for n, v in [("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
                               ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)] if not v]
    if missing:
        log("متغیرهای محیطی زیر تنظیم نشده‌اند: " + ", ".join(missing))
        sys.exit(1)

    seen = load_state()
    log(f"تعداد موارد ثبت‌شده در وضعیت فعلی: {len(seen)}")

    try:
        raw_letters = fetch_codal_letters()
    except Exception as e:
        log("خطا در دریافت اطلاعیه‌های کدال — احتمالا مسدودیت جغرافیایی یا قطعی موقت:")
        log(str(e))
        traceback.print_exc()
        sys.exit(0)  # با کد صفر خارج می‌شویم تا Action «قرمز» نشود؛ اجرای بعدی دوباره تلاش می‌کند

    letters = [normalize_letter(x) for x in raw_letters]
    letters = [l for l in letters if l["tracing_no"]]

    if SYMBOLS_FILTER:
        letters = [l for l in letters if l["symbol"] in SYMBOLS_FILTER]

    new_letters = [l for l in letters if l["tracing_no"] not in seen]
    new_letters.reverse()  # قدیمی‌ترین اول تا ترتیب کانال منطقی باشد

    log(f"{len(letters)} اطلاعیه دریافت شد، {len(new_letters)} مورد جدید است.")
    if letters and len(new_letters) == len(letters) and len(seen) > 0:
        log("هشدار: همه‌ی موارد دریافتی جدید بودند — یعنی احتمالاً بین این اجرا و "
            "اجرای قبلی بیش از ظرفیت یک صفحه (۱۰۰ مورد) اطلاعیه منتشر شده و ممکن "
            "است چیزی از قلم افتاده باشد. اگر این هشدار زیاد تکرار شد، MAX_ITEMS_PER_RUN "
            "و بازه‌ی اجرا را بازبینی کن.")

    if SEED_ONLY:
        for l in letters:
            seen.add(l["tracing_no"])
        save_state(seen)
        log("حالت SEED_ONLY: هیچ پیامی ارسال نشد، فقط وضعیت اولیه ثبت شد.")
        return

    sent_count = 0
    for letter in new_letters:
        if sent_count >= MAX_ITEMS_PER_RUN:
            log("به سقف MAX_ITEMS_PER_RUN رسیدیم؛ بقیه در اجرای بعدی ارسال می‌شوند.")
            break
        try:
            news_text = build_news_text(letter)
            image_path = None
            if SEND_IMAGE:
                image_path = f"/tmp/card_{letter['tracing_no']}.png"
                make_news_image(letter, news_text, image_path)
            send_telegram(letter, news_text, image_path)
            seen.add(letter["tracing_no"])
            sent_count += 1
            log(f"ارسال شد: [{letter['tracing_no']}] {letter['symbol']} — {letter['title'][:40]}")
            time.sleep(2.5)  # فاصله‌ی امن برای Rate limit مدل رایگان و تلگرام
        except Exception as e:
            log(f"ارسال اطلاعیه {letter['tracing_no']} شکست خورد، در اجرای بعدی دوباره تلاش می‌شود: {e}")

    save_state(seen)
    log(f"پایان اجرا. {sent_count} اطلاعیه ارسال شد.")


if __name__ == "__main__":
    main()
