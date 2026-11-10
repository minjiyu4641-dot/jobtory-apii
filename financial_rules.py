"""STEP 3: 확정된 회계 계정에 기반해 설명 가능한 위험 신호를 찾아낸다.

범용 MVP의 1차 규칙: 업종별 임계치/종합등급/부도확률을 계산하지 않는다.
입력: STEP 1 FinancialData, STEP 2 calculate_financial_ratios() 반환값.
출력: 연도별 위험 신호 및 평가 불가 항목. 최신 연도만 볼 때는 latest_year 참조.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .financial_ratios import calculate_financial_ratios, capital_impairment
from .schemas import FinancialData

# 자본잠식률 기준(사업연도말). 거래소 상장규정의 관리종목 지정(50% 이상)·상장폐지(전액잠식) 요건 수준을 따른다.
# 실제 지정 여부를 판정하는 것은 아니다(반기 기준·감사의견 등 다른 요건은 보지 않음).
IMPAIRMENT_WATCH_PCT = 50.0
IMPAIRMENT_FULL_PCT = 100.0

RULES = {
    # 기본 심각도는 'critical'이지만, 실제 심각도는 잠식 단계에 따라 플래그마다 정한다(아래 _impairment).
    "CAPITAL_IMPAIRMENT": ("자본잠식 (잠식률 50% 이상)", "critical"),
    "OPERATING_LOSS": ("영업적자", "warning"),
    "NET_LOSS": ("당기순손실", "warning"),
    "NEGATIVE_OPERATING_CASH_FLOW": ("영업활동현금흐름 음수", "warning"),
    "REPEATED_OPERATING_LOSS": ("2개 연속 회계연도 영업적자", "warning"),
    "REPEATED_NEGATIVE_CASH_FLOW": ("2개 연속 회계연도 영업현금흐름 음수", "warning"),
}


def _flag(code: str, year: str, evidence: dict[str, Any], *, severity: str | None = None,
          title: str | None = None) -> dict[str, Any]:
    base_title, base_severity = RULES[code]
    return {"code": code, "title": title or base_title, "severity": severity or base_severity, "year": year,
            "evidence": evidence}


def impairment_level(rate: float | None, parent_equity: int | None) -> str | None:
    """잠식 단계: 'full'(전액) / 'severe'(50% 이상) / 'partial'(0% 초과 50% 미만) / 'none' / None(판단 불가).

    지배기업 몫 자본이 0 이하이면 자본금을 몰라도 전액잠식이 확실하다.
    """
    if parent_equity is not None and parent_equity <= 0:
        return "full"
    if rate is None:
        return None
    if rate >= IMPAIRMENT_FULL_PCT:
        return "full"
    if rate >= IMPAIRMENT_WATCH_PCT:
        return "severe"
    return "partial" if rate > 0 else "none"


def _impairment(year: str, row: dict[str, Any]) -> tuple[dict | None, dict | None, dict]:
    """자본잠식 평가 → (플래그, 미평가 사유, 근거). 근거는 플래그가 없어도 화면 설명용으로 남긴다."""
    calc = capital_impairment(row)
    level = impairment_level(calc["value"], calc["parent_equity"])
    evidence = {"capital_impairment_ratio": calc["value"], "issued_capital": calc["issued_capital"],
                "parent_equity": calc["parent_equity"], "equity_basis": calc["basis"],
                "level": level, "unit": "KRW"}
    if level is None:
        return None, {"code": "CAPITAL_IMPAIRMENT", "reason": calc["reason"] or "자본잠식률 계산 불가"}, evidence
    if level == "full":
        return _flag("CAPITAL_IMPAIRMENT", year, evidence, severity="critical", title="완전자본잠식"), None, evidence
    if level == "severe":
        return _flag("CAPITAL_IMPAIRMENT", year, evidence, severity="warning",
                     title="부분자본잠식 (잠식률 50% 이상)"), None, evidence
    return None, None, evidence


def evaluate_financial_rules(data: FinancialData,
                             ratios_result: dict[str, Any] | None = None) -> dict[str, Any]:
    """연도별 위험 신호 판정. 계정 누락은 신호 없음이 아니라 미평가로 처리."""
    if ratios_result is None:
        ratios_result = calculate_financial_ratios(data)
    if ratios_result.get("corp_code") != data["corp_code"] or ratios_result.get("fs_div") != data["fs_div"]:
        raise ValueError("재무비율 결과의 기업번호 또는 재무제표 구분이 입력과 다릅니다.")
    if set(ratios_result.get("years", {})) != set(data["financials"]):
        raise ValueError("재무비율 결과의 사업연도와 입력 데이터의 사업연도가 다릅니다.")

    ordered = sorted(data["financials"].items(), key=lambda row: row[1]["period_end"])
    years: dict[str, Any] = {}
    previous: tuple[str, dict[str, Any]] | None = None
    for year, annual in ordered:
        row = annual["statement"]
        flags: list[dict[str, Any]] = []
        unassessed: list[dict[str, str]] = []
        flag, missing, impairment = _impairment(year, row)
        if flag:
            flags.append(flag)
        if missing:
            unassessed.append(missing)
        singles = [
            ("OPERATING_LOSS", "operating_income", lambda x: x < 0),
            ("NET_LOSS", "net_income", lambda x: x < 0),
            ("NEGATIVE_OPERATING_CASH_FLOW", "operating_cash_flow", lambda x: x < 0),
        ]
        for code, field, condition in singles:
            value = row.get(field)
            if value is None:
                unassessed.append({"code": code, "reason": f"{field} 계정 누락"})
            elif condition(value):
                flags.append(_flag(code, year, {field: value, "unit": "KRW"}))

        consecutive = False
        if previous is not None:
            prev_annual = previous[1]
            consecutive = (date.fromisoformat(prev_annual["period_end"]) + timedelta(days=1)
                           == date.fromisoformat(annual["period_start"]))
        for code, field in [
            ("REPEATED_OPERATING_LOSS", "operating_income"),
            ("REPEATED_NEGATIVE_CASH_FLOW", "operating_cash_flow"),
        ]:
            if not consecutive:
                unassessed.append({"code": code, "reason": "연속된 직전 회계기간 없음"})
                continue
            previous_value = previous[1]["statement"].get(field)
            current_value = row.get(field)
            if previous_value is None or current_value is None:
                unassessed.append({"code": code, "reason": f"전기 또는 당기 {field} 누락"})
            elif previous_value < 0 and current_value < 0:
                flags.append(_flag(code, year, {
                    "previous_year": previous[0], "previous_value": previous_value,
                    "current_value": current_value, "unit": "KRW"}))

        years[year] = {"period_end": annual["period_end"],
                       "flags": flags, "unassessed": unassessed,
                       "capital_impairment": impairment,  # 50% 미만 부분잠식도 설명할 수 있도록 항상 기록
                       "flag_count": len(flags), "evaluated_rules": len(RULES) - len(unassessed)}
        previous = (year, annual)

    latest = ordered[-1][0] if ordered else None
    return {"corp_code": data["corp_code"], "corp_name": data["corp_name"],
            "fs_div": data["fs_div"], "latest_year": latest, "years": years,
            "note": "위험 신호 탐지 결과이며 기업 종합 위험등급·부도확률이 아닙니다."}
