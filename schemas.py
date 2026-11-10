"""DART 정규화 데이터 입력 타입 정의 (런타임 검증은 별도 구현 필요)."""
from typing import TypedDict, NotRequired


class FinancialStatement(TypedDict):
    # 재무상태표: 결산일 현재 잔액 (원)
    total_assets: int | None  # 자산총계
    total_liabilities: int | None  # 부채총계
    total_equity: int | None  # 자본총계
    current_assets: int | None  # 유동자산
    current_liabilities: int | None  # 유동부채

    # 손익계산서: 해당 회계연도 발생액 (원)
    revenue: int | None  # 매출액(수익)
    operating_income: int | None  # 영업이익
    net_income: int | None  # 당기순이익

    # 현금흐름표: 해당 회계연도 현금 유출입 (원)
    operating_cash_flow: int | None  # 영업활동현금흐름

    # STEP 5 신규 선택 계정 (누락 시 미평가, 기존 데이터와 호환)
    property_plant_equipment: NotRequired[int | None]  # 유형자산 장부금액
    investing_cash_flow: NotRequired[int | None]  # 투자활동 현금흐름
    financing_cash_flow: NotRequired[int | None]  # 재무활동 현금흐름

    # v3.3 자본잠식률용 (누락 시 미평가)
    issued_capital: NotRequired[int | None]  # 자본금 (연결이면 지배기업 자본금)
    equity_attributable_to_owners: NotRequired[int | None]  # 지배기업 소유주지분 (연결에서만 공시)
    noncontrolling_interests: NotRequired[int | None]  # 비지배지분


class AnnualFinancialData(TypedDict):
    fiscal_year: int  # 프로젝트 규칙: 회계기간 시작연도
    period_start: str  # 회계기간 시작일 (YYYY-MM-DD)
    period_end: str  # 회계기간 종료일 (YYYY-MM-DD)
    statement: FinancialStatement  # 해당 사업연도 재무 계정


class FinancialData(TypedDict):
    corp_code: str  # DART 고유번호
    corp_name: str  # 기업명
    industry_code: str | None  # 업종코드 (분류 체계 별도 합의)
    fs_div: str  # CFS: 연결, OFS: 별도
    report_code: str  # 11011: 사업보고서
    currency: str  # KRW
    amount_unit: str  # KRW (원 단위)
    financials: dict[str, AnnualFinancialData]  # 연도별 정보
