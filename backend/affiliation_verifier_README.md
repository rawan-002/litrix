# Litrix Affiliation Verifier

أداة ذكية تتحقق آلياً من إن كل بحث في الـ DB كُتب فعلاً تحت **affiliation جامعة الباحة**، باستخدام 3 طبقات APIs + استخراج PDF.

## ليش هذي الأداة موجودة؟

الـ scraper من Google Scholar يسحب **كل** أبحاث الباحث من ملفه الشخصي، بما فيها:
- أبحاث الـ PhD/الـ Postdoc من جامعات سابقة
- co-author papers من مؤسسات أخرى
- أبحاث الـ sabbatical / الزيارات العلمية

النتيجة: أبحاث تنحسب كـ "إنتاج بحثي للباحة" وهي مو كذا. للـ NCAAA reporting لازم نفصل.

## التشغيل — 3 خطوات

### الخطوة 1: تطبيق الـ Schema Migration

افتحي Neon SQL Editor وشغّلي محتوى:
```
backend/migrations/20260605_affiliation_verification.sql
```

هذا يضيف 4 أعمدة جديدة لـ ResearchPaper:
- `AffiliationVerified` (BOOLEAN) — `TRUE`/`FALSE`/`NULL`
- `VerificationSource` (VARCHAR(20)) — أي طبقة تحققت
- `VerifiedAt` (TIMESTAMP) — متى صار التحقق
- `VerificationDetails` (JSONB) — تفاصيل الإثبات

الـ migration **يعلّم تلقائياً** أبحاث Scopus + OpenAlex كـ verified (لأنها 100% Al-Baha حسب التشخيص).

### الخطوة 2: تثبيت المكتبات (مرة واحدة)

```powershell
pip install requests psycopg2-binary pypdf
```

### الخطوة 3: تشغيل الأداة

**أولاً، تقرير الحالة (بدون أي API calls):**
```powershell
python affiliation_verifier.py --report
```

**ثانياً، تجربة Dry Run على 10 أبحاث فقط:**
```powershell
python affiliation_verifier.py --dry-run --limit 10
```

**ثالثاً، التشغيل الفعلي على كل Scholar papers:**
```powershell
python affiliation_verifier.py --apply --source Scholar
```

**رابعاً، أي بحث ما تحقق (NULL) نعيد المحاولة عليه:**
```powershell
python affiliation_verifier.py --apply
```

## كل الـ CLI options

| Argument | الوصف |
|----------|-------|
| `--dry-run` | تجربة بدون كتابة على الـ DB |
| `--apply` | التشغيل الفعلي |
| `--report` | تقرير الحالة فقط (بدون APIs) |
| `--source X` | محدود لمصدر معين (Scholar/Scopus/OpenAlex/Manual) |
| `--tier X` | طبقة واحدة فقط (openalex/crossref/pdf) |
| `--limit N` | أول N بحث فقط (للتجربة) |
| `--no-resume` | يعيد التحقق حتى لو سابقاً تحقق |
| `--re-verify` | يعيد التحقق من كل شي حتى المتأكد |

## مثال لمخرجات الأداة

```
14:23:45 [INFO] Connecting to database...
14:23:46 [INFO] Tier(s) to run: ['openalex', 'crossref', 'pdf']
14:23:47 [INFO] Fetched 166 pending papers
14:23:47 [INFO] [1/166] Paper #4521 (DOI=10.1109/ACCESS.2024.3398765)
14:23:48 [INFO]   ✓ VERIFIED Al-Baha via openalex
14:23:48 [INFO] [2/166] Paper #4522 (DOI=10.1007/s00500-023-...)
14:23:49 [INFO]   ✗ NOT Al-Baha via openalex
14:23:49 [INFO] [3/166] Paper #4523 (DOI=10.5555/...)
14:23:50 [WARNING]   ? PENDING (openalex_not_found)
14:23:51 [INFO]   ✓ VERIFIED Al-Baha via crossref
...

═══════════════════════════════════════════════════════════
                          RUN SUMMARY                          
═══════════════════════════════════════════════════════════
  Processed:        166
  Al-Baha verified: 124
  NOT Al-Baha:      31
  Pending (retry):  11
  Skipped (no DOI): 0
  Mode:             APPLY (writes to DB)
```

