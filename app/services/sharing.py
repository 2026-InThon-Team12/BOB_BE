"""Bounded HTTPS metadata retrieval with DNS-pinned public connections."""

import http.client
import ipaddress
import json
import re
import socket
from socket import getaddrinfo
import ssl
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

from app.core.errors import AppError


def public_target(url: str) -> tuple[str, str, str]:
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
        ):
            raise ValueError()
        host = parts.hostname.encode("idna").decode("ascii")
        if len(host) > 253 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
            raise ValueError()
        addresses = {entry[4][0] for entry in getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError()
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(parts.query, safe="%/?@!$&'()*+,;=:-._~")
        return host, sorted(addresses)[0], path + ("?" + query if query else "")
    except (ValueError, OSError, UnicodeError):
        raise AppError(422, "UNSAFE_PRODUCT_URL", "공개 HTTPS 상품 URL만 불러올 수 있습니다.") from None


class PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str):
        super().__init__(hostname, timeout=5, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Connect to the exact validated address. TLS still verifies the original hostname.
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def fetch_product_html(url: str) -> tuple[str, str]:
    for _ in range(4):
        host, address, path = public_target(url)
        connection = PinnedConnection(host, address)
        try:
            connection.request(
                "GET",
                path,
                headers={
                    "User-Agent": "BoB-Metadata/1.0",
                    "Accept": "text/html",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()
            if response.status in (401, 403, 429):
                raise AppError(422, "PRODUCT_URL_BLOCKED", "쇼핑몰이 자동 상품 조회를 허용하지 않아요. 상품명과 가격을 직접 입력하거나 장바구니 사진으로 등록해 주세요.")
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    break
                url = urljoin(url, location)
                continue
            if response.status != 200 or "text/html" not in response.getheader("Content-Type", "").lower():
                break
            content = response.read(4_194_305)
            if len(content) > 4_194_304:
                break
            charset = response.headers.get_content_charset() or "utf-8"
            return content.decode(charset, errors="replace"), url
        except (OSError, http.client.HTTPException, LookupError, ValueError):
            break
        finally:
            connection.close()
    raise AppError(
        422, "PRODUCT_URL_UNAVAILABLE", "상품 정보를 불러올 수 없어요. URL을 확인하거나 직접 입력해 주세요."
    )


class ProductMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.title, self.in_title, self.in_ld, self.ld, self.products = (
            {},
            [],
            False,
            False,
            [],
            [],
        )

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = attrs.get("property", attrs.get("name", attrs.get("itemprop", ""))).lower()
            self.meta[key] = attrs.get("content", "")
        if tag == "title":
            self.in_title = True
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self.in_ld, self.ld = True, []

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "script" and self.in_ld:
            self.in_ld = False
            try:
                data = json.loads("".join(self.ld))
                self.products.extend(product_nodes(data))
            except (ValueError, TypeError, RecursionError):
                pass

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if self.in_ld:
            self.ld.append(data)


def product_nodes(data, depth=0):
    """Read nested schema.org mainEntity/@graph without treating offers as products."""
    if depth > 20:
        return
    if isinstance(data, list):
        for value in data:
            yield from product_nodes(value, depth + 1)
    elif isinstance(data, dict):
        kinds = data.get("@type", [])
        if isinstance(kinds, str):
            kinds = [kinds]
        if isinstance(kinds, list) and "Product" in kinds:
            yield data
        else:
            for value in data.values():
                if isinstance(value, (dict, list)):
                    yield from product_nodes(value, depth + 1)


def extract_metadata(html: str, url: str) -> dict:
    parser = ProductMetadata()
    parser.feed(html)
    product = parser.products[0] if len(parser.products) == 1 else {}
    offer = product.get("offers", {})
    if isinstance(offer, list):
        offer = offer[0] if len(offer) == 1 else {}
    if not isinstance(offer, dict):
        offer = {}
    name = next(
        (
            value.strip()
            for value in (product.get("name"), parser.meta.get("og:title"), parser.meta.get("name"), "".join(parser.title))
            if isinstance(value, str) and value.strip()
        ),
        None,
    )
    amount = parser.meta.get("product:price:amount", offer.get("price", parser.meta.get("price")))
    currency = parser.meta.get("product:price:currency", offer.get("priceCurrency", parser.meta.get("pricecurrency")))
    price = None
    try:
        value = str(amount).strip()
        if re.fullmatch(r"\d{1,3}(,\d{3})+(\.0+)?", value):
            value = value.replace(",", "")
        number = Decimal(value)
        if (
            isinstance(currency, str) and currency.strip().upper() == "KRW"
            and number.is_finite()
            and number == number.to_integral_value()
            and 0 < number <= 1_000_000_000_000
        ):
            price = int(number)
    except (InvalidOperation, ValueError):
        pass
    image = parser.meta.get("og:image") or product.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl")
    if not isinstance(image, str) or not image.strip():
        image = None
    if image:
        image = urljoin(url, image.strip())
        try:
            public_target(image)
        except AppError:
            image = None
    return {
        "name": str(name or "상품명 확인 필요")[:200],
        "name_missing": name is None,
        "price": price,
        "image_url": image,
        "source_url": url,
    }
