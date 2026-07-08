<div dir="rtl">

# פתרון תקלות — הסורק לא עובד?

עברו על הטבלה מלמעלה למטה — היא מסודרת לפי סדר שרשרת החיבור: חומרה ← חיבור ל-WSL ← דרייבר ← שרת סריקה ← תוכנת הסריקה. התקלה נמצאת תמיד בחוליה הראשונה שלא מתנהגת כמצופה.

כלל אצבע ראשון לפני הכול: **הרצה חוזרת של `install.ps1` (כמנהל) בטוחה תמיד** ופותרת חלק גדול מהתקלות — היא מזהה מה תקין וממשיכה משם.

## טבלת תקלות

| מה קורה | סיבה סבירה | מה עושים |
|---|---|---|
| הסורק לא מופיע ב-`usbipd list` | הסורק כבוי, **ספק ה-24V לא מחובר**, כבל USB פגום, או חיבור דרך מפצל | ודאו שנורית ההפעלה דולקת; החליפו כבל; חברו ישירות ליציאת USB במחשב (עדיף אחורית בנייח); הריצו שוב `usbipd list`. כשהסורק מופיע — הריצו שוב את `install.ps1` |
| `usbipd list` מציג את הסורק אבל תוכנות לא רואות אותו | החיבור ל-WSL‏ (attach) נפל — הוא **אף פעם לא נשמר לבד** אחרי אתחול או ניתוק כבל; המשימה המתוזמנת אמורה לטפל בזה | בדקו את המשימה בהרצת <span dir="ltr">`Get-ScheduledTask ScannerBridge-AttachAV210C2`</span> ב-PowerShell; הפעילו ידנית: <span dir="ltr">`Start-ScheduledTask ScannerBridge-AttachAV210C2`</span>; הציצו בלוג (ראו "איסוף לוגים" למטה) |
| ה-attach נכשל אחרי ניתוק-חיבור של הכבל | הסורק חובר ליציאת USB **אחרת** — הכתובת (BUSID) השתנתה | החזירו את הכבל ליציאה המקורית, או הריצו שוב את `install.ps1` כדי שילמד את הכתובת החדשה. השתדלו להשאיר את הסורק תמיד באותה יציאה |
| <span dir="ltr">`scanimage -L`</span> בתוך WSL לא מציג כלום | או שהסורק לא הגיע ל-WSL, או בעיית הרשאות בלינוקס | הריצו בתוך WSL‏ <span dir="ltr">`lsusb`</span> (ראו פקודות למטה). אם `0638:0a3a` לא מופיע — זו בעיית attach, ראו שתי שורות למעלה. אם מופיע אבל הסריקה לא — הריצו שוב את `install.ps1` (מתקין מחדש את כללי ההרשאות), ואז נתקו וחברו את כבל ה-USB פעם אחת |
| הדפדפן לא נפתח / <span dir="ltr">`http://localhost:8090`</span> מחזיר שגיאת חיבור | שרת הסריקה (airsaned) לא רץ, או ש-WSL בכלל לא הופעל מאז ההדלקה | התנתקו והתחברו מחדש למשתמש Windows (זה מפעיל את המשימה המתוזמנת שמעירה את WSL), או הריצו ב-PowerShell:‏ <span dir="ltr">`wsl -d Ubuntu-24.04 -u root -- systemctl status airsaned`</span> ובדקו מה כתוב |
| NAPS2 לא מוצא את הסורק בחיפוש אוטומטי | זה צפוי — גילוי אוטומטי (mDNS) של סורק שיושב בתוך WSL לא אמין | אל תשתמשו בחיפוש: בחרו בדרייבר ESCL עם הזנת כתובת ידנית (Manual IP) והקלידו `localhost:8090`. פירוט: [NAPS2.he.md](NAPS2.he.md) |
| הגדרות Windows ← "הוספת התקן" לא מוצאות את הסורק | אותה סיבה — גילוי mDNS בין Windows ל-WSL על אותו מחשב לא אמין, ואין ב-Windows הוספת סורק eSCL לפי כתובת | לא תקלה — פשוט השתמשו בדפדפן או ב-NAPS2 |
| מחשבים אחרים במשרד לא רואים את הסורק | ברירת המחדל (רשת NAT של WSL) חושפת את השרת רק למחשב המקומי | זו התנהגות מכוונת ובטוחה. לשיתוף ברשת — פנו לאיש ה-IT (ראו [ARCHITECTURE.md](ARCHITECTURE.md)) |
| הכול מסתבך ודחוף לסרוק **עכשיו** | — | פתרון ביניים: התקינו VueScan מ-<span dir="ltr">https://www.hamrick.com/vuescan/avision_av210c2.html</span> — יש לו דרייבר משלו לדגם הזה. חשוב: קודם הריצו <span dir="ltr">`windows-bridge\uninstall.ps1`</span> כדי להחזיר את הסורק ל-Windows. גרסת הניסיון חינמית (עם סימן מים) |

