# ShadowShopBot
Shadow Shop bot
🌑 Shadow Shop Bot
ربات حرفه‌ای تلگرام برای فروش محصولات دیجیتال اختصاصی، مدیریت فروشگاه، شارژ کیف پول، کدهای تخفیف، دریافت رسیدهای کارت‌به‌کارت، پرداخت با استارز تلگرام و ولت‌های رمزارز.

🛍 ساختار فروش محصولات (Digital Goods Delivery)
در این ربات، تمام محصولات به‌صورت یک‌بار مصرف و اختصاصی عرضه می‌شوند:

تحویل یک‌بار مصرف: هر اکانت، کد، لایسنس یا محتوایی که در فروشگاه ثبت می‌شود، تنها به یک مشتری فروخته می‌شود.

خروج از چرخه فروش: بلافاصله پس از خرید محصول توسط یک کاربر، آن آیتم از لیست فروشگاه خارج شده و به مشتری دیگری ارائه نخواهد شد.

تحویل خودکار و آنی: متن تحویل (محتوای اختصاصی محصول) پس از کسر از موجودی به‌صورت فوری برای خریدار ارسال می‌گردد.

📋 ویژگی‌ها و امکانات (Features)
🛍 فروشگاه و محصولات اختصاصی: دسته‌بندی محصولات و تحویل تک‌کاربره (Exclusive / One-time per customer).

💰 کیف پول و روش‌های متنوع شارژ:

کارت به کارت (با بررسی و تأیید رسید توسط ادمین).

پرداخت هوشمند با Telegram Stars (XTR).

شارژ حساب با ولت‌های کریپتو (USDT ،TRC20 و...).

🎟 کدهای تخفیف: امکان ساخت کدهای تخفیف درصدی یا ثابت همراه با محدودیت استفاده، حداقل خرید و سقف تخفیف.

⚙️ پنل مدیریت جامع: مدیریت کاربران، گزارش‌گیری، مسدودسازی/رفع‌مسدودی، ارسال پیام همگانی (Broadcast) و دریافت بکاپ دیتابیس SQLite.

🌐 پشتیبانی از چند زبان: قابلیت تغییر زبان رابط کاربری (فارسی / انگلیسی).

🛠 پیش‌نیازها (Prerequisites)
قبل از راه‌اندازی، مطمئن شوید که موارد زیر روی سرور لینوکس شما نصب هستند:

Python 3.10+

pip (مدیریت پکیج‌های پایتون)

git (برای دریافت سورس کد)

🚀 مراحل نصب و اجرا در لینوکس (Step-by-Step Setup)
۱. به‌روزرسانی سیستم و نصب ابزارهای پایه
دستورات زیر را در ترمینال لینوکس اجرا کنید:

Bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3 python3-pip python3-venv git -y
۲. کلون کردن پروژه و ورود به پوشه برنامه
Bash
git clone https://github.com/YOUR_USERNAME/ShadowShopBot.git
cd ShadowShopBot
(به جای YOUR_USERNAME نام کاربری گیت‌هاب خود را قرار دهید)

۳. ساخت و فعال‌سازی محیط مجازی (Virtual Environment)
Bash
python3 -m venv venv
source venv/bin/activate
۴. نصب کتابخانه‌های مورد نیاز
Bash
pip install --upgrade pip
pip install python-telegram-bot httpx
⚙️ تنظیمات اولیه (Configuration)
داخل فایل Shadow_bot.py مقادیر زیر را با اطلاعات خود جایگزین کنید:

BOT_TOKEN: توکن دریافت شده از BotFather@

ADMIN_ID: آیدی عددی تلگرام ادمین اصلی

TELEGRAM_PROXY: (اختیاری) پروکسی SOCKS5 در صورت نیاز به عبور از محدودیت‌ها برای اتصال به API تلگرام.

🏃‍♂️ نحوه اجرا (Running the Bot)
اجرای موقت (جهت تست):
Bash
python3 Shadow_bot.py
اجرای دائمی و همیشگی در پس‌زمینه (Systemd Service)
برای اینکه ربات همیشه در پس‌زمینه روشن بماند و پس از ری‌استارت سرور خودکار اجرا شود:

۱. ساخت فایل سرویس:

Bash
sudo nano /etc/systemd/system/shadowshop.service
۲. قرار دادن محتوای زیر (مسیرها را مطابق مسیر فایل در سرور خود تنظیم کنید):

Ini, TOML
[Unit]
Description=Shadow Shop Telegram Bot
After=network.target

[Service]
User=root
WorkingDirectory=/root/ShadowShopBot
ExecStart=/root/ShadowShopBot/venv/bin/python3 /root/ShadowShopBot/Shadow_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
۳. فعال‌سازی و شروع سرویس:

Bash
sudo systemctl daemon-reload
sudo systemctl enable shadowshop
sudo systemctl start shadowshop
۴. بررسی وضعیت سرویس:

Bash
sudo systemctl status shadowshop
📦 تهیه بکاپ (Database Backup)
اطلاعات کاربران، محصولات، خریدهای انجام‌شده و تراکنش‌ها در فایل bot.db ذخیره می‌شوند. می‌توانید مستقیم از داخل پنل ادمین ربات با کلیک روی دکمه «📥 دریافت بکاپ دیتابیس» فایل بکاپ کامل را در تلگرام دریافت کنید.
