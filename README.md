<div dir="rtl">

# מחזירים לחיים את הסורק Avision AV210C2 ב-Windows 10/11

סורק הדפים Avision AV210C2 (מזהה USB:‏ `0638:0A3A`) הוא סורק מצוין — אבל Avision מעולם לא פרסמה עבורו דרייבר רשמי ל-Windows 10/11. מחברים אותו למחשב משרדי מודרני, ו-Windows פשוט לא יודע מה לעשות איתו.

המאגר הזה פותר את הבעיה, בשלוש דרכים — לפי סדר ההמלצה שלנו.

## מה הבעיה בעצם?

- הסורק תקין לחלוטין מבחינת חומרה, אבל **אין מנהל התקן (דרייבר) רשמי** ל-Windows 10/11.
- לעומת זאת, בעולם הלינוקס קיים דרייבר קוד פתוח בשל ומוכח — ה-backend‏ `avision` של פרויקט SANE — שתומך בדגם הזה **תמיכה מלאה** (סטטוס `complete`) כבר שנים רבות.
- הפתרון המומלץ שלנו פשוט מגשר בין השניים: הסורק מחובר ל-USB של מחשב ה-Windows, הדרייבר הפתוח רץ בתוך WSL2 (לינוקס קטן שמובנה ב-Windows), והסריקה חוזרת אליכם דרך הדפדפן או תוכנת סריקה רגילה.

## שלושת הפתרונות

| פתרון | מה זה | קלות התקנה | עלות | למי מתאים |
|---|---|---|---|---|
| **1.‏ windows-bridge** (מומלץ) | סקריפט התקנה אחד שמקים WSL2 + הדרייבר הפתוח + שרת סריקה. סורקים מהדפדפן או מ-NAPS2 | הרצת סקריפט אחד כמנהל, כ-10 דקות (ייתכן אתחול אחד) | חינם | כל משרד; איש IT מתקין פעם אחת, ואחר כך כל עובד סורק לבד |
| **2.‏ userspace-driver** (מתקדם) | דרייבר Python עצמאי שמדבר עם הסורק ישירות דרך USB, עם שורת פקודה: `av210 probe`,‏ `av210 scan` | דורש החלפת דרייבר עם Zadig והתקנת Python | חינם | אנשי IT, אבחון תקלות, משתמשי Linux/Mac, אוטומציה |
| **3.‏ VueScan** (מסחרי) | תוכנה מ-hamrick.com עם דרייבר מובנה בדיוק לדגם הזה. ללא WSL, ללא סקריפטים | התקנה רגילה של תוכנת Windows | רישיון חד-פעמי בתשלום; גרסת ניסיון חינם (עם סימן מים) | מי שרוצה אפס תחזוקה ומוכן לשלם |

## התחלה מהירה

### פתרון 1 — windows-bridge (מומלץ)

1. חברו את הסורק **ישירות** ליציאת USB במחשב (לא דרך מפצל) וודאו שהוא דלוק (ספק ה-24V מחובר).
2. פתחו PowerShell **כמנהל מערכת** בתיקיית `windows-bridge`, והריצו:

</div>

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

<div dir="rtl">

3. עקבו אחרי ההודעות על המסך (הן מופיעות בעברית ובאנגלית). ייתכן שתתבקשו לאתחל את המחשב פעם אחת ולהריץ שוב — הסקריפט ממשיך מאותה נקודה.
4. בסיום — סורקים מהדפדפן: <span dir="ltr">`http://localhost:8090`</span> (או <span dir="ltr">`http://localhost:8080`</span> לממשק נוח יותר), או מתוכנת NAPS2.

מדריך מפורט צעד-אחר-צעד: [docs/QUICKSTART.he.md](docs/QUICKSTART.he.md)

### פתרון 2 — userspace-driver (מתקדם)

לאבחון, לאוטומציה או לשימוש ישיר ללא WSL. ב-Windows נדרשת קודם החלפת הדרייבר של ההתקן ל-WinUSB בעזרת Zadig — ההוראות המלאות ב-`userspace-driver/README.md`. לאחר מכן:

</div>

```bash
pip install ./userspace-driver
av210 probe
av210 scan -o document.pdf --resolution 300 --mode color --all-pages
```

<div dir="rtl">

`av210 probe` הוא גם כלי האבחון של הפרויקט: הוא בודק תקשורת בסיסית מול הסורק ומדפיס את נתוני הזיהוי שלו — שימושי כשרוצים לדעת אם הבעיה בחומרה או בתוכנה.

### פתרון 3 — VueScan