## בדיקות ידניות — שרשרת האבחון המלאה

הריצו את הפקודות לפי הסדר ב-PowerShell (לא חייבים כמנהל). כל פקודה בודקת חוליה אחת בשרשרת:

**1. האם Windows רואה את הסורק ב-USB?**

</div>

```powershell
usbipd list
```

<div dir="rtl">

מצפים לשורה שמכילה `0638:0a3a`, בסטטוס `Shared` או `Attached`. אם הסטטוס `Not shared` — הריצו שוב את `install.ps1` כמנהל.

**2. האם הסורק הגיע אל תוך WSL?**

</div>

```powershell
wsl -d Ubuntu-24.04 -u root -- lsusb
```

<div dir="rtl">

מצפים לשורה עם <span dir="ltr">`ID 0638:0a3a`</span>.

**3. האם הדרייבר מזהה את הסורק?**

</div>

```powershell
wsl -d Ubuntu-24.04 -u root -- scanimage -L
```

<div dir="rtl">

מצפים לשורה שמתחילה ב-<span dir="ltr">`device 'avision:libusb:...'`</span>.

**4. האם שרת הסריקה חי?**

</div>

```powershell
wsl -d Ubuntu-24.04 -u root -- systemctl is-active airsaned
```

<div dir="rtl">

מצפים לתשובה `active`. ואז בדפדפן: <span dir="ltr">`http://localhost:8090`</span>.

החוליה הראשונה שנכשלת — שם התקלה. חפשו אותה בטבלה למעלה.

## איסוף לוגים (כשפונים לעזרה)

כשפונים לאיש ה-IT או פותחים תקלה, צרפו את הפלט של הפקודות הבאות — הן אוספות את כל המידע הרלוונטי לקובץ אחד על שולחן העבודה:

</div>

```powershell
$log = "$env:USERPROFILE\Desktop\scanner-debug.txt"
"=== usbipd list ==="            | Out-File $log
usbipd list                      | Out-File $log -Append
"=== lsusb ==="                  | Out-File $log -Append
wsl -d Ubuntu-24.04 -u root -- lsusb                        | Out-File $log -Append
"=== scanimage -L ==="           | Out-File $log -Append
wsl -d Ubuntu-24.04 -u root -- scanimage -L                 | Out-File $log -Append
"=== airsaned journal ==="       | Out-File $log -Append
wsl -d Ubuntu-24.04 -u root -- journalctl -u airsaned -n 100 --no-pager | Out-File $log -Append
"=== attach task log ==="        | Out-File $log -Append
Get-Content "$env:ProgramData\ScannerBridge\attach.log" -Tail 100 -ErrorAction SilentlyContinue | Out-File $log -Append
```

<div dir="rtl">

הקובץ `scanner-debug.txt` ייווצר על שולחן העבודה — שלחו אותו כמו שהוא.

בנוסף, לאבחון חומרה בלתי-תלוי לגמרי ב-WSL, איש ה-IT יכול להשתמש בכלי `av210 probe` מתיקיית `userspace-driver` (דורש Zadig — ראו את ה-README שם). אם `probe` מצליח לתקשר עם הסורק, החומרה תקינה והבעיה בצד התוכנה.

</div>
