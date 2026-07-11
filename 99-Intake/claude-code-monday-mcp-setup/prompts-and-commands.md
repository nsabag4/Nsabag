# כל הפרומפטים, ההוראות והפקודות — להעתקה חוזרת

> ריכוז מילולי של כל מה שנוסח בשיחה כפרומפט לבוט/סוכן או כפקודת טרמינל.
> `[REDACTED-TOKEN]` = טוקן ה־API של מאנדיי שנחשף בצילומים ואינו נשמר בריפו.
> **חובה להנפיק טוקן חדש לפני שימוש חוזר** (monday ← Developer Center ← API token ← Regenerate).

---

## 1. פקודות התקנה והגדרה (PowerShell)

### התקנת Claude Code על Windows

```powershell
irm https://claude.ai/install.ps1 | iex
```

### הוספת תיקיית ההתקנה ל־PATH של המשתמש

```powershell
[Environment]::SetEnvironmentVariable("Path", [Environment]::GetEnvironmentVariable("Path", "User") + ";$env:USERPROFILE\.local\bin", "User")
```

(לסגור ולפתוח מחדש את הטרמינל אחרי ההרצה.)

### הרצה ישירה בלי PATH (עוקף, עד לפתיחת טרמינל חדש)

```powershell
& "$env:USERPROFILE\.local\bin\claude.exe"
```

### הוספת שרת MCP מקומי של מאנדיי (הניסיון שנכשל בסוף — נשמר לתיעוד)

```powershell
claude mcp add monday -- npx -y @mondaydotcomorg/monday-api-mcp -t [REDACTED-TOKEN]
```

### מחיקת שרת MCP רשום

```powershell
claude mcp remove monday
```

### רשימת שרתי MCP ובדיקת חיבור

```powershell
claude mcp list
```

### חלופה שהוזכרה ולא נוסתה — חיבור לשרת המתארח של מאנדיי (HTTP)

```powershell
claude mcp add --transport http monday https://mcp.monday.com/mcp
```

### הפעלת Claude Code בתיקיית עבודה

```powershell
cd $env:USERPROFILE\Documents
claude
```

## 2. פקודות בתוך Claude Code (slash commands)

| פקודה | מטרה |
|---|---|
| `/theme` | שינוי ערכת נושא |
| `/mcp` | בדיקת שרתי MCP מחוברים בסשן |
| `/init` | יצירת קובץ CLAUDE.md לפרויקט (הוראות קבועות) |

## 3. פרומפטים לבוטים/סוכנים — מילה במילה

### פרומפט בדיקה ראשוני (לוודא שחיבור מאנדיי עובד)

```
הצג לי את רשימת הלוחות שלי במאנדיי
```

### תבנית הפרומפט למשימה המשולבת (חילוץ ← הטמעה) — פרומפט חד־פעמי

```
חלץ את הקובץ X, וכשתסיים — קח את הנתונים שחולצו והטמע אותם בלוח Y במאנדיי (צור אייטמים לפי העמודות...).
```

גרסה מפורטת יותר שנוסחה בהמשך:

```
חלץ את הנתונים מהקובץ C:\...\הקובץ.xlsx, וכשתסיים — צור מכל שורה אייטם בלוח [שם הלוח] במאנדיי, ומפה את העמודות לפי...
```

### הוראה קבועה לקובץ CLAUDE.md (תהליך חוזר) — מילה במילה

```
בכל פעם שאתה מסיים לחלץ נתונים מקובץ, הטמע אותם מיד בלוח [שם הלוח] במאנדיי:
- כל שורה הופכת לאייטם חדש
- מפה את השדות כך: ...
```

ניסוח נוסף שהוצע לאותה מטרה:

```
בכל פעם שאתה מסיים לחלץ קובץ, הטמע את הנתונים בלוח X במאנדיי
```

### הפרומפטים שהמשתמש נתן ל־Claude בסשן הזה — מילה במילה

```
איך ניתן לתת לו הנחיה שמתי שהוא מסיים לחלץ את הקובץ הוא מתחיל להטמיע אותו במערכת מאנדיי ?
```

```
API Token במאנדיי עצמו ?
```

```
מה להריץ את הקוד של מאנדיי או מה ששלחת כאן בקישור ?
```

```
מהתחלה תן לי צעד צעד מה לעשות מבלי לדלג על פקודות
```

```
העלה את כל התוכן של השיחה הזו לריפו nsabag4/Nsabag בגיטהאב, לפי הכללים:

1. צור ענף חדש משלך (שם שמתחיל ב-claude/). אל תיגע בענפים אחרים ואל תמזג ל-main.
2. כתוב הכול לתיקייה 99-Intake/<שם-הנושא-באנגלית>/ בלבד. אסור לגעת בשום תיקייה אחרת.
3. מה לכלול: מטרת הפרויקט, מה הוקם בפועל, מה תוכנן וטרם הוקם,
   כל הפרומפטים וההוראות לבוטים ולסוכנים מילה במילה (לא סיכום!),
   החלטות שהתקבלו, ושאלות פתוחות.
4. אל תסנן ואל תקצר — גולמי ומלא עדיף על מסודר וחסר.
5. בסוף: דחוף (push) לגיטהאב וכתוב לי את שם הענף שיצרת.
```

## 4. מצב סופי של שרתי ה־MCP (מתוך `claude mcp list`)

```
claude.ai Microsoft 365: https://microsoft365.mcp.claude.com/mcp - √ Connected
claude.ai Google Drive: https://drivemcp.googleapis.com/mcp/v1 - √ Connected
claude.ai monday.com: https://mcp.monday.com/mcp - √ Connected
claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - √ Connected
claude.ai Google Calendar: https://calendarmcp.googleapis.com/mcp/v1 - √ Connected
monday: npx -y @mondaydotcomorg/monday-api-mcp -t [REDACTED-TOKEN] - ✗ Failed to connect
```

**מסקנה:** משתמשים בחיבור המתארח `claude.ai monday.com` (עובד); את המקומי (`monday`)
מוחקים עם `claude mcp remove monday`.
