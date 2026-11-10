"""DART 원본 -> 핵심 15개 계정. 영업/투자/재무 현금흐름은 원본 부호 그대로 보존.

v3.3: 자본잠식률 계산용 자본금·지배기업 소유주지분·비지배지분 3개 계정 추가.

v3.1 변경
- 행의 currency가 KRW가 아니면 금액을 쓰지 않고 'currency_mismatch'로 기록(원화 가정 오류 방지).
- 출처 추적: 접수번호(rcept_no)와 당기 보고서에 함께 실린 전기 금액(frmtrm_amount)을 감사기록에 남긴다.
  전기 금액은 파이프라인에서 직전 연도 보고서 값과 비교해 재작성·결산기 변경 가능성을 점검하는 데 쓴다.
"""
from __future__ import annotations
import calendar
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

FIELDS = (
    'total_assets','total_liabilities','total_equity','current_assets','current_liabilities',
    'revenue','operating_income','net_income','operating_cash_flow',
    'property_plant_equipment','investing_cash_flow','financing_cash_flow',
    # v3.3 자본잠식률: 자본금, 지배기업 소유주지분(연결), 비지배지분(대체 계산 가능 여부 판단용)
    'issued_capital','equity_attributable_to_owners','noncontrolling_interests'
)
BASE_ACCOUNTS = {
 'total_assets': (('BS',),('ifrs-full_Assets','ifrs_Assets')),
 'total_liabilities': (('BS',),('ifrs-full_Liabilities','ifrs_Liabilities')),
 'total_equity': (('BS',),('ifrs-full_Equity','ifrs_Equity')),
 'current_assets': (('BS',),('ifrs-full_CurrentAssets','ifrs_CurrentAssets')),
 'current_liabilities': (('BS',),('ifrs-full_CurrentLiabilities','ifrs_CurrentLiabilities')),
 'revenue': (('IS','CIS'),('ifrs-full_Revenue','ifrs-full_RevenueFromContractsWithCustomers','ifrs_Revenue')),
 'operating_income': (('IS','CIS'),('dart_OperatingIncomeLoss','ifrs-full_ProfitLossFromOperatingActivities','ifrs_ProfitLossFromOperatingActivities')),
 'net_income': (('IS','CIS'),('ifrs-full_ProfitLoss','ifrs_ProfitLoss')),
 'operating_cash_flow': (('CF',),('ifrs-full_CashFlowsFromUsedInOperatingActivities','ifrs_CashFlowsFromUsedInOperatingActivities')),
 'property_plant_equipment': (('BS',),('ifrs-full_PropertyPlantAndEquipment','ifrs_PropertyPlantAndEquipment')),
 'investing_cash_flow': (('CF',),('ifrs-full_CashFlowsFromUsedInInvestingActivities','ifrs_CashFlowsFromUsedInInvestingActivities')),
 'financing_cash_flow': (('CF',),('ifrs-full_CashFlowsFromUsedInFinancingActivities','ifrs_CashFlowsFromUsedInFinancingActivities')),
 # 보통주자본금(dart_IssuedCapitalOfCommonStock)만으로는 우선주 몫이 빠질 수 있어 전체 자본금만 쓴다.
 'issued_capital': (('BS',),('ifrs-full_IssuedCapital','ifrs_IssuedCapital')),
 'equity_attributable_to_owners': (('BS',),('ifrs-full_EquityAttributableToOwnersOfParent','ifrs_EquityAttributableToOwnersOfParent')),
 'noncontrolling_interests': (('BS',),('ifrs-full_NoncontrollingInterests','ifrs_NoncontrollingInterests')),
}
# 같은 원천 금액이 여러 구조로 중복 노출되면 단일 선택을 검증한다.
INTEREST_IDS={'ifrs-full_InterestExpense','ifrs_InterestExpense'}
# 단일 검증된 총차입금 산식은 기업마다 다르므로 자동합산을 하지 않는다.
BORROWING_KEYWORDS=('차입금','차입부채','사채','전환사채','리스부채')

