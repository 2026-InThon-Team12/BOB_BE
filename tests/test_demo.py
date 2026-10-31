from app.services.simulations import get_today_cards
from scripts.seed_demo import seed_demo


def test_demo_has_each_stage_without_changing_clock(session, today, client):
    seeded = seed_demo(session, today.value)
    cards = get_today_cards(session, seeded["guest_id"], today.value)
    assert len(cards["questions"]) == 3
    assert len(cards["decisions"]) == len(cards["reviews"]) == 1
    headers = {"Authorization": "Bearer " + seeded["access_token"]}
    response = client.get("/api/v1/today", headers=headers)
    assert response.status_code == 200
    review = client.put(
        f"/api/v1/purchases/{seeded['purchase_id']}/review", headers=headers, json={"frequency": "SOMETIMES"}
    )
    assert review.status_code == 200
    assert review.json()["difference_percentage_points"] == 60.71
    assert review.json()["overprediction_ratio_estimate"] == 4.4
    home = client.get("/api/v1/home", headers=headers).json()
    assert home["goal"]["saved_amount"] == 0
    assert home["wallet"]["remaining_amount"] == 112000
    assert home["goal"]["daily_savings"] == 112000
