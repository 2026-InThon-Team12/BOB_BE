from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.core.auth import get_guest
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.services.ai import AiService, DraftAnalysis, ImageInputError, PositivePrice


router = APIRouter(tags=["AI drafts"], dependencies=[Depends(get_guest)])


class ItemDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    price: PositivePrice | None = None


@router.post("/item-drafts", response_model=DraftAnalysis)
def item_draft(body: ItemDraftRequest, settings: Annotated[Settings, Depends(get_settings)]):
    return AiService(settings).draft_item(body.name, body.price)


@router.post("/cart-extractions", response_model=DraftAnalysis)
def cart_extraction(
    settings: Annotated[Settings, Depends(get_settings)],
    image: UploadFile = File(..., description="Single JPEG, PNG, or WebP cart screenshot"),
):
    try:
        data = image.file.read(settings.max_image_bytes + 1)
        return AiService(settings).extract_cart(data)
    except ImageInputError as exc:
        raise AppError(
            exc.status_code, exc.code, "지원되는 크기의 JPEG, PNG 또는 WebP 이미지를 올려 주세요."
        ) from None
    finally:
        image.file.close()
