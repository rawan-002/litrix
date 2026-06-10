# Litrix — Migration to Google Cloud (Dammam, Saudi Arabia)

**الهدف:** نقل المشروع كاملاً من البنية الحالية (Vercel + Render Frankfurt + Neon Frankfurt) إلى Google Cloud Region `me-central2` (الدمام، السعودية) للتوافق مع PDPL والـ NDMO، وتحسين الـ Latency للمستخدمين السعوديين.

**التكلفة المتوقعة:** ~$30–40 شهرياً.

**المدة الكلية:** ~ساعتين شغل فعلي على عدة جلسات.

---

## Final Architecture

```
┌──────────────────────────────┐
│   Firebase Hosting           │  Angular Frontend (Free tier, Global CDN)
│   litrix-prod.web.app        │
└──────────────┬───────────────┘
               │ HTTPS
               ▼
┌──────────────────────────────┐
│   Cloud Run (me-central2)    │  Django REST API
│   litrix-api-xxxxx.run.app   │  Container, scale-to-zero
│   gunicorn 2 workers/4 thr   │  ~$10/شهر
└──────────────┬───────────────┘
               │ SSL + Private IP
               ▼
┌──────────────────────────────┐
│   Cloud SQL PostgreSQL 16    │  Managed Database
│   me-central2 (Dammam)       │  db-g1-small, 10GB SSD
│   Automated daily backups    │  Point-in-Time Recovery
└──────────────────────────────┘  ~$25/شهر
```

---

## Phase 0 — GCP Project Setup (15 min)

