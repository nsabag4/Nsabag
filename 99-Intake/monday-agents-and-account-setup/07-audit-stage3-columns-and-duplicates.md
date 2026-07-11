# ביקורת monday — שלב 3: מבנה עמודות וכפילויות
**נמשך מה-API בתאריך 05.07.2026** (שאילתות GraphQL חיות; לוחות המחלקות נקראו ב-get_board_info באותו סשן)

## מבנה משותף — 4 לוחות המשימות המחלקתיים (משפטי 5099714406 / תכנון 5099714408 / מימון 5099714411 / שיווק 5099714412)
| עמודה | סוג |
|---|---|
| Name | name |
| 📌 סטטוס (לביצוע/בטיפול/ממתין/תקוע/הושלם) | status |
| 🙋 אחראי | people |
| 📅 תאריך יעד | date |
| ⚠️ עדיפות (Q1–Q4) | status |
| 💰 דגל תזרים | checkbox |
| 🔁 פולואפ | date |
| 📝 הערות | long_text |
| 🗂️ תיק | board_relation → Master 5099714404 |

✅ אחידים לחלוטין — אין כפילויות פנימיות.
פערים שזוהו (בכל הארבעה): הקבוצה הראשונה היא "✅ הושלם" (פריט חדש נולד בקבוצת הושלם); "הושלם" עם is_done=false; אין Timeline; אין אף View; אין Mirror של סטטוס תיק.

## ⚖️ משפטית — לוחות נוספים
| לוח | עמודות |
|---|---|
| מחלקה משפטית (5090165194) | Name, Subitems, Person (people), Status, Date, סיווג (dropdown) |
| Legal – Registration (5090119602) | Name, Subitems, Person (people), Status, Date, monday Doc (doc) |

🚩 שני הלוחות הישנים חופפים בייעודם ל"⚖️ משפטי | משימות" — שכבה ישנה מול חדשה.

## 📣 שיווק ומכירות
| לוח | עמודות |
|---|---|
| Leads (1494474504) — 30 עמודות | Name, Phone (phone), אחראי (people), עדיפות (status), מצב סטטוס ליד (status), התרשמות נציג (rating), הערות (text), Date, Creation log, Text 1 (text), Email (email), Create a contact (button), FB Ads Ad Set (integration), Location, Hour, FB Ads Campaign (integration), Subitems, Timeline, Time tracking, Google Calendar event (integration), Google Event Link (text), Google Meet Link (text), full name (text), status (text), itemId (text), error (text), Text (text), Text 2 (text), Text 3 (text), Phone 1 (phone) |
| Deals (1494474505) | Name, Tasks (subtasks), סטטוס מיון (status), Owner (people), Deal Value (numbers), Contacts (board_relation), Expected Close Date (date), Close Probability (numbers), Forecast Value (formula) |
| Accounts (1494474512) | Name, Domain (link), Contacts (board_relation), Deals (mirror), Priority (status), Industry (dropdown), Description (long_text), No. of employees (text), HQ location (text), Type (status), Company profile (link), Timeline, Email (text) |
| Contacts (1494474506) | Name, Type (status), Accounts (board_relation), Deals (board_relation), Title (dropdown), Priority (status), Phone (phone), Email (email), Deals value (mirror), Dup. of Deals value (mirror), Comments (long_text), Title (text), Company (text), Status (status) |
| Activities (1494474507) | Name, Owner (people), Item (board_relation), Start time (date), End time (date), Status, Activity Type (status) |
| מחיר למשתכן (5091523854) — 27 עמודות | Name, Person, Phone (phone) + Phone (text), Email (text — לא email), Text 1, סטטוס, Date, התקשרות (status), אינפורמציה (status), Button ×2, board_relation לעצמו ×2, Auto number ×2, Mirror + Mirror 1, Group (text), First/Last Name, Notes, Creation log, קישור לרשימת זוכים, Time tracking, Subitems, קישור למרחב מזכירות |
| זלמן ארן 1 (5090296547) | Name, Person, Status, Date, Google Event Link (text), Google Meet Link (text) |
| sasz למכירה (5090200914) | Name, Subitems, Person, Status, Date, Text, Link |

| 🚩 כפילויות | פירוט |
|---|---|
| Leads — זבל אינטגרציה | 8 עמודות טקסט טכניות (full name, status, itemId, error, Text 1-3) + 2 עמודות Phone |
| מחיר למשתכן | Phone כפול, Button כפול, Auto number כפול, Mirror כפול, 2 קישורים לעצמו |
| Contacts | "Dup. of Deals value" + Title פעמיים (dropdown+text) |
| Google Event/Meet Link | גם ב-Leads וגם בזלמן ארן — עקבות סנכרון יומן ישן |
| עסקאות/יחידות | Deals, זלמן ארן, sasz — שלושה לוחות מכירה בלי קשר ביניהם |

