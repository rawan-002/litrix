# Litrix — Deployment Guide

**Stack:** Angular 19 (Frontend) + Django REST (Backend) + PostgreSQL (Database)

**التوصية:** Vercel + Render + Neon — كلهم فيهم free tier ويشتغلون مع بعض بسلاسة.

---

## الخريطة الكاملة (Architecture)

```
┌──────────────┐       HTTPS       ┌──────────────┐       SSL       ┌──────────────┐
│   Vercel     │ ───────────────►  │   Render     │ ──────────────► │    Neon      │
│  (Angular)   │   /api/* calls    │  (Django)    │   psql + SSL    │ (Postgres)   │
│  static CDN  │                   │  gunicorn    │                 │  managed DB  │
└──────────────┘                   └──────────────┘                 └──────────────┘
   ng build → dist/                  build.sh → migrate                snapshot from local
   نشر تلقائي من GitHub               نشر تلقائي من GitHub                 pg_dump / pg_restore
```

**ليش هذي التركيبة بالذات:**
- **Vercel** يخدم static files من CDN قريب من المستخدم → تحميل سريع جدًا للـ Angular
- **Render** يشغّل Django كـ background service مع HTTPS مجاني وdeploy تلقائي
- **Neon** هو Postgres-as-a-service بـ free tier 500MB ـ كافي للـ Litrix بكل الـ 2,800+ paper

---

## Phase 0 — متطلبات قبل ما نبدأ

1. **GitHub account** — كل شي يعتمد على push للـ repo
2. **Git installed** — لو ما عندك: `winget install Git.Git`
3. **حساب Vercel** — vercel.com (سجلي بـ GitHub login)
4. **حساب Render** — render.com (سجلي بـ GitHub login)
5. **حساب Neon** — neon.tech (سجلي بـ GitHub login)
6. **PostgreSQL client tools** — `psql` و `pg_dump` (موجودين مع PostgreSQL installation)

---

## Phase 1 — تجهيز الـ Repo

### 1.1 سوي `.gitignore` في root الـ Litrix folder

```gitignore
# Python
__pycache__/
*.pyc
.venv/
venv/

# Django
*.log
db.sqlite3
staticfiles/
media/

# Environment files (المهم!)
.env
.env.*
!.env.example

# Node
node_modules/
.angular/
dist/

# IDE
.vscode/
.idea/
*.swp

# OS
.DS_Store
Thumbs.db

# Litrix specific (السكرابر outputs)
*.csv.bak
scraper_logs/
```

### 1.2 سوي `.env.example` (بدون passwords)

في root الـ Litrix:

```env
# Database
DB_NAME=LitrixDB
DB_USER=postgres
DB_PASSWORD=your-password-here
DB_HOST=localhost
DB_PORT=5432

# Django
DJANGO_SECRET_KEY=generate-a-50-char-random-string
DJANGO_DEBUG=true
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1

# Production only
# DATABASE_URL=postgres://user:pass@host:5432/db?sslmode=require
# CORS_ALLOWED_ORIGINS=https://litrix.vercel.app
```

### 1.3 Push للـ GitHub

```bash
cd "C:\Users\...\Litrix"
git init
git add .
git commit -m "Initial commit: Litrix dashboard"
git branch -M main
# سوي repo جديد على github.com اسمه litrix
git remote add origin https://github.com/YOUR_USERNAME/litrix.git
git push -u origin main
```

**تحذير مهم:** قبل الـ push، تأكدي إن `.env` الحقيقي **مو** موجود في الـ commit. شغلي:
```bash
git status
# ما يجب يطلع .env بالقائمة
```

---

## Phase 2 — Database على Neon

### 2.1 إنشاء قاعدة البيانات

