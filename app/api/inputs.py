from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.auth import get_guest
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db import get_session
from app.models import DiscountComparison, Guest, ItemSource
from app.schemas import RequestModel
from app.services.ai import AiService, DraftAnalysis
from app.services.discounts import DiscountInput, DiscountDraft, calculate_discount
from app.services.cart_discounts import CartDiscountRequest, calculate_cart
from app.services.finance import lock_guest, request_hash
from app.services.sharing import fetch_product_html, extract_metadata

router = APIRouter(tags=["product URLs and discounts"])
DB = Annotated[Session, Depends(get_session)]
Owner = Annotated[Guest, Depends(get_guest)]
Config = Annotated[Settings, Depends(get_settings)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]


class ProductUrlInput(RequestModel):
    url: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=9, max_length=2048, pattern=r"^https://")
    ]


class DiscountDecision(RequestModel):
    decision: Literal["NEEDED", "REMOVE_EXTRA"]


class DiscountExtraction(BaseModel):
    conditions: DiscountDraft
    analysis_method: Literal["vision", "ocr", "manual"]
    warnings: list[str]


class CheckoutOutput(BaseModel):
    subtotal: int
    discount: int
    shipping: int
    total: int


class DiscountOutput(BaseModel):
    id: str
    inputs: DiscountInput
    original: CheckoutOutput
    expanded: CheckoutOutput
    extra_spending: int
    remaining_to_free_shipping: int | None
    pay_shipping_saves: int | None
    message: str
    decision: Literal["NEEDED", "REMOVE_EXTRA"] | None


def _product_url_draft(body: ProductUrlInput, settings: Settings, source: ItemSource) -> DraftAnalysis:
    html, final_url = fetch_product_html(body.url)
    metadata = extract_metadata(html, final_url)
    draft = AiService(settings).draft_item(metadata["name"], metadata["price"])
    draft.items[0].source = source
    draft.items[0].source_url, draft.items[0].image_url = metadata["source_url"], metadata["image_url"]
    draft.warnings.append(
        "CONFIRM_SHARED_PRODUCT" if source == ItemSource.SHARE else "CONFIRM_PRODUCT_INFORMATION"
    )
    if metadata["name_missing"]:
        draft.warnings.append("NAME_REQUIRED")
    if metadata["price"] is None:
        draft.warnings.append("PRICE_REQUIRED")
    if metadata["image_url"] is None:
        draft.warnings.append("PRODUCT_IMAGE_UNAVAILABLE")
    return draft


@router.post(
    "/product-url-drafts",
    response_model=DraftAnalysis,
    summary="붙여넣은 상품 URL에서 확인용 품목 초안 만들기",
)
def product_url_draft(body: ProductUrlInput, guest: Owner, settings: Config):
    return _product_url_draft(body, settings, ItemSource.URL)


@router.post("/share-drafts", response_model=DraftAnalysis, deprecated=True)
def share_draft(body: ProductUrlInput, guest: Owner, settings: Config):
    """Compatibility for old clients; new registration uses product-url-drafts."""
    try:
        return _product_url_draft(body, settings, ItemSource.SHARE)
    except AppError as exc:
        legacy_code = {
            "UNSAFE_PRODUCT_URL": "UNSAFE_SHARE_URL",
            "PRODUCT_URL_UNAVAILABLE": "SHARE_UNAVAILABLE",
              "PRODUCT_URL_BLOCKED": "SHARE_UNAVAILABLE",
        }.get(exc.code, exc.code)
        raise AppError(exc.status_code, legacy_code, exc.message) from None


@router.post("/discount-extractions", deprecated=True)
def discount_extraction(guest: Owner):
    raise AppError(410, "DISCOUNT_EXTRACTION_REMOVED", "할인 조건을 입력하고 품목 스위치로 비교해 주세요.")


@router.post("/discount-previews")
def discount_preview(body: CartDiscountRequest, guest: Owner):
    return calculate_cart(body)


def comparison_output(value: DiscountComparison) -> dict:
    return {"id": value.id, "inputs": value.inputs, **value.result, "decision": value.decision}


@router.post("/discount-comparisons", status_code=201, response_model=DiscountOutput, deprecated=True)
def discount_comparison(body: DiscountInput, guest: Owner, session: DB, idempotency_key: Key) -> dict:
    lock_guest(session, guest.id)
    fingerprint = request_hash(body.model_dump())
    existing = session.scalar(
        select(DiscountComparison).where(
            DiscountComparison.guest_id == guest.id, DiscountComparison.idempotency_key == idempotency_key
        )
    )
    if existing:
        if fingerprint != existing.request_hash:
            raise AppError(409, "IDEMPOTENCY_CONFLICT", "같은 키로 다른 비교를 저장할 수 없습니다.")
        return comparison_output(existing)
    comparison = DiscountComparison(
        guest_id=guest.id,
        idempotency_key=idempotency_key,
        request_hash=fingerprint,
        inputs=body.model_dump(),
        result=calculate_discount(body),
    )
    session.add(comparison)
    session.commit()
    return comparison_output(comparison)


def owned_comparison(session: Session, guest_id: str, comparison_id: str) -> DiscountComparison:
    comparison = session.scalar(
        select(DiscountComparison).where(
            DiscountComparison.guest_id == guest_id, DiscountComparison.id == comparison_id
        )
    )
    if comparison is None:
        raise AppError(404, "NOT_FOUND", "비교 기록을 찾을 수 없습니다.")
    return comparison


@router.get("/discount-comparisons/{comparison_id}", response_model=DiscountOutput)
def get_comparison(comparison_id: str, guest: Owner, session: DB) -> dict:
    return comparison_output(owned_comparison(session, guest.id, comparison_id))


@router.put("/discount-comparisons/{comparison_id}/decision", response_model=DiscountOutput)
def decide_comparison(comparison_id: str, body: DiscountDecision, guest: Owner, session: DB) -> dict:
    lock_guest(session, guest.id)
    comparison = owned_comparison(session, guest.id, comparison_id)
    comparison.decision = body.decision
    session.commit()
    return comparison_output(comparison)