## 💼 כספים
| לוח | עמודות |
|---|---|
| תזרים "החמצן" (5094859480) | Name, יעד אופטימי (date), יעד ריאלי (date), סטטוס תשלום (status), מגן מע"מ 18% (formula), סכום ברוטו (numbers), Subitems, כרית ביטחון 3 חודשים (formula), Creation log, חמצן נקי (formula), מטריצת גבייה (formula) |
| שער הקלט והנתב (5094858663) | Name, סכום גולמי (numbers), ישות (dropdown), סוג פעולה (status), מקור הכסף (dropdown), עדיפות תזרימית (status), ניתוב (status), תאריך ביצוע (date), סוג הקלט (status), Subitems, Last updated |
| הלוואות וחובות (5094863667) | Name, סכום (numbers), סוג תנועה (status), Subitems, סוג חוב (status), סטטוס ביצוע (status), תאריך פירעון (date), שיוך לפרויקט (board_relation), עתודת מס (formula) |
| הוצאות קבועות (5094867324) | Name, Frequency (dropdown), Item (text), Fixed Amount (numbers), Status, Day of Month (numbers), Category (dropdown) |
| 🟢 AR (5095228284) | Name, Client (board_relation), Invoice File (file), Amount, Billing Date, Status, CFO (people), Finance (people), Payment Method (dropdown), Days Overdue (formula), Email (mirror) |
| 🟠 AP (5095228283) | Name, Vendor (board_relation), Vendor's Email (mirror), Invoice File, Amount, Billing Date, Payment Confirmation (file), Status, Team Leader/CFO/Finance (people ×3), Days to Due (formula), Payment Method, Amount (-) (formula) |
| Financial Statement (5095229445) | Name, Approval, Gross Sales, Returns, NET Sales (formula), Cost of Sales, COGS, Gross Profit (formula), Selling/Admin Expenses, Total Expenses, NET Income ×2, Tax, ROI (formulas), Comments |
| Financial Accounting (5095227844) | Name, Due date, Manager, Invoice Number, Total Payment, Credit Limit, Remaining Balance, Payment 1+2 (formula), Payment Status 1+2, Payment Date (date) ×2, Comments, Files |
| 👤 Contacts CFO (5095228287) | Name, AR Invoices (board_relation), Contact Type, AP Invoices (board_relation), Company, Contact Person, Email, Phone, Address |
| Quotes & Invoices (5021195792) | Name, Type (status), Amount, Recipient (board_relation), File, Status, Issue date, Owner (creation_log) |

| 🚩 כפילויות | פירוט |
|---|---|
| Financial Accounting | "Payment Date" פעמיים באותו לוח — אומת בהמשך: שתיהן עם דאטה שונה (תאריך תשלום 1 ו-2) — לא למחיקה, מומלץ שינוי שם |
| Financial Accounting מול AP | אותו ייעוד — תשלומי ספקים בשני לוחות |
| Quotes & Invoices מול AR | חשבוניות יוצאות בשני לוחות (אחד ריק) |
| Contacts CFO מול Contacts CRM | שני מאגרי אנשי קשר מנותקים |

## 🏗️ תכנון ובנייה
| לוח | עמודות |
|---|---|
| בדיקות מקדימות (5043629046) | Name, Initiator (people), Request Status, Request Date, Executive summary (long_text), Anticipated Outcomes (long_text), Recommendation (long_text), Approved Budget (numbers) |

🚩 אין כפילויות.

## 🚧 ביצוע
| לוח | עמודות |
|---|---|
| ניהול פרוייקטים (5043629033) | Name, Project Manager (people), Project Phase (status), Project Budget (numbers), Project Timeline (timeline), Project Request Board (board_relation), "link to תיק לקוח 🗂️🗂️" ×4, Subitems, link to מזכירות |
| Project Board (5043629035) | Name, Owner, Status, Duration, Planned Timeline (timeline), Dependent On (dependency), Expenses, Cost type (dropdown), Planned/Effort spent (numbers), Actual completion (date), Completion Status (formula) |
| תהליך פרוייקט (5043736686) | Name, Project manager, Overall progress (mirror), Status, 8 עמודות שלבי בנייה (Permit/Foundations/Drywall/Trench/Framing/Electrical/Roofing/QC — status), Start date, קישור ל-Project details ×2 |

| 🚩 כפילויות | פירוט |
|---|---|
| ניהול פרוייקטים | "link to תיק לקוח" משוכפל 4 פעמים |
| תהליך פרוייקט | קישור ל-Project details פעמיים |
| חפיפת ייעוד | 3 לוחות ניהול פרויקט חופפים ללא חיבור |

## 🔴 סיכום — 5 הכפילויות החמורות
| # | בעיה | לוח |
|---|---|---|
| 1 | "link to תיק לקוח" ×4 | ניהול פרוייקטים |
| 2 | Phone/Button/Auto number/Mirror — הכל כפול | מחיר למשתכן |
| 3 | 8 עמודות טקסט טכניות + Phone כפול | Leads |
| 4 | "Payment Date" ×2 | Financial Accounting |
| 5 | "Dup. of Deals value" + Title כפול | Contacts CRM |
