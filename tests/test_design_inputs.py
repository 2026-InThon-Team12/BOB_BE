import socket

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.lablup import LablupError
from app.services import discounts, sharing
from app.services.discounts import DiscountInput, calculate_discount, extract_discount
from tests.test_ai import png_bytes


def test_discount_original_plan_and_shipping_alternative():
    result = calculate_discount(
        DiscountInput(
            original_subtotal=20000, additional_subtotal=10000, discount_minimum=30000, discount_percent=10
        )
    )
    assert result["original"]["total"] == 20000 and result["expanded"]["total"] == 27000
    assert result["extra_spending"] == 7000
    shipping = calculate_discount(
        DiscountInput(
            original_subtotal=20000, additional_subtotal=0, shipping_fee=3000, free_shipping_minimum=30000
        )
    )
    assert shipping["remaining_to_free_shipping"] == 10000 and shipping["pay_shipping_saves"] == 7000
    cheaper = calculate_discount(
        DiscountInput(
            original_subtotal=29000, additional_subtotal=1000, shipping_fee=3000, free_shipping_minimum=30000
        )
    )
    assert cheaper["extra_spending"] == -2000


def test_discount_applies_existing_discount_and_shipping_threshold_basis():
    result = calculate_discount(
        DiscountInput(
            original_subtotal=20000,
            additional_subtotal=10000,
            discount_percent=10,
            shipping_fee=3000,
            free_shipping_minimum=30000,
            shipping_threshold_basis="AFTER_DISCOUNT",
        )
    )
    assert result["original"]["total"] == 21000 and result["expanded"]["total"] == 30000
    assert result["extra_spending"] == 9000 and result["expanded"]["shipping"] == 3000
    assert (
        calculate_discount(DiscountInput(original_subtotal=101, additional_subtotal=0, discount_percent=10))[
            "original"
        ]["discount"]
        == 10
    )
    with pytest.raises(ValidationError):
        DiscountInput(original_subtotal=100, additional_subtotal=10, discount_percent=10, discount_fixed=1)


def test_discount_comparison_persistence_and_ownership(client, guest_headers):
    headers = {**guest_headers, "Idempotency-Key": "discount-comparison"}
    body = {
        "original_subtotal": 20000,
        "additional_subtotal": 10000,
        "discount_minimum": 30000,
        "discount_percent": 10,
    }
    first = client.post("/api/v1/discount-comparisons", headers=headers, json=body)
    assert first.status_code == 201
    assert client.post("/api/v1/discount-comparisons", headers=headers, json=body).json() == first.json()
    assert (
        client.post(
            "/api/v1/discount-comparisons", headers=headers, json={**body, "additional_subtotal": 20000}
        ).status_code
        == 409
    )
    url = "/api/v1/discount-comparisons/" + first.json()["id"]
    assert (
        client.put(url + "/decision", headers=guest_headers, json={"decision": "REMOVE_EXTRA"}).json()[
            "decision"
        ]
        == "REMOVE_EXTRA"
    )
    assert client.get("/api/v1/wallet", headers=guest_headers).json()["spent_amount"] == 0
    other = client.post("/api/v1/guest-sessions").json()
    assert client.get(url, headers={"Authorization": "Bearer " + other["access_token"]}).status_code == 404


def test_discount_image_fallback_correction_and_unknown_prices(monkeypatch):
    monkeypatch.setattr(discounts, "extract_ocr", lambda *_: "3만원 이상 10% 할인")

    class FakeClient:
        calls = 0

        def message(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise LablupError("IMAGE_UNSUPPORTED", image_unsupported=True)
            if self.calls == 2:
                return "invalid output"
            return '{"discount_minimum":30000,"discount_percent":10}'

    fake = FakeClient()
    result = extract_discount(png_bytes(), Settings(_env_file=None, lablup_vision_enabled=True), fake)
    assert fake.calls == 3 and result["analysis_method"] == "ocr"
    assert result["conditions"]["shipping_fee"] is None
    assert result["conditions"]["discount_percent"] == 10

    class FailedClient:
        def message(self, **kwargs):
            raise LablupError("AI_TIMEOUT")

    result = extract_discount(png_bytes(), Settings(_env_file=None), FailedClient())
    assert result["analysis_method"] == "manual" and "AI_TIMEOUT" in result["warnings"]


def dns(monkeypatch, addresses):
    monkeypatch.setattr(
        sharing,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses
        ],
    )


@pytest.mark.parametrize(
    "url", ["http://shop.example/p", "https://user:pass@shop.example/p", "https://shop.example:8443/p"]
)
def test_share_rejects_unsafe_url_forms(url):
    with pytest.raises(AppError):
        sharing.public_target(url)


@pytest.mark.parametrize(
    "addresses", [["127.0.0.1"], ["169.254.169.254"], ["10.1.2.3"], ["::1"], ["93.184.216.34", "192.168.1.1"]]
)
def test_share_rejects_private_or_mixed_dns(monkeypatch, addresses):
    dns(monkeypatch, addresses)
    with pytest.raises(AppError):
        sharing.public_target("https://shop.example/product")


def test_metadata_price_must_be_known_krw_and_single_offer(monkeypatch):
    dns(monkeypatch, ["93.184.216.34"])
    html = '<meta property="og:title" content="무드등"><meta property="product:price:amount" content="29000"><meta property="product:price:currency" content="KRW"><meta property="og:image" content="/lamp.png">'
    metadata = sharing.extract_metadata(html, "https://shop.example/p")
    assert metadata["name"] == "무드등" and metadata["price"] == 29000
    assert metadata["image_url"] == "https://shop.example/lamp.png"
    assert sharing.extract_metadata(html.replace("KRW", "USD"), "https://shop.example/p")["price"] is None
    assert sharing.extract_metadata(html.replace("29000", "NaN"), "https://shop.example/p")["price"] is None
    assert sharing.extract_metadata("<title>상품</title>", "https://shop.example/p")["price"] is None
    ld = '<script type="application/ld+json">{"@type":"Product","name":"코트","offers":[{"price":100,"priceCurrency":"KRW"},{"price":200,"priceCurrency":"KRW"}]}</script>'
    assert sharing.extract_metadata(ld, "https://shop.example/p")["price"] is None


def test_share_redirect_revalidates_before_second_connection(monkeypatch):
    monkeypatch.setattr(
        sharing,
        "getaddrinfo",
        lambda host, *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1" if host == "localhost" else "93.184.216.34", 443),
            )
        ],
    )
    opened = []

    class Connection:
        def __init__(self, host, address):
            opened.append((host, address))

        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            return type(
                "Response", (), {"status": 302, "getheader": lambda *_: "https://localhost/private"}
            )()

        def close(self):
            pass

    monkeypatch.setattr(sharing, "PinnedConnection", Connection)
    with pytest.raises(AppError):
        sharing.fetch_product_html("https://shop.example/p")
    assert opened == [("shop.example", "93.184.216.34")]


def test_share_draft_preserves_missing_price(client, guest_headers, monkeypatch):
    from app.api import inputs

    monkeypatch.setattr(
        inputs, "fetch_product_html", lambda _: ("<title>무선 키보드</title>", "https://shop.example/p")
    )
    response = client.post(
        "/api/v1/share-drafts", headers=guest_headers, json={"url": "https://shop.example/p"}
    )
    assert response.status_code == 200, response.text
    draft = response.json()["items"][0]
    assert draft["price"] is None and draft["source"] == "SHARE" and draft["simulation_days"] is None
    assert "PRICE_REQUIRED" in response.json()["warnings"]
