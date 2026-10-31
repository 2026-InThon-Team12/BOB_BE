from scripts.evaluate_cart import score


def test_evaluation_rejects_wrong_photo_even_when_name_and_price_match():
    case = {
        "excluded": 1,
        "expected": [{"price": 10000, "name_hints": ["가방"], "photo_box": [100, 100, 300, 300]}],
    }
    body = {
        "items": [
            {"name": "가방", "price": 10000, "image_crop": {"base64": "fixture", "box": [100, 500, 300, 700]}}
        ],
        "selection_summary": {"excluded": 1},
    }
    result = score(body, case)
    assert result["names"] and result["prices"] and result["selection"]
    assert not result["photos"] and not result["pass"]


def test_evaluation_never_counts_manual_review_as_automatic_success():
    case = {"expected": []}
    body = {"items": [], "review_items": [{"name": "unknown"}]}
    assert not score(body, case)["selection"]


def test_evaluation_records_fallback_instead_of_crediting_vision():
    result = score({"items": [], "extraction_strategy": "spatial_ocr"}, {"expected": []})
    assert result["pass"] and result["strategy"] == "spatial_ocr"
