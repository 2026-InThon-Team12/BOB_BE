"""The 14 fixed pre-purchase prompts supplied by the user."""

from app.services.question_catalog import Template, choices, YES_NO, CONTEXTS

VERSION = "pre-v1"
FIRST_USE_SPEC = {
    "type": "OBJECT",
    "fields": {
        "when": choices(
            ("TODAY", "구매 당일"),
            ("TOMORROW", "구매 다음 날"),
            ("THIS_WEEK", "일주일 안에"),
            ("THIS_MONTH", "한 달 안에"),
            ("UNPLANNED", "아직 계획 없음"),
            ("UNSURE", "잘 모르겠어요"),
        ),
        "context": {
            "type": "CHOICE",
            "options": [row for row in CONTEXTS["options"] if row["code"] != "NO_USE"],
        },
    },
}
CATALOG = (
    Template(
        "DISCOVERY",
        "TRIGGER",
        "이 물건을 어떻게 알게 됐어요?",
        "{item}을(를) 어떻게 알게 됐어요?",
        choices(
            ("ADVERTISEMENT", "광고"),
            ("SNS", "SNS·유튜버"),
            ("FRIEND", "친구"),
            ("SEARCH", "필요해서 직접 찾음"),
            ("OTHER", "기타"),
        ),
    ),
    Template(
        "CURRENT_MOOD",
        "TRIGGER",
        "지금 기분은 어때요?",
        "{item}을(를) 사고 싶은 지금 기분은 어때요?",
        choices(
            ("STRESS", "스트레스"), ("BORED", "심심함"), ("HAPPY", "기분 좋음"), ("NORMAL", "평소와 같음")
        ),
    ),
    Template(
        "DISCOUNT_PRESSURE",
        "TRIGGER",
        "할인이나 한정 때문에 서두르고 있나요? 정가여도 샀을까요?",
        "{item}의 할인·한정 판매 때문에 서두르고 있나요? 정가여도 살까요?",
        choices(
            ("HURRIED_BUY", "서두르고 있지만 정가여도 살 거예요"),
            ("HURRIED_SKIP", "할인·한정이 아니면 안 살 것 같아요"),
            ("NO_PRESSURE", "할인·한정 때문에 서두르는 건 아니에요"),
            ("UNSURE", "잘 모르겠어요"),
        ),
    ),
    Template(
        "RECENT_NEED",
        "NECESSITY",
        "최근에 이게 없어서 불편했던 적이 있어요?",
        "최근에 {item}이 없어서 불편했던 적이 있어요?",
        YES_NO,
    ),
    Template(
        "ALTERNATIVES",
        "NECESSITY",
        "이미 비슷한 걸 갖고 있나요? 그걸로 대신할 순 없나요?",
        "{item}과 비슷한 것을 이미 갖고 있나요? 그걸로 대신할 수 있나요?",
        choices(
            ("NONE", "비슷한 게 없어요"),
            ("SUFFICIENT", "있고 대신 쓸 수 있어요"),
            ("INSUFFICIENT", "있지만 대신 쓰기 어려워요"),
            ("UNSURE", "잘 모르겠어요"),
        ),
    ),
    Template(
        "PROBLEM_TO_SOLVE",
        "NECESSITY",
        "이게 해결해 주는 문제를 한 문장으로 말한다면?",
        "{item}이 해결해 줄 문제를 한 문장으로 말한다면 무엇인가요?",
        {"type": "TEXT", "max_length": 300, "options": [{"code": "UNSURE", "label": "잘 모르겠어요"}]},
    ),
    Template(
        "FIRST_USE",
        "PREDICTION",
        "산다면 언제, 어디서 처음 쓸 것 같아요?",
        "{item}을(를) 산다면 언제, 어떤 상황에서 처음 쓸 것 같아요?",
        FIRST_USE_SPEC,
    ),
    Template(
        "EXPECTED_USE_DAYS",
        "PREDICTION",
        "한 달에 몇 번 쓸 것 같아요?",
        "{item}을(를) 산 뒤 30일 동안 며칠 사용할 것 같아요?",
        {
            "type": "INTEGER",
            "unit": "DAYS",
            "minimum": 0,
            "maximum": 30,
            "options": [{"code": "UNSURE", "label": "잘 모르겠어요"}],
        },
    ),
    Template(
        "CASH_CHOICE",
        "VALUE",
        "이 금액을 지금 현금으로 받는다면, 그래도 이걸 살까요?",
        "{item}의 가격만큼 지금 현금으로 받는다면, 그래도 이 물건을 살까요?",
        choices(("BUY", "그래도 살 거예요"), ("KEEP", "현금으로 갖고 있을래요"), ("UNSURE", "잘 모르겠어요")),
    ),
    Template(
        "WORK_VALUE",
        "VALUE",
        "이건 알바 ○시간이에요. 그만큼의 가치가 있나요?",
        "{item}을(를) 사는 데 필요한 근로 시간을 생각하면 그만큼의 가치가 있나요?",
        choices(("YES", "가치가 있어요"), ("NO", "그만큼은 아닌 것 같아요"), ("UNSURE", "잘 모르겠어요")),
    ),
    Template(
        "GOAL_DELAY",
        "VALUE",
        "목표를 ○일 미루고도 사고 싶나요?",
        "내 목표 달성이 표시된 일수만큼 늦어져도 {item}을(를) 사고 싶나요?",
        YES_NO,
    ),
    Template(
        "WANTED_WEEK_AGO",
        "PERSISTENCE",
        "일주일 전에도 이게 갖고 싶었나요?",
        "일주일 전에도 {item}이 갖고 싶었나요?",
        YES_NO,
    ),
    Template(
        "SHOW_TO",
        "PERSISTENCE",
        "사고 나서 제일 먼저 누구에게 보여주고 싶어요?",
        "{item}을(를) 사고 나서 제일 먼저 누구에게 보여주고 싶어요?",
        choices(
            ("SELF", "나 자신"),
            ("FAMILY", "가족·연인"),
            ("FRIEND", "친구"),
            ("SNS", "SNS에 올리고 싶어요"),
            ("NO_ONE", "보여줄 생각은 없어요"),
            ("UNSURE", "잘 모르겠어요"),
        ),
    ),
    Template(
        "REPLACE_IF_LOST",
        "PERSISTENCE",
        "산 뒤에 잃어버리면 똑같은 걸 다시 살 건가요?",
        "{item}을(를) 산 뒤 잃어버린다면 똑같은 것을 다시 살 건가요?",
        YES_NO,
    ),
)
BY_CODE = {row.code: row for row in CATALOG}


def catalog_output():
    return {
        "version": VERSION,
        "metric_unit": "usage_days",
        "prediction_window_days": 30,
        "templates": [
            {"code": row.code, "stage": row.stage, "original": row.original, "answer_spec": row.answer_spec}
            for row in CATALOG
        ],
    }
