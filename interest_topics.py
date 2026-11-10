"""학과·관심어를 탐색 주제로 연결하는 편집 가능한 안내 사전.

회사에 그 사업이 있다는 증거 또는 특정 학과의 취업 가능성 판단이 아닙니다.
실제 사업은 선택한 주제로 검색한 공시에서 별도로 확인합니다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Topic:
    id: str
    label: str
    terms: tuple[str, ...]
    aliases: tuple[str, ...]


TOPICS = (
    Topic("software", "소프트웨어·시스템 개발", ("소프트웨어", "시스템 개발", "시스템 구축", "SI"), ("소프트웨어", "시스템개발", "백엔드", "프론트엔드", "앱개발")),
    Topic("data", "데이터 분석·AI", ("데이터", "빅데이터", "AI", "인공지능", "분석"), ("데이터", "인공지능", "머신러닝", "ai")),
    Topic("service", "디지털 서비스 기획·UX", ("디지털", "플랫폼", "서비스", "기획", "고객"), ("서비스기획", "디지털서비스", "ux", "ui", "사용자경험", "hci")),
    Topic("mobile", "모바일 서비스", ("모바일", "앱", "플랫폼", "서비스"), ("모바일", "모바일서비스")),
    Topic("itservice", "IT 서비스 구축·운영", ("IT 서비스", "시스템통합", "SI", "SM", "운영", "유지보수"), ("it서비스", "시스템통합", "시스템운영", "유지보수")),
    Topic("cloud", "클라우드·IT 인프라", ("클라우드", "서버", "IT 인프라", "데이터센터"), ("클라우드", "서버", "인프라", "데이터센터")),
    Topic("network", "네트워크·정보보안", ("네트워크", "보안", "통신", "장비"), ("네트워크", "보안", "통신")),
    Topic("factory", "스마트팩토리·공정 자동화", ("스마트팩토리", "스마트공장", "자동화", "공정", "제조"), ("스마트팩토리", "스마트공장", "공정", "생산관리", "품질관리", "자동화")),
    Topic("charging", "전기차 충전", ("전기차", "충전", "EV", "충전기"), ("전기차", "충전", "ev충전")),
    Topic("health", "디지털 헬스케어·원격의료", ("헬스케어", "원격의료", "비대면", "건강", "의료"), ("헬스케어", "원격의료", "디지털헬스", "의료", "건강관리")),
    Topic("xr", "XR·메타버스·콘텐츠 플랫폼", ("메타버스", "XR", "VR", "콘텐츠", "플랫폼"), ("메타버스", "xr", "vr", "콘텐츠", "영상")),
    Topic("sales", "IT 솔루션 영업·사업관리", ("솔루션", "판매", "고객", "수주", "사업"), ("영업", "사업관리", "마케팅")),
    Topic("transport", "교통 시스템", ("교통", "철도", "도로", "요금징수", "교통시스템"), ("교통", "철도", "요금징수")),
)
TOPIC_BY_ID = {t.id: t for t in TOPICS}
# 학과별로 탐색을 시작해 볼 주제입니다. 역량·지원 자격·채용 직무를 뜻하지 않습니다.
MAJOR_TOPICS = (
    (("컴퓨터공학", "컴퓨터과학", "소프트웨어학", "소프트웨어공학", "컴공"), ("software", "data", "cloud", "network")),
    (("산업공학", "산업경영공학", "산공"), ("factory", "data", "itservice")),
    (("전자공학", "전기공학", "전기전자", "정보통신공학", "전자과"), ("network", "charging", "transport", "factory")),
    (("기계공학", "화학공학", "화공", "기계과"), ("factory", "charging")),
    (("통계학", "수학과", "데이터사이언스학", "인공지능학"), ("data", "software", "factory")),
    (("경영학", "경제학", "경영정보", "경영과"), ("service", "sales", "data")),
    (("심리학", "인지과학", "심리과"), ("service", "health")),
    (("영화", "영상학", "영화영상", "영상미디어", "미디어학", "언론정보", "신문방송", "커뮤니케이션학"), ("xr", "service")),
    (("디자인학", "시각디자인", "산업디자인", "디자인과"), ("service", "xr", "mobile")),
    (("간호학", "보건학", "의공학", "생명공학", "간호과"), ("health", "data")),
)
BROAD_TOPICS = {
    "서비스": ("itservice", "service", "mobile", "cloud", "health"),
    "개발": ("software", "mobile", "data", "factory"),
    "기획": ("service", "sales", "xr"),
    "연구": ("data", "health", "factory"),
    "기술": ("software", "network", "factory", "charging"),
    "it": ("itservice", "software", "cloud", "network"),
}


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]", "", value.casefold())


@dataclass(frozen=True)
class InputGuide:
    topic_ids: tuple[str, ...]
    needs_choice: bool
    message: str


def guide_interest(value: str) -> InputGuide:
    query = normalized(value)
    if not query:
        return InputGuide((), False, "학과·전공 또는 구체적인 관심분야를 입력해 주세요.")
    for aliases, ids in MAJOR_TOPICS:
        if any(normalized(alias) in query for alias in aliases):
            return InputGuide(ids, True, "학과에서 탐색해 볼 수 있는 주제예요. 연결해서 볼 분야 하나를 선택하세요. 해당 회사의 실제 사업 여부는 공시로 확인합니다.")
    if query.endswith(("학과", "학부", "전공")):
        return InputGuide(tuple(t.id for t in TOPICS), True, "아직 연결 사전에 없는 학과예요. 아래에서 관심있는 분야를 직접 골라 주세요.")
    if query in BROAD_TOPICS:
        return InputGuide(BROAD_TOPICS[query], True, "입력한 단어의 범위가 넓어요. 어떤 분야의 " + value.strip() + "에 관심있는지 하나를 골라 주세요.")
    ids = tuple(t.id for t in TOPICS if any(normalized(alias) in query for alias in t.aliases))
    if len(ids) > 1:
        return InputGuide(ids, True, "여러 탐색 주제가 연결돼요. 이번에 비교할 분야 하나를 골라 주세요.")
    return InputGuide(ids, False, "관심어를 관련 사업 용어로 넓혀 검색하고, 회사 공시에서 실제 연결 여부를 확인합니다.")


def resolve_interest(value: str, topic_id: str = "") -> tuple[str, tuple[str, ...]]:
    value = value.strip()
    if not 1 <= len(value) <= 200:
        raise ValueError("학과·관심분야를 1~200자로 입력하세요.")
    guide = guide_interest(value)
    if topic_id:
        if topic_id not in guide.topic_ids:
            raise ValueError("입력한 학과·관심어에 연결된 분야를 다시 선택해 주세요.")
        topic = TOPIC_BY_ID[topic_id]
    elif guide.needs_choice:
        raise ValueError("관심어의 범위가 넓거나 학과명입니다. ‘연결해서 볼 분야’에서 하나를 골라 주세요.")
    elif guide.topic_ids:
        topic = TOPIC_BY_ID[guide.topic_ids[0]]
    else:
        return value, (value,)
    return topic.label, tuple(dict.fromkeys((topic.label, *topic.terms)))
