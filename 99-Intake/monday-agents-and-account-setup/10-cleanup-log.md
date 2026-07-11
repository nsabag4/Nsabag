# יומן ניקוי כפילויות — 05.07.2026
**שיטה:** לפני כל מחיקה — אימות API שהעמודה ריקה בכל הפריטים. עמודה עם דאטה אינה נמחקת בשום מקרה.
**אישור:** נחי אישר "אישור לניקוי כפילויות" + ביקש גיבוי מבנה Leads לפני מחיקה (בוצע — קבצים 08-09).

## סבב 1 — בוצע ✅ (12 עמודות נמחקו, אישור API לכל אחת)
| לוח | עמודה שנמחקה | column_id | אימות לפני מחיקה |
|---|---|---|---|
| ניהול פרוייקטים (5043629033) | link to תיק לקוח 🗂️🗂️ (כפול 2) | board_relation_mkwssmmh | ריקה בכל 10 הפריטים |
| ניהול פרוייקטים | link to תיק לקוח 🗂️🗂️ (כפול 3) | board_relation_mkwsamtn | ריקה |
| ניהול פרוייקטים | link to תיק לקוח 🗂️🗂️ (כפול 4) | board_relation_mkws1e5c | ריקה (נשמר העותק board_relation_mkwst7dz) |
| תהליך פרוייקט (5043736686) | link to Project details | link_to_project_details | ריקה בכל 6 הפריטים (נשמר link_to_item24) |
| Contacts CRM (1494474506) | Title (text) | text_mkx01b59 | ריקה; Title האמיתי (dropdown title5 עם CEO/COO) נשמר |
| Contacts CRM | Dup. of Deals value (mirror) | lookup_mkwhs732 | עמודה מחושבת, ללא דאטה עצמאי |
| מחיר למשתכן (5091523854) | Mirror | lookup_mm0ed5nh | ניזונה רק מהקישור הריק board_relation_mm0etzp2 (אומת ב-settings_str) |
| מחיר למשתכן | Mirror 1 | lookup_mm0es9j5 | ניזונה רק מהקישור הריק board_relation_mm0ehfh7 |
| מחיר למשתכן | מחיר למשתכן כולל (קישור עצמי) | board_relation_mm0etzp2 | ריקה בכל 564 הפריטים (בדיקת is_not_empty) |
| מחיר למשתכן | link to מחיר למשתכן כולל (קישור עצמי) | board_relation_mm0ehfh7 | ריקה בכל 564 הפריטים |
| מחיר למשתכן | Button (כפול) | button_mm0e5fg0 | עמודת כפתור — אינה מחזיקה דאטה |
| מחיר למשתכן | Auto number 1 (כפול) | autonumber_mm3930er | מספור אוטומטי — אינו דאטה משתמש |

## סבב 2 — Leads (1494474504): מאושר, גובה, ממתין ל"בצע" סופי ⏸️
7 עמודות אומתו ריקות מול כל 1,033 הלידים (בדיקת is_not_empty) + אומת שאף אוטומציה/webhook לא מפנה אליהן (0 הפניות בקובץ האוטומציות):
| עמודה | column_id |
|---|---|
| status (text) | text_mm3npbyr |
| itemId (text) | text_mm3n4s9v |
| error (text) | text_mm3nmf8c |
| Text (text) | text_mm3yj3q6 |
| Text 2 (text) | text_mm3ynfdz |
| Text 3 (text) | text_mm3yzfqf |
| Phone 1 (phone) | phone_mm3z653a |

## לא נמחק — יש דאטה או שימוש 🔴
| לוח | עמודה | סיבה |
|---|---|---|
| Financial Accounting (5095227844) | Payment Date (date6) + Payment Date (date_1) | שתיהן מכילות תאריכים שונים (תשלום 1 מול תשלום 2) — לא כפילות אמיתית. מומלץ: שינוי שם ל-Payment Date 1/2 |
| מחיר למשתכן | Phone (text_mm0ev1kq) | מכילה דאטה (אומת: פריט 2709749012 "אביביל גבאי") |
| Leads | Text 1 (text_mkz7hnm0) | יש דאטה + webhook חיצוני מאזין לה |
| Leads | full name (text_mm3nsctx) | יש דאטה |
| Leads | Google Event Link (text_mm1kh5f6) | יש דאטה + webhook |
| Leads | Google Meet Link (text_mm1k3j0m) | יש דאטה |

## ממצאי לוואי חשובים
1. **אוטומציית וואטסאפ פעילה על Leads** (id 161018786) — מזהה הודעות וואטסאפ נכנסות מול עמודת lead_phone.
2. **9 webhooks פעילים (source=API) על Leads** — מערכת חיצונית לא מזוהה מאזינה ל: שינוי שם, Date, Timeline, Hour, הערות, Location, Text 1, יצירת פריט, מחיקת פריט. לברר מי בנה ולאן זה שולח לפני כל שינוי בעמודות האלו.
3. **עמודת "עדיפות" ב-Leads מזוהמת** — עשרות כתובות מייל נדחפו כתוויות סטטוס. דורש ניקוי ידני זהיר.