## ربط الأداة مع الـ Dashboard

بعد تشغيل الأداة بنجاح، لازم نضيف فلتر في `backend/analytics/public_views.py`:

```python
AFFILIATION_VERIFIED_SQL = '''
    (rp."AffiliationVerified" = TRUE OR rp."AffiliationVerified" IS NULL)
'''
```

ونضيفه على نفس الـ queries اللي فيها `NOT_RETRACTED_SQL`. هذا يستبعد بس الأبحاث المؤكد إنها **NOT Al-Baha**، ويترك الـ pending مرئية لحد ما نتحقق منها.

## نسبة النجاح المتوقعة

من تجربة جامعات مشابهة:
- **Tier 1 (OpenAlex):** يحل ~75% من Scholar contamination
- **Tier 2 (Crossref):** يحل إضافي ~10%
- **Tier 3 (PDF):** يحل إضافي ~5-10%
- **يبقى pending:** ~5-10% (يحتاج عين بشرية)

## التوصيات للمستقبل

1. **Cron job أسبوعي:** يشغّل `--apply` على الأبحاث الجديدة تلقائياً
2. **Admin UI:** صفحة في Django Admin تعرض الـ pending وتسمح بالـ manual flag
3. **Cross-reference بـ ROR:** نضيف ROR lookup في الـ scraper نفسه عشان نمنع التلوّث من البداية

## تحسينات الدقة (2026-06-09)

- **ما يُستبعد بحث إلا بدليل فعلي:** لو OpenAlex/Crossref ما عندهم بيانات affiliation للبحث أصلاً، النتيجة تصير **inconclusive (pending)** بدل "ليس بالباحة" — يمنع الاستبعاد الخاطئ. البحث يُعلّم `FALSE` فقط لو طبقة وحدة على الأقل فحصت affiliations حقيقية وما لقت الباحة.
- **بحث الصفحة الكامل (Tier HTML fallback) يتجاهل قوائم المراجع والشكر** — قبل، بحث يستشهد ببحث من الباحة كان ممكن يُعلّم خطأً "بالباحة".
- **القرار النهائي يتتبّع أي طبقة قالت "لا" بشكل قاطع** (مو آخر طبقة فقط).
- **`--report` يستخدم نفس نطاق السنوات الديناميكي** (السنة الماضية → القادمة، أو `--years`)، ويعدّ الأبحاث بشكل صحيح (`DISTINCT` — كان يضاعف العدد بسبب الـ JOIN على المؤلفين).
- **تغطية أوسع للناشرين:** SAGE, Emerald, IOP, RSC, ACS, De Gruyter, Karger, AIP/Atypon + selectors عامة `[class*="affiliation"]`. والطبقات العامة (meta tags بمعيار Google Scholar، JSON-LD) تغطي أي مجلة غير معروفة.

> **فجوة معروفة:** الأبحاث **بدون DOI** تُتخطّى (الطبقات كلها تعتمد على DOI). لتغطية شاملة 100% تحتاج مطابقة بالعنوان — مؤجّلة لأن المطابقة الضبابية بالعنوان قد تتحقق من بحث خاطئ (راجع قواعد سلامة البيانات في CLAUDE.md).

## ملاحظات أمان وأداء

- الأداة **idempotent**: ممكن تعيدي تشغيلها بأمان
- الـ rate limiting محسوب: ~10 req/sec للـ OpenAlex، ~20 للـ Crossref
- كل update داخل transaction — لو فشلت في الوسط، الـ DB يبقى متناسق
- الـ failures الشبكية ما تعلّم البحث "not Al-Baha" — تتركه NULL للـ retry