הורידו מ-<span dir="ltr">https://www.hamrick.com/vuescan/avision_av210c2.html</span> — לחברת Hamrick יש דף תמיכה ייעודי בדיוק לדגם הזה, עם דרייבר משלהם שמותקן אוטומטית. גרסת הניסיון החינמית (מוסיפה סימן מים לסריקות) היא דרך מהירה **לוודא שהסורק עצמו תקין** לפני שמשקיעים בהתקנה כלשהי.

שימו לב: אחרי התקנת windows-bridge הסורק "שייך" ל-WSL ותוכנות Windows לא יראו אותו ישירות — כדי לחזור ל-VueScan הריצו קודם את `windows-bridge\uninstall.ps1`.

## שקיפות מלאה — מה בדקנו ומה לא

- הפתרון המומלץ **לא ממציא דרייבר חדש**: הוא עוטף את דרייבר הקוד הפתוח `avision` של SANE, שנמצא בשימוש ובתחזוקה כ-20 שנה ותומך בדגם הזה באופן מלא ומתועד.
- **סביבת הפיתוח של המאגר הזה לא כללה את הסורק הפיזי** — לא בוצעה בדיקה מקצה-לקצה מול חומרה אמיתית. כל פקודה וכל עובדה במסמכים אומתו מול התיעוד והקוד הרשמיים של הרכיבים עצמם.
- לכן צירפנו כלי אבחון (`av210 probe`) ותהליך אימות מסודר: סקריפט ההתקנה בודק בעצמו שהסורק מזוהה (בעזרת `scanimage -L`) ומדפיס בכל שלב מה אמור לקרות. אם משהו לא תואם — [docs/TROUBLESHOOTING.he.md](docs/TROUBLESHOOTING.he.md) בנוי בדיוק בשביל זה, ו-VueScan זמין כרשת ביטחון.

## מסמכים

| מסמך | תוכן |
|---|---|
| [docs/QUICKSTART.he.md](docs/QUICKSTART.he.md) | מדריך התקנה מלא צעד-אחר-צעד (עברית) |
| [docs/TROUBLESHOOTING.he.md](docs/TROUBLESHOOTING.he.md) | טבלת תקלות ופתרונות + איך אוספים לוגים (עברית) |
| [docs/NAPS2.he.md](docs/NAPS2.he.md) | סריקה עם NAPS2, פרופיל מומלץ, ‏OCR בעברית (עברית) |
| [docs/OFFICE-ONEPAGER.he.md](docs/OFFICE-ONEPAGER.he.md) | דף אחד להדפסה ולתלייה ליד הסורק (עברית) |
| [docs/SHARE-LAN.he.md](docs/SHARE-LAN.he.md) | שיתוף הסורק לכל מחשבי המשרד דרך הרשת (עברית) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | ארכיטקטורה, אבטחה והסרה — לאיש ה-IT (אנגלית) |
| `windows-bridge/README.md` | תיעוד טכני של סקריפטי ההתקנה (אנגלית) |
| `userspace-driver/README.md` | תיעוד הדרייבר העצמאי (אנגלית) |

</div>

---

## English summary (for IT)

The **Avision AV210C2** sheetfed scanner (USB `0638:0A3A`) has no official Windows 10/11 driver. This repo provides three ways to keep it in service:

1. **`windows-bridge/` (recommended)** — a single Administrator PowerShell script (`install.ps1`) that sets up WSL2 (Ubuntu-24.04), usbipd-win, the mature SANE `avision` backend (which supports this model with status *complete*), and AirSane, which re-exposes the scanner as a standard eSCL/AirScan device plus a browser UI. After install, users scan at `http://localhost:8090` (AirSane) / `http://localhost:8080` (scanservjs), or via NAPS2's ESCL driver with Manual IP `localhost:8090`. A logon scheduled task keeps the USB device attached to WSL across reboots and replugs. Free, open source, no cloud; with WSL's default NAT networking everything is reachable only from the local machine.
2. **`userspace-driver/` (advanced)** — a portable Python/libusb userspace driver (WinUSB via Zadig on Windows; plain udev rules on Linux) that ports the SANE avision protocol. CLI: `av210 probe` (diagnostics), `av210 scan -o file.pdf --resolution 300 --mode color --all-pages`. Best for diagnostics, scripting, and non-Windows hosts.
3. **VueScan (commercial fallback)** — Hamrick ships a reverse-engineered driver for this exact model on Windows 10/11; zero setup complexity, one-time license, and a free watermarked trial that is useful for validating hardware health before any other investment.

Honest framing: the recommended path wraps a driver with ~20 years of field testing, but this repo itself was developed **without the physical scanner attached** — end-to-end hardware verification is on you. `av210 probe`, the installer's built-in `scanimage -L` check, and [docs/TROUBLESHOOTING.he.md](docs/TROUBLESHOOTING.he.md) exist precisely for that. Architecture, security notes, and uninstall: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