### 0.1 إنشاء Project جديد
1. روحي [console.cloud.google.com](https://console.cloud.google.com)
2. اضغطي على Project Selector (أعلى الصفحة) → **New Project**
3. **Project Name:** `litrix-production`
4. **Project ID:** سيتولّد تلقائياً، احفظيه (شي مثل `litrix-production-12345`)
5. اضغطي **Create**

### 0.2 ربط Billing
1. القائمة الجانبية → **Billing** → **Link a billing account**
2. لو ما عندك Billing Account: اضغطي **Create billing account** وعبّي البيانات
3. **مهم:** الـ GCP يعطيك **$300 free credits** لمدة 90 يوم لأي حساب جديد — هذا يكفي لتشغيل Litrix بدون أي تكلفة فعلية حوالي 7-8 أشهر.

### 0.3 تفعيل الـ APIs المطلوبة
في الـ Console، روحي **APIs & Services** → **Enable APIs**، وفعّلي هذي ٤:
- **Cloud SQL Admin API**
- **Cloud Run Admin API**
- **Cloud Build API**
- **Artifact Registry API**

أو من Terminal (لو ركّبتي gcloud CLI):

```bash
gcloud services enable \
  sqladmin.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com
```

---

## Phase 1 — Cloud SQL Setup (20 min)

### 1.1 إنشاء PostgreSQL Instance

1. القائمة الجانبية → **SQL** → **Create Instance** → **PostgreSQL**
2. عبّيها كذا:

| الحقل | القيمة |
|---|---|
| **Instance ID** | `litrix-db` |
| **Password (postgres user)** | اختاري كلمة قوية + احفظيها فوراً |
| **Database version** | PostgreSQL 16 |
| **Region** | `me-central2 (Dammam)` ← **مهم جداً** |
| **Zonal availability** | Single zone (لتقليل التكلفة) |
| **Machine type** | Shared core → **db-g1-small** (1.7 GB RAM) |
| **Storage** | SSD, 10 GB, Enable automatic storage increase |
| **Backups** | Automatic, Window: 02:00 AST, Retention: 7 days |
| **Connections** | Public IP (مؤقتاً للنقل، نغلقه بعدين) |

اضغطي **Create Instance** — ياخذ ~5-10 دقايق.

### 1.2 إنشاء الـ Database والـ User

بعد ما الـ Instance يصير Running:

1. اضغطي على الـ Instance → **Databases** → **Create database** → اسم: `litrix`
2. **Users** → **Add user account** → username: `litrix_app`, password قوية

### 1.3 السماح بـ IP اللاب توب (مؤقتاً)

عشان نقدر نرفع الـ data من اللاب توب:
1. **Connections** → **Networking** → **Authorized networks** → **Add network**
2. شغّلي على المتصفح [whatismyipaddress.com](https://whatismyipaddress.com) لمعرفة IP اللاب توب
3. أضيفيه كـ `YOUR.IP.HERE/32` → Save

---

## Phase 2 — Data Migration: Neon → Cloud SQL (30 min)

### 2.1 تصدير البيانات من Neon

من PowerShell في root الـ Litrix:

```powershell
# الـ Connection String من Neon موجود في .env
$NEON_URL = "postgres://neondb_owner:npg_...@ep-fragrant-violet-...eu-central-1.aws.neon.tech/neondb?sslmode=require"

# تصدير كامل: schema + data، format ضغط مرن
pg_dump --no-owner --no-acl --format=custom --verbose `
        --file=litrix_neon_backup.dump `
        "$NEON_URL"
```

**ليش `--no-owner --no-acl`؟** لأن أسماء الـ Roles بتختلف بين Neon و Cloud SQL، فنشيل قيود التملك عشان الـ restore ما يفشل.

### 2.2 رفع البيانات لـ Cloud SQL

```powershell
# Cloud SQL Public IP من الـ Console → Overview tab
$GCP_HOST = "X.X.X.X"   # ضعي الـ IP الفعلي
$GCP_USER = "litrix_app"
$GCP_DB   = "litrix"

# يطلب الباسوورد تلقائياً
pg_restore --no-owner --no-acl --verbose `
           --host=$GCP_HOST --port=5432 `
           --username=$GCP_USER --dbname=$GCP_DB `
           litrix_neon_backup.dump
```

### 2.3 التحقق من نجاح النقل

```powershell
psql --host=$GCP_HOST --username=$GCP_USER --dbname=$GCP_DB
```

داخل psql:
```sql
\dt                                          -- يجب تشوفين قائمة الجداول
SELECT COUNT(*) FROM "ResearchPaper";        -- نفس العدد اللي كان في Neon
SELECT COUNT(*) FROM "Users";
SELECT COUNT(*) FROM "Authors";
\q
```

---

## Phase 3 — Backend Deployment on Cloud Run (35 min)

### 3.1 إنشاء Artifact Registry Repo (لتخزين الـ Docker images)

```bash
gcloud artifacts repositories create litrix-images \
  --repository-format=docker \
  --location=me-central2 \
  --description="Litrix container images"
```

### 3.2 بناء الـ Container Image

من root الـ Litrix:

```bash
# Cloud Build يبني الـ image على الكلاود ويرفعه لـ Artifact Registry
cd backend
gcloud builds submit \
  --tag me-central2-docker.pkg.dev/PROJECT_ID/litrix-images/litrix-api:latest .
```

استبدلي `PROJECT_ID` بالـ Project ID اللي حصلتي عليه في Phase 0.

### 3.3 Deploy على Cloud Run

```bash
gcloud run deploy litrix-api \
  --image=me-central2-docker.pkg.dev/PROJECT_ID/litrix-images/litrix-api:latest \
  --region=me-central2 \
  --platform=managed \
  --allow-unauthenticated \
  --memory=1Gi \
  --cpu=1 \
  --min-instances=0 \
  --max-instances=10 \
  --timeout=120 \
  --concurrency=80 \
  --set-env-vars="DJANGO_DEBUG=false" \
  --set-env-vars="DJANGO_ALLOWED_HOSTS=litrix-api-xxxxx-uc.a.run.app" \
  --set-env-vars="CORS_ALLOWED_ORIGINS=https://litrix-prod.web.app" \
  --set-secrets="DJANGO_SECRET_KEY=django-secret:latest" \
  --set-secrets="DATABASE_URL=database-url:latest" \
  --add-cloudsql-instances=PROJECT_ID:me-central2:litrix-db
```

**ليش نستخدم Secret Manager؟** لأن `DATABASE_URL` و `DJANGO_SECRET_KEY` ما يلزم تكون في env vars عادية — تظهر في الـ Console وفي الـ Build Logs. الـ Secret Manager يخزّنها مشفّرة ويعطي Cloud Run access لها فقط أثناء التشغيل.

### 3.4 إنشاء الـ Secrets

```bash
# Django Secret Key
echo -n "$(python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())')" | \
  gcloud secrets create django-secret --data-file=-

# Database URL (للاتصال بـ Cloud SQL عبر Unix socket — أسرع وأأمن)
echo -n "postgres://litrix_app:PASSWORD@//cloudsql/PROJECT_ID:me-central2:litrix-db/litrix" | \
  gcloud secrets create database-url --data-file=-
```

### 3.5 اختبار الـ API

```bash
curl https://litrix-api-xxxxx-uc.a.run.app/api/stats/overview/
```

المفروض ترجع JSON.

---

## Phase 4 — Frontend Deployment على Firebase Hosting (15 min)

### 4.1 تثبيت Firebase CLI

```bash
npm install -g firebase-tools
firebase login
```

### 4.2 تحديث `environment.prod.ts`

افتحي `frontend/src/environments/environment.prod.ts` وغيّري:

```typescript
export const environment = {
  production: true,
  apiBaseUrl: 'https://litrix-api-xxxxx-uc.a.run.app/api',
};
```

### 4.3 إنشاء `.firebaserc`

```bash
cd frontend
firebase use --add  # اختاري الـ litrix-production project
```

### 4.4 Build + Deploy

```bash
npm run build
firebase deploy --only hosting
```

بيعطيك URL مثل: `https://litrix-production.web.app`

### 4.5 تحديث CORS في Cloud Run

```bash
gcloud run services update litrix-api \
  --region=me-central2 \
  --update-env-vars="CORS_ALLOWED_ORIGINS=https://litrix-production.web.app"
```

---

## Phase 5 — Lockdown & Custom Domain (20 min)

### 5.1 إغلاق Public IP للـ Database

بعد ما يشتغل كل شي عبر Cloud Run (اللي يتصل بالـ DB عبر Private Unix socket):

1. Cloud SQL → litrix-db → Connections → Networking
2. **Public IP:** Disable
3. **Private IP:** Enable (في نفس الـ VPC)

### 5.2 ربط الدومين

لو عندك `litrix.albahau.edu.sa`:

**للـ Frontend:**
```bash
firebase hosting:channel:deploy litrix.albahau.edu.sa
```
وتضيفين الـ DNS records اللي يعطيكي إياها Firebase.

**للـ Backend:**
```bash
gcloud run domain-mappings create \
  --service=litrix-api \
  --domain=api.litrix.albahau.edu.sa \
  --region=me-central2
```

---

## Phase 6 — التحقق النهائي

- [ ] Frontend يفتح من السعودية بـ latency < 50ms
- [ ] جميع الـ API calls تشتغل بدون CORS errors
- [ ] الـ DB في الدمام (تأكدي من الـ Console)
- [ ] الـ Backups مفعّلة على Cloud SQL
- [ ] الـ Secret Manager فيه `django-secret` و `database-url`
- [ ] الـ Neon القديمة موقوفة (أو نحذفها بعد أسبوع تأكد)

---

## Rollback Plan (لو شي راح غلط)

الجمال إن النقل يخلّي الـ Neon القديمة شغّالة بالكامل خلال الفترة. لو شي راح غلط:
1. غيّري `environment.prod.ts` يرجع لـ Render API القديم
2. أعيدي Build + Deploy Vercel
3. الـ Users ما يحسّون بشي

---

## التكلفة الفعلية الشهرية المتوقعة

| Service | السعر |
|---|---|
| Cloud SQL db-g1-small (24/7) | ~$25 |
| Cloud Run (scale-to-zero, ~50k requests/month) | ~$5-10 |
| Cloud Storage (5GB) | ~$0.10 |
| Egress (10GB/month for SA users) | ~$1.20 |
| Secret Manager (3 secrets) | ~$0.18 |
| **Total** | **~$31-36/شهر** |

لو فعّلتي $300 Free Credits، أول 7-8 أشهر **مجاناً تماماً**.
