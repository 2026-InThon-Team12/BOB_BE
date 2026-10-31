import json

import pytest
from sqlalchemy import func, select

from app.api import inputs
from app.core.errors import AppError
from app.models import Item
from app.services import sharing
from tests.test_design_inputs import dns

P = "/api/v1"


def test_pasted_url_creates_draft_then_confirmed_registration(client, guest_headers, session, monkeypatch):
    dns(monkeypatch, ["93.184.216.34"])
    requested = []

    def fetch(url):
        requested.append(url)
        return (
            '<meta property="og:title" content="무선 키보드">'
            '<meta property="product:price:amount" content="29000">'
            '<meta property="product:price:currency" content="KRW">'
            '<meta property="og:image" content="/keyboard.png">',
            "https://shop.example/products/keyboard",
        )

    monkeypatch.setattr(inputs, "fetch_product_html", fetch)
    body = {"url": "  https://shop.example/short-link  "}
    response = client.post(P + "/product-url-drafts", headers=guest_headers, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    draft = result["items"][0]
    assert requested == [body["url"].strip()]
    assert draft["name"] == "무선 키보드" and draft["price"] == 29000
    assert draft["source"] == "URL" and draft["source_url"] == "https://shop.example/products/keyboard"
    assert draft["image_url"] == "https://shop.example/keyboard.png"
    assert "CONFIRM_PRODUCT_INFORMATION" in result["warnings"]
    assert "CONFIRM_SHARED_PRODUCT" not in result["warnings"]
    assert session.scalar(select(func.count(Item.id))) == 0
    # The user's correction is authoritative; preview generation did not register anything.
    confirmed = {k: draft[k] for k in ("name", "category", "source", "source_url", "image_url")}
    confirmed.update(price=28000, purchase_reasons=["NECESSITY"], question_mode="LOCAL")
    headers = {**guest_headers, "Idempotency-Key": "url-registration-001"}
    response = client.post(P + "/items/batch", headers=headers, json={"items": [confirmed]})
    assert response.status_code == 201, response.text
    assert (
        client.post(P + "/items/batch", headers=headers, json={"items": [confirmed]}).json()
        == response.json()
    )
    item = response.json()["items"][0]
    assert item["source"] == "URL" and item["price"] == 28000 and item["source_url"] == draft["source_url"]
    assert item["simulation"]["question_mode"] == "LOCAL"
    assert session.scalar(select(func.count(Item.id))) == 1
    detail = client.get(P + "/items/" + item["id"], headers=guest_headers).json()
    assert detail["source"] == "URL" and detail["image_url"] == draft["image_url"]


def test_missing_metadata_is_manual_and_cannot_register_without_price(
    client, guest_headers, session, monkeypatch
):
    monkeypatch.setattr(inputs, "fetch_product_html", lambda _: ("<html></html>", "https://shop.example/p"))
    response = client.post(
        P + "/product-url-drafts", headers=guest_headers, json={"url": "https://shop.example/p"}
    )
    assert response.status_code == 200
    result = response.json()
    draft = result["items"][0]
    assert draft["price"] is None and draft["simulation_days"] is None
    assert {"NAME_REQUIRED", "PRICE_REQUIRED", "PRODUCT_IMAGE_UNAVAILABLE"} <= set(result["warnings"])
    item = {k: draft[k] for k in ("name", "price", "category", "source", "source_url")}
    item["purchase_reasons"] = ["NECESSITY"]
    headers = {**guest_headers, "Idempotency-Key": "missing-price-001"}
    assert client.post(P + "/items/batch", headers=headers, json={"items": [item]}).status_code == 422
    item["price"] = 10000
    item.pop("source_url")
    assert client.post(P + "/items/batch", headers=headers, json={"items": [item]}).status_code == 422
    assert session.scalar(select(func.count(Item.id))) == 0


def test_url_drafts_require_guest_and_unavailable_page_keeps_manual_path(client, guest_headers, monkeypatch):
    def unavailable(_):
        raise AppError(422, "PRODUCT_URL_UNAVAILABLE", "상품 정보를 불러올 수 없어요. 직접 입력해 주세요.")

    monkeypatch.setattr(inputs, "fetch_product_html", unavailable)
    body = {"url": "https://shop.example/p"}
    assert client.post(P + "/product-url-drafts", json=body).status_code == 401
    response = client.post(P + "/product-url-drafts", headers=guest_headers, json=body)
    assert response.status_code == 422 and response.json()["error"]["code"] == "PRODUCT_URL_UNAVAILABLE"
    legacy = client.post(P + "/share-drafts", headers=guest_headers, json=body)
    assert legacy.status_code == 422 and legacy.json()["error"]["code"] == "SHARE_UNAVAILABLE"


@pytest.mark.parametrize(
    "url", ["http://shop.example/p", "https://@shop.example/p", "https://shop.example/a b"]
)
def test_invalid_product_urls_never_connect(client, guest_headers, monkeypatch, url):
    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid URL must be rejected before any connection")

    monkeypatch.setattr(sharing, "PinnedConnection", forbidden)
    monkeypatch.setattr(sharing.socket, "getaddrinfo", forbidden)
    response = client.post(P + "/product-url-drafts", headers=guest_headers, json={"url": url})
    assert response.status_code == 422


def test_private_product_url_is_rejected(client, guest_headers, monkeypatch):
    dns(monkeypatch, ["127.0.0.1"])
    response = client.post(
        P + "/product-url-drafts", headers=guest_headers, json={"url": "https://localhost/private"}
    )
    assert response.status_code == 422 and response.json()["error"]["code"] == "UNSAFE_PRODUCT_URL"


def test_korean_path_and_query_are_encoded_without_double_encoding(monkeypatch):
    dns(monkeypatch, ["93.184.216.34"])
    _, _, path = sharing.public_target("https://shop.example/상품?q=키보드&ref=a%2Fb#details")
    assert path == "/%EC%83%81%ED%92%88?q=%ED%82%A4%EB%B3%B4%EB%93%9C&ref=a%2Fb"


@pytest.mark.parametrize("image", ["/lamp.png", ["/lamp.png"], {"@type": "ImageObject", "url": "/lamp.png"}])
def test_jsonld_product_image_is_used_when_og_image_is_absent(monkeypatch, image):
    dns(monkeypatch, ["93.184.216.34"])
    product = {
        "@type": "Product",
        "name": "무드등",
        "image": image,
        "offers": {"price": 19000, "priceCurrency": "KRW"},
    }
    html = '<script type="application/ld+json">' + json.dumps(product) + "</script>"
    metadata = sharing.extract_metadata(html, "https://shop.example/p")
    assert metadata["name"] == "무드등" and metadata["price"] == 19000
    assert metadata["image_url"] == "https://shop.example/lamp.png"