def to_int(x: Any) -> int | None:
    if x is None or str(x).strip() in ('','-','—','N/A'):
        return None
    s=str(x).strip().replace(',','')
    try:
        if re.fullmatch(r'\([+-]?\d+\)',s): return -int(s[1:-1])
        if re.fullmatch(r'[+-]?\d+',s): return int(s)
    except ValueError: pass
    return None


def _candidates(rows: list[dict], field: str) -> list[dict]:
    sj, ids = BASE_ACCOUNTS[field]
    found=[]
    for account_id in ids:
        found.extend([r for r in rows if r.get('sj_div') in sj
                      and r.get('account_id') == account_id
                      and str(r.get('account_detail','-')).strip() in ('-','')])
        if found: break  # 우선순위가 명확한 XBRL 계정을 먼저 사용
    return found


def normalize_year(rows: list[dict], year: int, *, corp_code: str, corp_name: str,
                   fs_div='OFS', settlement_month=12, validated_overrides: dict | None=None) -> tuple[dict,dict]:
    """회계 결산 월은 DART 기업개황에서 확보; 별도 원천 CSV는 12월 결산 가정 후 경고."""
    settlement_month=int(settlement_month)
    if not 1<=settlement_month<=12: raise ValueError('결산월은 1~12')
    fiscal_start_year=year if settlement_month==12 else year-1
    start_month=1 if settlement_month==12 else settlement_month+1
    start=f'{fiscal_start_year}-{start_month:02d}-01'
    end_day=calendar.monthrange(year,settlement_month)[1]
    end=f'{year}-{settlement_month:02d}-{end_day:02d}'
    # 연간 말일 기준 결산, 단순 달력 계산; 기말 변경/비정상 회계기간은 별도 검증 필요.
    stmt={field: None for field in FIELDS}
    trace={}
    normalized_rows=[r for r in rows if str(r.get('bsns_year',year))==str(year)
                    and str(r.get('reprt_code','11011'))=='11011']
    for field in BASE_ACCOUNTS:
        matches=_candidates(normalized_rows,field)
        foreign=[r for r in matches if str(r.get('currency') or 'KRW').strip().upper() not in ('KRW','')]
        if foreign:
            trace[field]={'status':'currency_mismatch','value':None,'candidate_count':len(matches),
                          'currencies':sorted({str(r.get('currency')) for r in foreign})}
            continue
        by_value=defaultdict(list)
        for r in matches:
            value=to_int(r.get('thstrm_amount'))
            if value is not None: by_value[value].append(r)
        if len(by_value)==1:
            value=next(iter(by_value));stmt[field]=value
            example=by_value[value][0]
            prior={to_int(r.get('frmtrm_amount')) for r in by_value[value]} - {None}
            trace[field]={'status':'verified','value':value,'account_id':example.get('account_id'),
                          'account_nm':example.get('account_nm'),'source_row_count':len(matches),
                          'prior_value_in_report':next(iter(prior)) if len(prior)==1 else None}
        else:
            trace[field]={'status':'ambiguous' if len(by_value)>1 else 'missing',
                         'value':None,'candidate_count':len(matches),
                         'account_ids':sorted(set(str(r.get('account_id')) for r in matches))}
    if validated_overrides:
        raise ValueError('MVP에서는 외부 검증 주입을 지원하지 않습니다. 차입금과 이자비용은 분석 대상에서 제외했습니다.')
    rcept=sorted({str(r.get('rcept_no')) for r in normalized_rows if r.get('rcept_no')})
    audit={'business_year':year,'fs_div':fs_div,'period_start':start,'period_end':end,
           'period_source':'derived_from_current_acc_mt',
           'rcept_no':rcept[0] if len(rcept)==1 else (rcept or None),
           'fields':trace,
           'warnings':['세 가지 활동별 현금흐름은 DART 원본의 부호를 유지합니다.',
                       '누락 계정은 0으로 대체하지 않습니다.',
                       '회계기간 시작·종료일은 현재 결산월로 계산한 값입니다. 과거 결산기 변경은 전기금액 대조로만 간접 점검합니다.']}
    return {'fiscal_year':fiscal_start_year,'period_start':start,'period_end':end,'statement':stmt},audit
