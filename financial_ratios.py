"""STEP 2: 재무지표의 계산만 수행한다 (양호/주의/위험 판정 없음).

입력: analysis.schemas.FinancialData 구조에 맞게 정규화된 연간 재무데이터.
출력: 연도별 지표 사전. 비율의 단위는 %, 영업현금흐름의 단위는 원.

계산할 수 없거나 해석상 의미가 없는 경우 값은 None으로 두고 사유를 기록한다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .schemas import FinancialData, FinancialStatement


RATIO_LABELS = {
    "debt_ratio": "부채비율",
    "current_ratio": "유동비율",
    "equity_ratio": "자기자본비율",
    "operating_margin": "영업이익률",
    "net_margin": "순이익률",
    "roa": "총자산순이익률(ROA)",
    "operating_cash_flow": "영업활동현금흐름",
    "cash_flow_to_debt": "부채 대비 영업현금 창출력",
    "investing_cash_flow": "투자활동현금흐름",
    "financing_cash_flow": "재무활동현금흐름",
    "revenue_growth": "매출액 증가율",
    "operating_income_growth": "영업이익 증가율",
    "total_asset_growth": "총자산증가율",
    "ppe_growth": "유형자산증가율",
    "capital_impairment_ratio": "자본잠식률",
}


def parent_equity(statement: FinancialStatement) -> tuple[int | None, str | None, str | None]:
    """자본잠식률에 쓸 '지배기업 몫 자본'과 그 기준. (값, 기준, 못 구한 사유)

    - 연결: 지배기업 소유주지분을 쓴다. 연결 자본총계에는 비지배지분이 섞여 있어 잠식률이 낮게 나온다.
    - 소유주지분 행이 없을 때는 비지배지분이 없거나 0인 경우에만 자본총계로 대신한다(별도재무제표 포함).
    """
    owners = statement.get("equity_attributable_to_owners")
    if owners is not None:
        return owners, "owners_equity", None
    total = statement.get("total_equity")
    nci = statement.get("noncontrolling_interests")
    if total is None:
        return None, None, "자본총계 누락"
    if nci not in (None, 0):
        return None, None, "비지배지분이 있으나 지배기업 소유주지분 누락"
    return total, "total_equity", None


def capital_impairment(statement: FinancialStatement) -> dict[str, Any]:
    """자본잠식률 = (자본금 − 지배기업 몫 자본) ÷ 자본금 × 100.

    자본이 자본금보다 크면(잠식 없음) 0%로 표시하고 원래 계산값은 raw_value에 남긴다.
    """
    capital = statement.get("issued_capital")
    equity, basis, why = parent_equity(statement)
    out: dict[str, Any] = {"value": None, "raw_value": None, "reason": None, "basis": basis,
                           "issued_capital": capital, "parent_equity": equity}
    if equity is None:
        out["reason"] = why
    elif capital is None:
        out["reason"] = "자본금 누락"
    elif capital <= 0:
        out["reason"] = "자본금이 0 이하라 잠식률 계산 불가"
    else:
        raw = round((capital - equity) / capital * 100, 2)
        out["raw_value"] = raw
        out["value"] = max(raw, 0.0)
    return out


def _result(numerator: int | None, denominator: int | float | None,
            *, invalid_nonpositive: bool = True) -> dict[str, Any]:
    """나누기 계산. 비정상 분모나 필수값 누락은 None과 사유 반환."""
    if numerator is None or denominator is None:
        return {"value": None, "reason": "필수 재무항목 누락"}
    if denominator == 0:
        return {"value": None, "reason": "분모가 0임"}
    if invalid_nonpositive and denominator < 0:
        return {"value": None, "reason": "분모가 음수여서 일반적 비율 해석 불가"}
    return {"value": round(numerator / denominator * 100, 2), "reason": None}


def _growth(current: int | None, previous: int | None,
            *, label: str) -> dict[str, Any]:
    if current is None or previous is None:
        return {"value": None, "reason": f"{label}: 당기 또는 전기 금액 누락", "change_type": "unknown"}
    if previous <= 0:
        if previous < 0 and current > 0:
            change_type = "turnaround_to_profit"
        elif previous > 0 and current < 0:
            change_type = "turnaround_to_loss"  # 이 분기는 정상 계산 경로를 거치므로 아래에서 다룸
        elif previous < 0 and current == 0:
            change_type = "recovered_to_zero"
        elif previous < 0 and current < 0:
            change_type = "loss_narrowed" if current > previous else "loss_widened" if current < previous else "unchanged_loss"
        elif previous == 0 and current > 0:
            change_type = "from_zero_to_positive"
        elif previous == 0 and current < 0:
            change_type = "from_zero_to_negative"
        else:
            change_type = "zero_to_zero"
        return {"value": None, "reason": f"{label}: 전기 금액이 0 이하라 일반적 증가율 해석 불가", "change_type": change_type}
    return {
        "value": round((current - previous) / previous * 100, 2),
        "reason": None,
        "change_type": "turnaround_to_loss" if current < 0 else "normal",
    }


def calculate_year_ratios(
    current: FinancialStatement,
    previous: FinancialStatement | None = None,
) -> dict[str, dict[str, Any]]:
    """해당 연도 비율 계산. ROA와 성장률에는 전년도 자료가 필요함."""
    assets = current.get("total_assets")
    liabilities = current.get("total_liabilities")
    equity = current.get("total_equity")
    revenue = current.get("revenue")
    operating_income = current.get("operating_income")
    net_income = current.get("net_income")
    ocf = current.get("operating_cash_flow")

    prev_assets = previous.get("total_assets") if previous else None
    avg_assets = (assets + prev_assets) / 2 if assets is not None and prev_assets is not None else None

    results = {
        "debt_ratio": _result(liabilities, equity),
        "current_ratio": _result(current.get("current_assets"), current.get("current_liabilities")),
        "equity_ratio": _result(equity, assets),
        "operating_margin": _result(operating_income, revenue),
        "net_margin": _result(net_income, revenue),
        "roa": _result(net_income, avg_assets),
        "operating_cash_flow": {"value": ocf, "reason": None if ocf is not None else "영업활동현금흐름 누락"},
        "cash_flow_to_debt": _result(ocf, liabilities),
        "investing_cash_flow": {"value": current.get("investing_cash_flow"), "reason": None if current.get("investing_cash_flow") is not None else "투자활동현금흐름 누락"},
        "financing_cash_flow": {"value": current.get("financing_cash_flow"), "reason": None if current.get("financing_cash_flow") is not None else "재무활동현금흐름 누락"},
        "revenue_growth": _growth(revenue, previous.get("revenue") if previous else None, label="매출액"),
        "operating_income_growth": _growth(operating_income, previous.get("operating_income") if previous else None, label="영업이익"),
        "total_asset_growth": _growth(assets, prev_assets, label="총자산"),
        "ppe_growth": _growth(current.get("property_plant_equipment"), previous.get("property_plant_equipment") if previous else None, label="유형자산"),
        "capital_impairment_ratio": capital_impairment(current),
    }
    for key, item in results.items():
        item["label"] = RATIO_LABELS[key]
        item["unit"] = "KRW" if key in {"operating_cash_flow", "investing_cash_flow", "financing_cash_flow"} else "%"
    return results


def calculate_financial_ratios(data: FinancialData) -> dict[str, Any]:
    """기업의 모든 사업연도를 결산일 순서로 계산해 반환한다.

    재무상태표 기준일만 이어졌다고 전기 비교가 가능한 것은 아니므로
    연속된 회계기간(전기 종료일 + 1일 = 당기 시작일)만 비교한다.
    """
    if data.get("currency") != "KRW" or data.get("amount_unit") != "KRW":
        raise ValueError("입력 금액은 KRW(원 단위)로 정규화해야 합니다.")
    if data.get("report_code") != "11011":
        raise ValueError("STEP 2는 사업보고서(11011) 연간 자료만 지원합니다.")
    if data.get("fs_div") not in {"CFS", "OFS"}:
        raise ValueError("fs_div는 CFS 또는 OFS여야 합니다.")

    financials = data.get("financials", {})
    ordered = sorted(financials.items(), key=lambda item: item[1]["period_end"])
    by_year: dict[str, Any] = {}
    prev = None
    for year_key, annual in ordered:
        start = date.fromisoformat(annual["period_start"])
        end = date.fromisoformat(annual["period_end"])
        if start > end:
            raise ValueError(f"{year_key}: 회계기간 시작일이 종료일보다 늦습니다.")
        if str(annual["fiscal_year"]) != year_key:
            raise ValueError(f"{year_key}: fiscal_year와 financials 키가 일치하지 않습니다.")
        comparable_prev = None
        if prev is not None:
            from datetime import timedelta
            prev_end = date.fromisoformat(prev["period_end"])
            if prev_end + timedelta(days=1) == start:
                comparable_prev = prev["statement"]

        by_year[year_key] = {
            "period_start": annual["period_start"],
            "period_end": annual["period_end"],
            "ratios": calculate_year_ratios(annual["statement"], comparable_prev),
        }
        prev = annual

    return {
        "corp_code": data["corp_code"],
        "corp_name": data["corp_name"],
        "fs_div": data["fs_div"],
        "years": by_year,
    }
