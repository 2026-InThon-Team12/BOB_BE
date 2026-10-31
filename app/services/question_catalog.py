"""Versioned question bank from the supplied post-purchase questionnaire.

AI may select an eligible code and adapt wording, never define response codes,
observation dates, prices or measured usage. Usage uses DAYS by user decision.
"""

from dataclasses import dataclass

VERSION = "purchase-v1"


def choices(*pairs):
    return {"type": "CHOICE", "options": [{"code": code, "label": label} for code, label in pairs]}


YES_NO = choices(("YES", "네"), ("NO", "아니요"), ("UNSURE", "잘 모르겠어요"))
CONTEXTS = choices(
    ("COMMUTE", "출퇴근·이동"),
    ("WORK_STUDY", "업무·공부"),
    ("EXERCISE", "운동"),
    ("OUTING", "외출·약속"),
    ("HOME", "집·일상생활"),
    ("HOBBY", "취미"),
    ("OTHER", "기타"),
    ("NO_USE", "사용하지 않았어요"),
    ("UNSURE", "잘 모르겠어요"),
)


@dataclass(frozen=True)
class Template:
    code: str
    stage: str
    original: str
    wording: str
    answer_spec: dict


CATALOG = (
    Template(
        "EXPECTATION",
        "IMMEDIATE",
        "받아보니 기대했던 것과 비교해 어때요?",
        "{item}을(를) 받아보니 기대했던 것과 비교해 어때요?",
        choices(("ABOVE", "기대 이상"), ("SIMILAR", "비슷함"), ("BELOW", "기대 이하")),
    ),
    Template(
        "MOOD",
        "IMMEDIATE",
        "지금 기분은?",
        "{item}을(를) 산 지금 기분은 어떤가요?",
        choices(("EXCITED", "설렘"), ("NEUTRAL", "덤덤함"), ("REGRET", "살짝 후회")),
    ),
    Template(
        "CHAIN_PURCHASE",
        "IMMEDIATE",
        "이거 사고 나서 바로 또 사고 싶은 게 생겼나요?",
        "{item}을(를) 사고 나서 바로 또 사고 싶은 게 생겼나요?",
        YES_NO,
    ),
    Template(
        "USAGE_DAYS",
        "USAGE",
        "이번 주에 몇 번 썼어요?",
        "{item}을(를) 표시된 기간 동안 며칠 사용했나요?",
        {"type": "CHOICE", "options": []},
    ),
    Template(
        "USAGE_CONTEXT",
        "USAGE",
        "주로 언제 썼어요?",
        "{item}을(를) 최근에 주로 어떤 상황에서 사용했나요?",
        CONTEXTS,
    ),
    Template(
        "NON_USE_REASON",
        "USAGE",
        "안 썼다면 이유는?",
        "{item}을(를) 사용하지 않았다면 이유는 무엇인가요?",
        choices(
            ("NO_OCCASION", "쓸 일이 없었음"),
            ("INCONVENIENT", "생각보다 불편"),
            ("FORGOT", "잊고 있었음"),
            ("OTHER", "기타"),
            ("USED", "사용했어요"),
        ),
    ),
    Template(
        "BUY_AGAIN",
        "VALUE",
        "지금 다시 고른다면, 이걸 살 건가요?",
        "지금 다시 고른다면 {item}을(를) 살 건가요?",
        choices(
            ("SAME", "그대로 산다"),
            ("CHEAPER", "더 싼 걸 산다"),
            ("NO", "안 산다"),
            ("UNSURE", "잘 모르겠어요"),
        ),
    ),
    Template(
        "VALUE_WORTH",
        "VALUE",
        "지금까지 회당 ○원이에요. 그만한 가치가 있었나요?",
        "{item}의 구매 가격과 실제 사용을 돌아보면 그만한 가치가 있었나요?",
        choices(
            ("YES", "가치가 있었어요"),
            ("PARTLY", "어느 정도 있었어요"),
            ("NO", "별로 없었어요"),
            ("UNSURE", "잘 모르겠어요"),
        ),
    ),
    Template(
        "GOAL_TRADEOFF",
        "VALUE",
        "이 돈을 목표에 썼다면 어땠을 것 같아요?",
        "{item}에 쓴 돈을 내 목표에 썼다면 어땠을 것 같아요?",
        choices(
            ("GOAL", "목표에 쓰는 게 더 좋았겠어요"),
            ("PURCHASE", "이 물건을 산 게 더 좋아요"),
            ("SIMILAR", "비슷해요"),
            ("UNSURE", "잘 모르겠어요"),
        ),
    ),
    Template(
        "PROBLEM_SOLVED",
        "MOTIVE",
        "살 때 이유가 구매 전 답이었는데, 실제로 그 문제가 해결됐나요?",
        "{item}을(를) 살 때 기대했던 필요나 문제가 실제로 해결됐나요?",
        choices(
            ("YES", "해결됐어요"),
            ("PARTLY", "일부 해결됐어요"),
            ("NO", "해결되지 않았어요"),
            ("UNSURE", "잘 모르겠어요"),
        ),
    ),
    Template(
        "TRUE_MOTIVE",
        "MOTIVE",
        "돌아보면 진짜 산 이유는 뭐였던 것 같아요?",
        "돌아보면 {item}을(를) 산 진짜 이유는 무엇이었던 것 같아요?",
        choices(
            ("NEED", "필요"),
            ("MOOD", "기분"),
            ("ADVERTISEMENT", "광고"),
            ("SOCIAL", "남들이 다 있어서"),
            ("OTHER", "기타"),
        ),
    ),
    Template("RECOMMEND", "MOTIVE", "주변에 추천하겠어요?", "주변 사람에게 {item}을(를) 추천하겠어요?", YES_NO),
    Template(
        "LONG_USAGE",
        "LONG_TERM",
        "아직 쓰고 있나요?",
        "{item}을(를) 아직 사용하고 있나요?",
        choices(
            ("OFTEN", "자주"), ("SOMETIMES", "가끔"), ("UNUSED", "방치 중"), ("DISPOSED", "팔았거나 버림")
        ),
    ),
    Template(
        "REPURCHASE", "LONG_TERM", "잃어버리면 다시 살 건가요?", "{item}을(를) 잃어버리면 다시 살 건가요?", YES_NO
    ),
    Template(
        "RESALE_VALUE",
        "LONG_TERM",
        "중고로 판다면 얼마 받을 것 같아요?",
        "{item}을(를) 지금 중고로 판다면 얼마를 받을 수 있을 것 같아요?",
        {
            "type": "INTEGER",
            "unit": "KRW",
            "minimum": 0,
            "maximum": 1_000_000_000_000,
            "options": [{"code": "UNSURE", "label": "잘 모르겠어요"}],
        },
    ),
)
BY_CODE = {template.code: template for template in CATALOG}


def catalog_output():
    return {
        "version": VERSION,
        "metric_unit": "usage_days",
        "templates": [
            {"code": row.code, "stage": row.stage, "original": row.original, "answer_spec": row.answer_spec}
            for row in CATALOG
        ],
    }