1. روحي [neon.tech](https://neon.tech) → New Project
2. اختاري المنطقة الأقرب (مثلاً Frankfurt للسرعة من السعودية)
3. اسم المشروع: `litrix-db`
4. Postgres version: 16 (أحدث stable)
5. اضغطي Create Project

Neon راح يعطيك **Connection string** بهذا الشكل:
```
postgres://litrix_owner:abc123@ep-cool-name-12345.eu-central-1.aws.neon.tech/litrix?sslmode=require
```
احفظيه في مكان آمن — هذا الي بيكون `DATABASE_URL`.

### 2.2 رفع البيانات من اللاب توب للسحابة

من PowerShell في root الـ Litrix:

```bash
# 1. صدّري الـ schema + data من LitrixDB المحلية
pg_dump -h localhost -U postgres -d LitrixDB -F c -b -v -f litrix_backup.dump

# 2. ارفعيها على Neon (استبدلي الـ URL باللي عطاكي إياه Neon)
pg_restore --no-owner --no-acl -d "postgres://litrix_owner:abc123@ep-...neon.tech/litrix?sslmode=require" -v litrix_backup.dump
```

**ليش `--no-owner --no-acl`؟** لأن المستخدم في Neon اسمه مختلف عن `postgres` المحلي، وهالـ flags تتجاهل أوامر التملّك.

### 2.3 تأكيد الـ migration

```bash
psql "postgres://litrix_owner:...@ep-...neon.tech/litrix?sslmode=require"

# داخل psql:
\dt                                          -- قائمة الجداول
SELECT COUNT(*) FROM "ResearchPaper";        -- يجب يطلع 2800+
SELECT COUNT(*) FROM "Users";                -- يجب يطلع 100+
\q
```

---

## Phase 3 — Backend على Render

### 3.1 إنشاء الـ Web Service

1. روحي [render.com](https://render.com) → Dashboard → **New** → **Web Service**
2. **Connect repository:** اختاري repo الـ litrix من GitHub
3. عبّيها كذا:

| الحقل | القيمة |
|------|--------|
| **Name** | `litrix-api` |
| **Region** | Frankfurt (الأقرب لـ Neon) |
| **Branch** | `main` |
| **Root Directory** | `backend` |
| **Runtime** | Python 3 |
| **Build Command** | `./build.sh` |
| **Start Command** | `gunicorn litrix_backend.wsgi:application` |
| **Plan** | Free |

### 3.2 Environment Variables

اضغطي **Advanced** → **Add Environment Variable** وضيفي كذا:

| Key | Value |
|-----|-------|
| `DATABASE_URL` | الـ connection string من Neon |
| `DJANGO_SECRET_KEY` | string عشوائي 50 حرف (يمكن تولّديه من [djecrety.ir](https://djecrety.ir/)) |
| `DJANGO_DEBUG` | `false` |
| `DJANGO_ALLOWED_HOSTS` | `litrix-api.onrender.com` (هذا الدومين الي بيعطيكي إياه Render) |
| `CORS_ALLOWED_ORIGINS` | (نتركه فاضي مؤقتًا، نرجع لها بعد ما ننشر Vercel) |
| `PYTHON_VERSION` | `3.12.3` |

### 3.3 Deploy

اضغطي **Create Web Service** → بيبدأ يبني تلقائيًا. متابعة الـ logs:
- لو طلع `Build successful` و `Listening on port ...` → نجحنا
- لو طلع errors، اقرأي الـ logs بعناية (غالبًا env var ناقص)

### 3.4 اختبار الـ API

```bash
curl https://litrix-api.onrender.com/api/stats/overview/
```

المفروض ترجع JSON.

---

## Phase 4 — Frontend على Vercel

### 4.1 حدّثي `environment.prod.ts`

افتحي `frontend/src/environments/environment.prod.ts` وعدّلي:

```typescript
export const environment = {
  production: true,
  apiBaseUrl: 'https://litrix-api.onrender.com/api',
};
```

ثم commit + push:
```bash
git add frontend/src/environments/environment.prod.ts
git commit -m "chore: point production frontend at Render API"
git push
```

### 4.2 إنشاء Vercel Project

1. روحي [vercel.com/new](https://vercel.com/new)
2. **Import Git Repository:** litrix
3. **Configure:**

| الحقل | القيمة |
|------|--------|
| **Framework Preset** | Other |
| **Root Directory** | `frontend` |
| **Build Command** | `npm run build` |
| **Output Directory** | `dist/frontend/browser` |
| **Install Command** | `npm install` |

4. اضغطي **Deploy**.

Vercel بيعطيكي domain مثل: `https://litrix.vercel.app`

### 4.3 رجعي لـ Render وحدّثي CORS

روحي service الـ litrix-api في Render → Environment → عدّلي:
```
CORS_ALLOWED_ORIGINS=https://litrix.vercel.app
```

اضغطي **Save** → Render بيعمل redeploy تلقائيًا.

---

## Phase 5 — اختبار شامل

افتحي `https://litrix.vercel.app` في المتصفح. لازم:
- [ ] Overview يطلع بأرقامه (Total Papers, Researchers, etc.)
- [ ] Year toggle يشتغل (All / 2025 / 2026)
- [ ] Yearly Department Breakdown يفتح ويقفل
- [ ] **Load More** button يطلع لو فيه أكثر من 10 بحث
- [ ] Export Excel يحمّل ملف
- [ ] الـ console بدون CORS errors

---

## Phase 6 — Custom Domain (اختياري)

لو عندك دومين (مثل `litrix.albahau.edu.sa`):

**Vercel:**
- Project Settings → Domains → Add `litrix.albahau.edu.sa`
- Vercel بيعطيكي DNS records تضيفيها عند registrar

**Render:**
- Service Settings → Custom Domains → Add `api.litrix.albahau.edu.sa`
- بعدها أضيفي الدومين الجديد لـ `DJANGO_ALLOWED_HOSTS` و `CORS_ALLOWED_ORIGINS`

---

## نصايح مهمة

### Free tier limitations
- **Render Free:** السيرفر **ينام** بعد 15 دقيقة بدون نشاط. أول request بعد النوم تاخذ 30-60 ثانية. الحل: ارقي لـ Starter ($7/شهر) أو استخدمي [UptimeRobot](https://uptimerobot.com) عشان يضربه ping كل 14 دقيقة.
- **Neon Free:** auto-suspend بعد 5 دقائق inactivity (يرجع تلقائيًا). 500MB storage.
- **Vercel Free:** 100GB bandwidth/شهر — أكثر من كافي.

### Re-deploy السكرابر
السكرابر `litrix_scraper.py` بيظل يشتغل من اللاب توب عندك (مو على الكلاود). كل ما يضيف بيانات جديدة، تحدّث Neon مباشرة لو عدّلتي connection string في `.env`:

```env
# في .env المحلي (للسكرابر)
DB_HOST=ep-cool-name-12345.eu-central-1.aws.neon.tech
DB_PORT=5432
DB_NAME=litrix
DB_USER=litrix_owner
DB_PASSWORD=abc123
```

كذا لما تشغّلي السكرابر محليًا، البيانات تروح مباشرة لـ Neon والداش بورد يشوفها على طول.

### Monitoring
- **Render:** Logs → real-time
- **Neon:** Monitoring → query performance
- **Vercel:** Analytics tab → page views

---

## Troubleshooting سريع

| المشكلة | الحل |
|--------|-----|
| `CORS error` في console | تأكدي إن `CORS_ALLOWED_ORIGINS` في Render فيها domain Vercel بدون trailing slash |
| `502 Bad Gateway` | السيرفر نايم — انتظري 30 ثانية أو ضربيه ping |
| `relation "ResearchPaper" does not exist` | الـ pg_restore فشل — جربي تعيدينه |
| `Module not found: environments/environment` | تأكدي إن `fileReplacements` متظبط في angular.json |
| `Mixed content blocked` | تأكدي إن environment.prod.ts فيه `https://` مو `http://` |

---

**خلاصة:** بعد ما تخلصين هالخطوات، يكون عندك:
- Frontend سريع على CDN عالمي
- API يشتغل 24/7 (مع free tier sleep)
- Database managed مع backups تلقائية

لو احتجتي تطوّري المشروع لـ paid tier (لما يصير production فعلي للجامعة)، التركيبة كلها تترقّى بدون تغيير كود — بس Render Plan + Neon Pro.
