# MasterHttpRelayVPN — نسخه بهبودیافته

[![GitHub](https://img.shields.io/badge/GitHub-MasterHttpRelayVPN-blue?logo=github)](https://github.com/masterking32/MasterHttpRelayVPN)
[![نویسنده اصلی](https://img.shields.io/badge/نویسنده%20اصلی-masterking32-orange?logo=github)](https://github.com/masterking32)
[![English README](https://img.shields.io/badge/🇬🇧-English%20README-blue)](README.md)

<div dir="rtl">

> **این یک نسخه بهبودیافته از پروژه [MasterHttpRelayVPN](https://github.com/masterking32/MasterHttpRelayVPN) ساخته [@masterking32](https://github.com/masterking32) است که در دانشگاه تورنتو توسعه یافته.**
> تمام اعتبار ایده، معماری و پیاده‌سازی اصلی متعلق به نویسنده اصلی است. این نسخه صرفاً روی بهبود سرعت، صحت و امنیت تمرکز دارد.

---

## این ابزار چیست؟

ابزاری رایگان که با پنهان کردن ترافیک شما پشت وب‌سایت‌های معتبر مانند گوگل، دسترسی آزاد به اینترنت را ممکن می‌سازد. نیازی به VPS یا سرور ندارید — فقط یک حساب گوگل رایگان کافی است.

> **به زبان ساده:** مرورگر شما با این ابزار روی کامپیوترتان صحبت می‌کند. این ابزار ترافیک شما را شبیه به ترافیک معمول گوگل جلوه می‌دهد. فیلتر «google.com» را می‌بیند و اجازه عبور می‌دهد. در پشت صحنه، یک relay رایگان بر بستر Google Apps Script وب‌سایت واقعی را برای شما دریافت می‌کند.

```
مرورگر → پروکسی محلی → CDN/Google → relay شما → وب‌سایت مقصد
              |
              └─ فیلتر فقط google.com را می‌بیند
```

---

## تغییرات این نسخه نسبت به پروژه اصلی

### `proxy_server.py`
- **`current_speeds()`** — جستجوی خطی O(n) روی ۳۰۰ نمونه با جستجوی دودویی `bisect` جایگزین شد (O log n)
- **`parse_ttl()` و `_is_likely_download()`** — حلقه‌های Python روی لیست پسوندها با `str.endswith(tuple)` که در سطح C اجرا می‌شود جایگزین شدند
- **`recent_hosts`** — تخصیص دوگانه لیست غیرضروری حذف شد
- **`_do_http()`** — `header_block.split()` که دوبار صدا زده می‌شد، یکبار شد
- **`_tunnel_http()`** — `import re` درون تابع بود و با هر درخواست مجدداً اجرا می‌شد؛ به بیرون منتقل شد
- **`asyncio.get_event_loop().create_task()`** — با `asyncio.ensure_future()` جایگزین شد

### `main.py`
- **`_sparkline()`** — `list.insert(0, ...)` درون حلقه از O(n²) به O(n) تبدیل شد
- **پاک‌سازی داشبورد** — escape کد ANSI در ترمینال ویندوز کار نمی‌کرد؛ در ویندوز از `os.system("cls")` و در Linux/macOS از ANSI استفاده می‌شود
- **صفحه splash** — صفحه امضای انیمیشنی ۸ ثانیه‌ای با نوار شمارش معکوس قبل از شروع پروکسی اضافه شد
- **غلط تایپی** — «University of Tornoto» به «University of Toronto» اصلاح شد

### `domain_fronter.py`
- **`_close_writer()`** — هلپر مشترک استخراج شد؛ ۸ بلوک تکراری `try: writer.close() except: pass` حذف شدند
- **`_REDIRECT_STATUSES`** — به عنوان `frozenset` ثابت کلاس استخراج شد
- **`_SKIP` در `_parse_relay_json()`** — با هر پاسخ بازسازی می‌شد؛ به ثابت سطح کلاس منتقل شد
- **`_pool_maintenance()`** — writer ها داخل `_pool_lock` بسته می‌شدند و خطر deadlock وجود داشت؛ اصلاح شد
- **`_rewrite_206_to_200()`** — زنجیره شکننده `.replace()` با `re.sub` جایگزین شد
- **`_read_http_response()` / `_split_raw_response()`** — `split(":", 1)` با `partition(":")` جایگزین شد
- **`_prewarm_script()`** — دو بلوک try/except تکراری در یک حلقه ادغام شدند
- **`_relay_fallback`** — `asyncio.gather` گم بود و fallback ها به صورت ترتیبی اجرا می‌شدند؛ اصلاح شد

### `h2_transport.py`
- **`_close_internal()`** — `cancel()` روی task بدون `await` بود (task روی connection مرده ادامه می‌داد)؛ اصلاح شد. `writer.close()` حالا با `await writer.wait_closed()` همراه است
- **`_dispatch()`** — حالا یک `bool` برمی‌گرداند؛ reader loop یک `_flush()` برای کل batch رویدادها انجام می‌دهد به جای یکی به ازای هر رویداد — کاهش چشمگیر lock contention
- **مسیر timeout** — stream با `RST_STREAM` پاک‌سازی می‌شود تا سرور منابع را آزاد کند
- **رویداد `WindowUpdated`** — `pass` بود؛ حالا `need_flush = True` برمی‌گرداند
- **`RemoteSettingsChanged`** — حالا handle می‌شود؛ وقتی سرور `MAX_CONCURRENT_STREAMS` را کاهش می‌دهد، semaphore به‌روز می‌شود
- **`ConnectionTerminated` (GOAWAY)** — نادیده گرفته می‌شد؛ حالا `_connected = False` می‌شود
- **`_reader_loop()`** — timeout 120 ثانیه‌ای با PING خودکار برای تشخیص connection مرده اضافه شد
- **`_send_body()`** — تخلیه پنجره flow-control حالا log می‌شود به جای اینکه داده‌ها بی‌صدا رها شوند
- **semaphore `max_concurrent`** — برای جلوگیری از فشار بیش از حد روی `MAX_CONCURRENT_STREAMS` سرور اضافه شد
- **ثوابت** به سطح کلاس منتقل شدند

### `Code.gs` (Google Apps Script)
- **محافظت SSRF** — اعتبارسنجی URL با مسدود کردن `localhost`، `127.x`، `169.254.x.x` و محدوده‌های RFC-1918 اضافه شد
- **رفع آسیب‌پذیری prototype pollution** — `SKIP_HEADERS` از plain object به `new Set()` + `.has()` تبدیل شد
- **fallback `contentType`** — بدون نوع محتوا، Apps Script گاهی داده‌های باینری را خراب می‌کرد؛ حالا پیش‌فرض `application/octet-stream` است
- **micro-cache** — cache 5 ثانیه‌ای درون‌فرآیندی برای GET های یکسان اضافه شد؛ بدون مصرف quota
- **`URL_RE`** — regex حالا یکبار کامپایل می‌شود به جای هر درخواست
- **هلپر `_encodeResponse()`** — بلوک تکراری `getResponseCode/getHeaders/getContent/base64Encode` استخراج شد
- **محافظ batch خالی** — قبل از صدا زدن `fetchAll([])` بررسی می‌شود
- **`const`/`let`** — `var` با scoping مناسب جایگزین شد

---

## راهنمای نصب گام به گام

### مرحله ۱: دانلود پروژه

```bash
git clone https://github.com/ItsRedDev/MasterHttpRelayVPNNew.git
cd MasterHttpRelayVPNNew
pip install -r requirements.txt
```

> **اگر دسترسی به PyPI ندارید:**
> ```bash
> pip install -r requirements.txt -i https://mirror-pypi.runflare.com/simple/ --trusted-host mirror-pypi.runflare.com
> ```

### مرحله ۲: راه‌اندازی relay گوگل (Code.gs)

۱. به [Google Apps Script](https://script.google.com/) بروید و وارد شوید.
۲. روی **New project** کلیک کنید.
۳. همه کد پیش‌فرض را پاک کنید.
۴. محتوای فایل `Code.gs` را کپی و paste کنید.
۵. **کلید احراز هویت را تغییر دهید:**
   ```javascript
   const AUTH_KEY = "رمز-سری-خودتان";
   ```
۶. روی **Deploy → New deployment** کلیک کنید.
۷. نوع: **Web app** | اجرا به عنوان: **Me** | دسترسی: **Anyone**
۸. Deploy کنید و **Deployment ID** را کپی کنید.

> ⚠️ اگر بعداً `Code.gs` را ویرایش کردید، حتماً **deployment جدید** بسازید — ویرایش کد به تنهایی نسخه زنده را آپدیت نمی‌کند.

### مرحله ۳: پیکربندی

```bash
cp config.example.json config.json
```

`config.json` را ویرایش کنید:
```json
{
  "mode": "apps_script",
  "google_ip": "216.239.38.120",
  "front_domain": "www.google.com",
  "script_id": "DEPLOYMENT_ID_خود_را_اینجا_بگذارید",
  "auth_key": "رمز-سری-خودتان",
  "listen_host": "127.0.0.1",
  "listen_port": 8085,
  "log_level": "INFO",
  "verify_ssl": true
}
```

### مرحله ۴: اجرا

```bash
python main.py
```

در اولین اجرا، صفحه امضای انیمیشنی ۸ ثانیه نمایش داده می‌شود، سپس پروکسی روی `127.0.0.1:8085` شروع به کار می‌کند.

### مرحله ۵: تنظیم مرورگر

| مرورگر | نحوه تنظیم |
|--------|-----------|
| Firefox | Settings → General → Network Settings → Manual proxy → آدرس `127.0.0.1` پورت `8085` → تیک "Also use this proxy for HTTPS" |
| Chrome/Edge | تنظیمات ویندوز → شبکه → پروکسی → دستی → `127.0.0.1:8085` |
| هر مرورگری | از [FoxyProxy](https://addons.mozilla.org/en-US/firefox/addon/foxyproxy-standard/) یا [SwitchyOmega](https://chrome.google.com/webstore/detail/proxy-switchyomega/) استفاده کنید |

### مرحله ۶: نصب گواهی CA (برای HTTPS)

ابزار در اولین اجرا گواهی CA در مسیر `ca/ca.crt` می‌سازد. باید آن را نصب کنید وگرنه مرورگر هشدار امنیتی نشان می‌دهد.

**ویندوز**
۱. روی `ca/ca.crt` دوبار کلیک کنید → Install Certificate → Current User
۲. در **Trusted Root Certification Authorities** قرار دهید
۳. مرورگر را ریستارت کنید

**macOS**
۱. روی `ca/ca.crt` دوبار کلیک کنید → Keychain Access باز می‌شود
۲. گواهی را پیدا کنید → دوبار کلیک → Trust → **Always Trust**
۳. مرورگر را ریستارت کنید

**لینوکس (Ubuntu/Debian)**
```bash
sudo cp ca/ca.crt /usr/local/share/ca-certificates/masterhttp-relay.crt
sudo update-ca-certificates
```

**Firefox (در همه سیستم‌عامل‌ها باید جداگانه انجام شود)**
۱. Settings → Privacy & Security → Certificates → View Certificates
۲. سربرگ Authorities → Import → فایل `ca/ca.crt` را انتخاب کنید
۳. تیک **Trust this CA to identify websites** → OK

> پروکسی در هنگام شروع تلاش می‌کند گواهی را خودکار نصب کند. اگر موفق نشد: `python main.py --install-cert`

---

## حالت‌های کار

| حالت | نیاز دارد | توضیح |
|------|----------|-------|
| `apps_script` | حساب گوگل رایگان | **پیشنهادی.** رایگان، بدون نیاز به سرور. |
| `google_fronting` | Google Cloud Run | سرویس Cloud Run خودتان پشت CDN گوگل. |
| `domain_fronting` | Cloudflare Worker | یک Worker کلودفلر به عنوان relay. |
| `custom_domain` | دامنه روی Cloudflare | اتصال مستقیم به دامنه کلودفلر شما. |

### توزیع بار (افزایش سرعت)

`Code.gs` را در چند پروژه مجزا deploy کنید و همه ID ها را لیست کنید:

```json
{
  "script_ids": [
    "DEPLOYMENT_ID_1",
    "DEPLOYMENT_ID_2",
    "DEPLOYMENT_ID_3"
  ]
}
```

---

## مرجع پیکربندی

| تنظیم | پیش‌فرض | توضیح |
|-------|---------|-------|
| `mode` | — | نوع relay |
| `auth_key` | — | رمز مشترک بین پروکسی و relay |
| `script_id` | — | Deployment ID اپ اسکریپت |
| `listen_host` | `127.0.0.1` | آدرس شنود پروکسی |
| `listen_port` | `8085` | پورت شنود |
| `log_level` | `INFO` | سطح جزئیات لاگ |
| `google_ip` | `216.239.38.120` | IP گوگل برای اتصال |
| `front_domain` | `www.google.com` | دامنه نمایشی به فیلتر |
| `verify_ssl` | `true` | بررسی گواهی TLS |
| `script_ids` | — | آرایه‌ای از ID ها برای توزیع بار |

---

## گزینه‌های خط فرمان

```bash
python main.py                          # شروع معمول
python main.py -p 9090                  # پورت دیگر
python main.py --log-level DEBUG        # لاگ تفصیلی
python main.py -c /path/to/config.json  # فایل config دیگر
python main.py --install-cert           # نصب گواهی CA و خروج
python main.py --no-cert-check          # رد شدن از بررسی CA در شروع
```

---

## فایل‌های پروژه

| فایل | توضیح |
|------|-------|
| `main.py` | نقطه ورود، صفحه splash، پردازش آرگومان‌ها |
| `proxy_server.py` | مدیریت اتصالات مرورگر، منطق پروکسی HTTP/CONNECT |
| `domain_fronter.py` | domain fronting، relay، batching، coalescing |
| `h2_transport.py` | انتقال HTTP/2 مالتی‌پلکس (اختیاری، سریع‌تر) |
| `mitm.py` | تولید و رهگیری گواهی HTTPS |
| `cert_installer.py` | نصب‌کننده CA برای همه سیستم‌عامل‌ها |
| `ws.py` | فریم‌بندی WebSocket |
| `Code.gs` | اسکریپت relay گوگل — این را deploy کنید |
| `config.example.json` | نمونه پیکربندی — کپی به `config.json` |

---

## عیب‌یابی

| مشکل | راه‌حل |
|------|--------|
| «Config not found» | `config.example.json` را به `config.json` کپی کنید |
| خطای گواهی در مرورگر | `ca/ca.crt` را نصب کنید (مرحله ۶) |
| تلگرام کار می‌کند ولی وب باز نمی‌شود | گواهی CA نصب نشده — مرحله ۶ را دنبال کنید، سپس مرورگر را **کاملاً ببندید** و دوباره باز کنید |
| گواهی نصب شده ولی هنوز خطا | Chrome/Edge گواهی‌ها را cache می‌کند — از Task Manager همه process های مرورگر را ببندید |
| خطای «unauthorized» | `auth_key` در `config.json` باید دقیقاً با `AUTH_KEY` در `Code.gs` یکسان باشد |
| timeout اتصال | یک `google_ip` دیگر امتحان کنید |
| مرور کند | چند نسخه `Code.gs` deploy کنید و از `script_ids` استفاده کنید |
| خطای `502 Bad JSON` | `script_id` اشتباه است یا بعد از ویرایش `Code.gs` deployment جدید نساختید |

---

## معماری

```
┌──────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────┐
│  مرورگر  │───►│  پروکسی محلی │───►│ Google / CDN │───►│  Relay   │──► اینترنت
│          │◄───│  (این ابزار) │◄───│  (fronted)   │◄───│ Endpoint │◄──
└──────────┘    └──────────────┘    └──────────────┘    └──────────┘
                  HTTP/CONNECT        TLS (SNI: ok)       دریافت مقصد
                  MITM اختیاری       Host: relay           ارسال پاسخ
```

---

## نکات امنیتی

- **`AUTH_KEY` را در `Code.gs` تغییر دهید** — مقدار پیش‌فرض عمومی است.
- **`config.json` را با کسی به اشتراک نگذارید** — حاوی رمز شما است.
- **پوشه `ca/` را به اشتراک نگذارید** — حاوی کلید خصوصی گواهی است.
- `listen_host` را روی `127.0.0.1` نگه دارید تا فقط کامپیوتر خودتان از پروکسی استفاده کند.

---

## تقدیر و تشکر

- **پروژه اصلی:** [MasterHttpRelayVPN](https://github.com/masterking32/MasterHttpRelayVPN) توسط [@masterking32](https://github.com/masterking32)
- **تشکر ویژه:** از [@abolix](https://github.com/abolix) که این پروژه را ممکن ساخت
- **این نسخه:** بهبودهای عملکردی، صحت و امنیت — دانشگاه تورنتو

---

## سلب مسئولیت

این ابزار صرفاً برای اهداف آموزشی، آزمایشی و پژوهشی ارائه می‌شود. توسعه‌دهندگان هیچ مسئولیتی در قبال خسارات ناشی از استفاده ندارند. شما مسئول رعایت قوانین کشور خود و شرایط خدمات گوگل هستید.

## مجوز

MIT

</div>
