# تصمیم‌یار — داشبورد مدیریتی هوشمند (مرحله ۱)

Monorepo با `/apps/web` (Next.js 14 + Tailwind + shadcn/ui) و `/apps/api` (FastAPI + SQLAlchemy + PostgreSQL).

## ویژگی‌های مرحله ۱
- چندمستاجری با `organization_id` و PostgreSQL Row-Level Security روی `memberships`
- احراز هویت JWT، ثبت‌نام/ورود، ساخت سازمان یا دعوت
- نقش‌ها: Owner, Admin, Manager, Analyst, Viewer (سلسله‌مراتب)
- Middleware استخراج `tenant_id` از JWT / هدر `X-Organization-Id`
- صفحات فارسی RTL با تم آبی تیره (`#0f2a44`)
- Settings برای مدیریت اعضا (دعوت، تغییر نقش، حذف)

## اجرا

```bash
docker compose up --build
# web: http://localhost:3000
# api: http://localhost:8000  (docs: /docs)
```

متغیرها در `docker-compose.yml` تنظیم شده‌اند. برای اجرا بدون docker:
- `apps/api`: `pip install -r requirements.txt && uvicorn app.main:app --reload`
- `apps/web`: `npm install && npm run dev`

## مرحله بعد
داشبورد KPI و دستیار هوش مصنوعی.
