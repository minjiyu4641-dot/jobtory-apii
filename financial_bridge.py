"""Read saved quantitative reports and expose the same values to charts and answers.

No DART, ECOS or model requests occur in this module. Financial evidence has
its own source kind; it never claims to have a PDF page.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from analysis.financial_ratios import RATIO_LABELS
from core import route_question
from dart_service import normalize_company_name


class FinancialDataError(ValueError):
    pass


FIELDS = {
    'revenue': '매출액', 'operating_income': '영업이익', 'net_income': '당기순이익',
    'total_assets': '자산총계', 'total_liabilities': '부채총계', 'total_equity': '자본총계',
    'current_assets': '유동자산', 'current_liabilities': '유동부채',
    'operating_cash_flow': '영업활동현금흐름', 'investing_cash_flow': '투자활동현금흐름',
    'financing_cash_flow': '재무활동현금흐름', 'property_plant_equipment': '유형자산',
    'issued_capital': '자본금', 'equity_attributable_to_owners': '지배기업 소유주지분',
    'noncontrolling_interests': '비지배지분',
}
FINANCIAL_TERMS = (
    '매출', '영업이익', '영업손실', '순이익', '순손실', '재무', '부채', '이익률',
    '수익성', '유동비율', '자본잠식', '현금', '자산', '실적', '성장률', '자본비율',
    '업종 비교', '업종비교', '동종업계', '5년', '5개년', 'roa',
)


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, UnicodeError) as exc:
        raise FinancialDataError(f'저장 재무 자료를 읽지 못했습니다: {path.name}') from exc
    if not isinstance(value, dict):
        raise FinancialDataError(f'저장 재무 자료의 형식이 올바르지 않습니다: {path.name}')
    return value


def financial_signature(root: Path) -> tuple:
    folder = Path(root) / 'data'
    paths = list((folder / 'analysis').glob('*/*/quantitative_report.json'))
    paths += list((folder / 'company_info').glob('*.json'))
    paths += list((folder / 'normalized').glob('*/*/audit.json'))
    return tuple((str(p.relative_to(folder)), p.stat().st_size, p.stat().st_mtime_ns)
                 for p in sorted(paths))


@dataclass(frozen=True)
class FinancialBundle:
    corp_code: str
    fiscal_year: int
    report: dict
    data: dict
    meta: dict
    analysis: dict

    def report_json(self) -> str:
        return json.dumps(self.report, ensure_ascii=False, indent=2, allow_nan=False)


def load_financial_bundle(root: Path, company: str, year: int) -> FinancialBundle | None:
    """Exact normalized-name match -> unique corp code -> exact report year.

    Matching names are only an adapter for existing section JSONs, which have
    no corp_code. Ambiguous names are rejected instead of selecting a company.
    """
    folder = Path(root) / 'data'
    matches = []
    for path in sorted((folder / 'analysis').glob('*/*/quantitative_report.json')):
        payload = _read(path)
        identity = payload.get('company') or {}
        if normalize_company_name(str(identity.get('corp_name', ''))) == normalize_company_name(company):
            matches.append((path, payload))
    if not matches:
        return None
    codes = {p['company'].get('corp_code') for _, p in matches}
    if len(codes) != 1:
        raise FinancialDataError('같은 이름에 기업코드가 다른 재무 자료가 있습니다. 대상 법인을 확인하세요.')
    candidates = [(path, p) for path, p in matches
                  if p.get('financials') and str(year) in p['financials']
                  and max(int(k) for k in p['financials']) == year]
    if not candidates:
        raise FinancialDataError(f'{company}의 {year} 사업연도에 맞는 저장 재무 분석이 없습니다. 다른 연도 결과를 대신 사용하지 않았습니다.')
    candidates.sort(key=lambda row: (row[1].get('basis', {}).get('fs_div') != 'CFS', str(row[0])))
    path, report = candidates[0]
    code = report['company'].get('corp_code')
    basis = report.get('basis') or {}
    fs_div = basis.get('fs_div')
    if (report.get('schema_version') != 'quant-report/1.2'
            or not isinstance(code, str) or not re.fullmatch(r'\d{8}', code)
            or path.parent.parent.name != code or path.parent.name != fs_div
            or fs_div not in ('CFS', 'OFS') or basis.get('currency') != 'KRW'
            or basis.get('report_code') != '11011'):
        raise FinancialDataError('저장 분석의 스키마·기업코드·연결/별도·통화 기준을 확인할 수 없습니다.')
    for key in ('ratios', 'rules', 'trends'):
        part = report.get(key) or {}
        if part.get('corp_code') != code or part.get('fs_div') != fs_div:
            raise FinancialDataError('재무 분석에 다른 기업 또는 연결/별도 기준의 결과가 섞여 있습니다.')
    if set(report['ratios'].get('years', {})) != set(report['financials']):
        raise FinancialDataError('재무 금액과 계산된 지표의 연도가 일치하지 않습니다.')
    data = {
        **report['company'], 'fs_div': fs_div, 'report_code': basis['report_code'],
        'currency': 'KRW', 'amount_unit': 'KRW', 'financials': report['financials'],
    }
    audit_path = folder / 'normalized' / code / fs_div / 'audit.json'
    audit = _read(audit_path) if audit_path.exists() else {}
    if audit and (audit.get('corp_code') != code or audit.get('fs_div') != fs_div):
        raise FinancialDataError('계정 감사 기록과 저장 재무 분석의 기업·기준이 다릅니다.')
    info_path = folder / 'company_info' / f'{code}.json'
    info = _read(info_path) if info_path.exists() else {}
    quality = report.get('data_quality') or {}
    meta = {
        **audit, 'corp_code': code, 'stock_code': report['company'].get('stock_code'),
        'settlement_month': basis.get('settlement_month') or 12,
        'business_year_window': audit.get('business_year_window') or list(range(year - 4, year + 1)),
        'year_status': report.get('sources') or {},
        'accounts_audit': audit.get('accounts_audit') or {},
        'consistency_checks': quality.get('consistency_checks') or [],
        'collected_at': quality.get('collected_at'), 'company_info': info,
    }
    analysis = {k: report[k] for k in ('ratios', 'rules', 'trends', 'comparison', 'benchmark_files') if k in report}
    analysis['industry_auto'] = report.get('industry_auto') or {}
    return FinancialBundle(code, year, report, data, meta, analysis)


@dataclass(frozen=True)
class FinancialEvidence:
    source_id: str
    corp_name: str
    fiscal_year: int
    period_year: int
    corp_code: str
    fs_div: str
    section: str
    category: str
    source_file: str
    text: str
    facts: dict
    source_url: str
    rcept_no: str
    fetched_at: str
    benchmark_url: str = ''
    source_kind: str = 'financial_analysis'
    start_pdf_page: None = None
    end_pdf_page: None = None
    char_start: int = 0
    char_end: int = 0

    def to_dict(self) -> dict:
        result = asdict(self)
        # text already contains the complete facts JSON; avoid sending it twice.
        result.pop('facts')
        return result


def _evidence(bundle: FinancialBundle, company: str, year: int, section: str,
              facts: dict, fs_div: str, receipt: str | None, benchmark_url: str = '') -> FinancialEvidence:
    receipt = receipt if isinstance(receipt, str) and re.fullmatch(r'\d{14}', receipt) else ''
    text = json.dumps(facts, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    seed = f'{bundle.corp_code}|{bundle.fiscal_year}|{year}|{fs_div}|{section}|{receipt}|{text}'
    identifier = 'S' + hashlib.sha256(seed.encode()).hexdigest()[:10]
    return FinancialEvidence(
        identifier, company, bundle.fiscal_year, year, bundle.corp_code, fs_div,
        section, 'financial_statements', f'{company} · {section}', text, facts,
        'https://dart.fss.or.kr/dsaf001/main.do?rcpNo=' + receipt if receipt else '',
        receipt, bundle.report.get('generated_at', ''), benchmark_url=benchmark_url, char_end=len(text),
    )


def make_financial_evidence(bundle: FinancialBundle, company: str, question: str) -> list[FinancialEvidence]:
    if normalize_company_name(bundle.report['company']['corp_name']) != normalize_company_name(company):
        raise FinancialDataError('선택한 기업과 재무 분석의 법인명이 다릅니다.')
    report = bundle.report
    fs_div = report['basis']['fs_div']
    output = []
    for key, annual in sorted(report['financials'].items()):
        year = int(key)
        statement = {FIELDS.get(k, k): v for k, v in annual['statement'].items()}
        ratios = {}
        for metric, value in report['ratios']['years'][key]['ratios'].items():
            # Preserve null reasons and profit/loss transitions, not just numbers.
            ratios[RATIO_LABELS.get(metric, metric)] = {
                k: v for k, v in value.items() if k in ('value', 'unit', 'reason', 'change_type')
            }
        facts = {
            '기간': [annual['period_start'], annual['period_end']], '금액단위': 'KRW(원)',
            '재무제표기준': fs_div, '재무금액': statement, '계산지표': ratios,
        }
        source = report.get('sources', {}).get(key) or {}
        output.append(_evidence(bundle, company, year, f'{year}년 재무 수치와 계산 지표',
                                facts, fs_div, source.get('rcept_no')))
    if any(word in question for word in ('업종', '동종', '중앙값', '부채비율', '이익률')):
        comparison = report.get('comparison') or {}
        metrics = comparison.get('metrics') or {}
        if metrics:
            basis_data = comparison.get('basis_data') or {}
            source = (basis_data.get('sources') or report.get('sources') or {}).get(str(bundle.fiscal_year)) or {}
            selected = {m: {k: item.get(k) for k in (
                'label', 'value', 'unit', 'median', 'quantiles', 'benchmark_year', 'industry_name',
                'benchmark_company_size', 'status', 'reason', 'quantile_coverage', 'fallback_used',
            )} for m, item in metrics.items()}
            facts = {'용도': '업종 참고 비교', '재무제표기준': comparison.get('basis_fs_div', fs_div),
                     '금액단위': 'KRW(원)', '지표단위': '%', '업종비교': selected,
                     '주의': '기업 순위·등급이나 고용 안정성 판정이 아닙니다.'}
            output.append(_evidence(bundle, company, bundle.fiscal_year, '업종 비교 (계산 기준 별도 표시)',
                facts, comparison.get('basis_fs_div', fs_div), source.get('rcept_no'), 'https://ecos.bok.or.kr/'))
    if any(word in question for word in ('잠식', '주의', '위험', '안정', '점검', '현금')):
        source = report.get('sources', {}).get(str(bundle.fiscal_year)) or {}
        facts = {'재무제표기준': fs_div, '재무조건점검': report['rules']['years'].get(str(bundle.fiscal_year)),
                 '주의': '확인 불가는 안전이나 해당 없음이 아닙니다. 고용 안정성·부도 확률을 추정하지 않습니다.'}
        output.append(_evidence(bundle, company, bundle.fiscal_year, '재무 조건 점검', facts, fs_div, source.get('rcept_no')))
    return output


def connect_financial_evidence(evidence, bundle: FinancialBundle | None, company: str,
                               year: int, question: str, preset_id: str | None = None):
    route = route_question(question, preset_id)
    needed = route.id in ('performance', 'stability') or any(t in question.casefold() for t in FINANCIAL_TERMS)
    if not needed or bundle is None:
        return list(evidence), []
    if bundle.fiscal_year != year:
        raise FinancialDataError('선택한 사업연도와 재무 분석 기준연도가 다릅니다.')
    financial = make_financial_evidence(bundle, company, question)
    if sum(len(e.text) for e in financial) > 14000:
        raise FinancialDataError('저장 재무 분석이 전달 분량을 넘었습니다. 질문 범위를 좁혀 주세요.')
    remaining = 18000 - sum(len(e.text) for e in financial)
    kept = []
    for item in evidence:
        if len(item.text) <= remaining:
            kept.append(item)
            remaining -= len(item.text)
    notes = [
        '재무 분석 근거는 재무 대시보드와 같은 저장 JSON입니다. 연도·연결/별도 기준·접수번호를 함께 인용하세요.',
        '금액은 KRW(원), 비율은 %입니다. 사전 계산 결과를 설명하고 비율·증감률·업종 순위를 새로 계산하지 마세요.',
        '각 연도 수치는 해당 연도 보고서의 당기 값입니다. 최신 공시의 재작성·재분류 과거 값과 다를 수 있습니다.',
        '수치의 변화 원인은 별도 공시 본문에서 회사가 직접 설명한 근거가 있을 때만 연결하세요.',
    ]
    if len(kept) < len(evidence):
        notes.append('전달 분량 제한으로 일부 공시 발췌문을 제외했습니다. 전달되지 않은 원인·내용을 추정하지 마세요.')
    if bundle.meta.get('consistency_checks'):
        notes.append('다음 해 보고서에서 과거 수치가 달라진 항목이 있습니다. 재무 대시보드의 자료 출처 탭을 함께 확인하세요.')
    return financial + kept, notes
