"""راستر API کاتالوگ زمینه — فاز ۲.۴ گام ۲ (docs/PHASE4_CONTEXT_CATALOG_PLAN.md §9)

تک اندپوینت طرح — دقیقاً یکی:
  GET /context/catalog → **analyst+** (["owner","admin","manager","analyst"])؛ viewer → 403

مرزهای امنیتی (طبق طرح §7/§9):
  - organization_id هرگز از کلاینت پذیرفته نمی‌شود — از membership تأییدشده می‌آید
    (require_role → require_membership → set_rls_context)؛ کوئری‌های builder تحت
    RLS context درخواست اجرا می‌شوند و همه org-scoped هستند (لایه ۳ دفاع کنار RLS)
  - هیچ path parameter و هیچ query parameter وجود ندارد — filtering کار لایه AI
    آینده است (در-پردازش روی ساختار bounded)؛ سمانتیک 404 ندارد
  - هیچ credential/secret در پاسخ نیست — builder از اتصالات فقط provenance
    database_connection_id را می‌خواند (تست non-disclosure در گام ۲)
  - org خالی → 200 با کاتالوگ معتبر خالی («هنوز داده‌ای نیست» پاسخ درست است —
    همان قرارداد empty-result فاز ۲.۳ §3.3)
  - هیچ منطق catalogی در این راستر نیست — فقط فراخوانی builder خالص گام ۱
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.middleware.tenant import require_role
from app.schemas.context_catalog import ContextCatalogOut
from app.services.context_catalog import build_context_catalog

router = APIRouter(prefix="/context", tags=["context"])

ANALYST_ROLES = ["owner", "admin", "manager", "analyst"]


@router.get("/catalog", response_model=ContextCatalogOut)
def get_context_catalog(
    membership=Depends(require_role(ANALYST_ROLES)),
    db: Session = Depends(get_db),
):
    """کاتالوگ زمینه کامل سازمان (§4) — فقط-خواندنی، deterministic، bounded.

    - viewer → 403 (require_role) · unauthenticated → 401 (HTTPBearer)
    - خروجی مستقیماً از builder خالص گام ۱ است — هیچ منطق اضافه‌ای اینجا نیست
    - RLS context پیش از کوئری‌ها توسط require_membership ست شده است
    """
    return build_context_catalog(db, organization_id=membership.organization_id)
