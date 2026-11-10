"""저장된 OpenDART 직원 현황을 페이지 없는 공시 근거로 연결합니다."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import route_question
from dart_service import EmployeeSnapshot, normalize_company_name

MAX_FILE_BYTES = 2 * 1024 * 1024
FIELDS = (
    ("fo_bbm", "사업부문"), ("sexdstn", "성별"), ("sm", "직원 합계"),
    ("rgllbr_co", "정규직 수(API 필드명)"), ("cnttk_co", "계약직 수(API 필드명)"),
    ("reform_bfe_emp_co_rgllbr", "개정 전 정규직 수"),
    ("reform_bfe_emp_co_cnttk", "개정 전 계약직 수"),
    ("avrg_cnwk_sdytrn", "평균근속연수"), ("jan_salary_am", "1인평균급여(API 원문값)"),
    ("fyer_salary_totamt", "연간급여총액(API 원문값)"), ("stlm_dt", "결산기준일"), ("rm", "비고"),
)


class DartDataError(ValueError):
    pass


@dataclass(frozen=True)
class DartEvidence:
    source_id: str
    corp_name: str
    fiscal_year: int
    section: str
    category: str
    source_file: str
    text: str
    char_start: int
    char_end: int
    source_url: str
    rcept_no: str
    fetched_at: str
    source_kind: str = "opendart"
    start_pdf_page: None = None
    end_pdf_page: None = None

    def to_dict(self) -> dict:
        return asdict(self)


def dart_data_signature(directory: Path, company: str, year: int) -> tuple:
    return (company, year, tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size)
            for p in sorted(directory.glob(f"employees_*_{year}_11011_*.json"))))


def _snapshot(data: object, company: str, year: int) -> EmployeeSnapshot | None:
    if not isinstance(data, dict):
        raise DartDataError("직원 현황 저장 형식이 올바르지 않습니다.")
    if normalize_company_name(str(data.get("corp_name", ""))) != normalize_company_name(company):
        return None
    if data.get("fiscal_year") != year:
        return None
    if data.get("schema_version") != 1 or data.get("provider") != "OpenDART" or data.get("endpoint") != "empSttus.json":
        raise DartDataError("OpenDART 직원 현황 형식을 확인할 수 없습니다. check_dart.py로 다시 조회하세요.")
    code, receipt = data.get("corp_code"), data.get("rcept_no")
    if not isinstance(code, str) or not re.fullmatch(r"\d{8}", code):
        raise DartDataError("회사 고유번호가 올바르지 않습니다.")
    if not isinstance(receipt, str) or not re.fullmatch(r"\d{14}", receipt) or data.get("report_code") != "11011":
        raise DartDataError("공시 접수번호·보고서 종류가 올바르지 않습니다.")
    try:
        fetched = datetime.fromisoformat(data.get("fetched_at", ""))
        if fetched.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError):
        raise DartDataError("자료 수집 시각을 확인할 수 없습니다.") from None
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) > 300:
        raise DartDataError("직원 현황 목록이 비어 있거나 허용 행 수를 넘었습니다.")
    for row in rows:
        if not isinstance(row, dict) or row.get("corp_code") != code or row.get("rcept_no") != receipt:
            raise DartDataError("저장 자료에 다른 법인·공시의 행이 섞여 있습니다.")
        if normalize_company_name(str(row.get("corp_name", ""))) != normalize_company_name(company):
            raise DartDataError("응답 행의 법인명이 선택 회사와 일치하지 않습니다.")
        if any(not isinstance(v, (str, type(None))) or (isinstance(v, str) and len(v) > 10000) for v in row.values()):
            raise DartDataError("직원 현황 값의 형식 또는 길이가 올바르지 않습니다.")
    return EmployeeSnapshot(code, data["corp_name"], year, "11011", receipt, data["fetched_at"], tuple(rows))


def load_employee_snapshot(directory: Path, company: str, year: int) -> tuple[EmployeeSnapshot | None, list[str]]:
    snapshots, warnings = [], []
    try:
        paths = sorted(directory.glob(f"employees_*_{year}_11011_*.json"))
    except OSError:
        return None, ["저장된 DART 자료 폴더를 읽지 못했습니다."]
    if len(paths) > 200:
        return None, ["해당 연도의 DART 저장 파일이 너무 많습니다. 회사별 최신 공시를 정리해 주세요."]
    for path in paths:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise DartDataError("파일이 허용 크기를 넘었습니다.")
            snapshot = _snapshot(json.loads(path.read_text(encoding="utf-8-sig")), company, year)
            if snapshot is not None:
                snapshots.append(snapshot)
        except (OSError, UnicodeError, json.JSONDecodeError, DartDataError) as exc:
            detail = str(exc) if isinstance(exc, DartDataError) else "파일을 읽거나 해석하지 못했습니다."
            warnings.append(path.name + ": " + detail)
    if len({s.corp_code for s in snapshots}) > 1:
        return None, warnings + ["같은 회사명에 고유번호가 다른 자료가 있습니다. 대상 법인을 확인한 뒤 저장 파일을 정리하세요."]
    if not snapshots:
        return None, warnings
    # 사업연도는 위에서 고정하고, 같은 법인의 가장 늦은 공시 접수번호만 사용합니다.
    return max(snapshots, key=lambda s: (s.rcept_no, datetime.fromisoformat(s.fetched_at))), warnings


def is_employee_question(question: str, preset_id: str | None = None) -> bool:
    if "employees" not in route_question(question, preset_id).categories:
        return False
    # API에 없는 제도·사용 현황에 직원 수·급여 표를 대신 붙이지 않습니다.
    unsupported = ("육아", "출산휴가", "유연근무", "시차출퇴근", "선택근무", "원격근무", "재택근무", "워라밸", "워라벨")
    staff = ("직원", "인원", "고용형태", "근속", "기간제", "급여", "연봉", "초봉", "상여", "성과급")
    return any(term in question for term in staff) and not any(term in question for term in unsupported)


def make_employee_evidence(snapshot: EmployeeSnapshot, company: str, max_chars: int = 6500) -> list[DartEvidence]:
    if normalize_company_name(snapshot.corp_name) != normalize_company_name(company):
        raise DartDataError("선택 회사와 DART 근거의 법인명이 다릅니다.")
    header = (f"OpenDART 직원 현황 · {snapshot.corp_name}\n사업연도: {snapshot.fiscal_year}\n"
              f"보고서 종류: 사업보고서(11011)\n공시 접수번호: {snapshot.rcept_no}\n"
              "아래 값은 API에서 반환한 문자열입니다. 급여 금액 단위와 포함 범위는 이 응답만으로 확정하지 않았습니다.\n")
    blocks = []
    for number, row in enumerate(snapshot.rows, 1):
        lines = [f"[직원 현황 행 {number}]"]
        for key, label in FIELDS:
            if key in row and row[key] not in (None, ""):
                lines.append(f"{label}: {row[key]}")
        blocks.append("\n".join(lines))
    # 표 일부를 조용히 잘라 전체 집계처럼 쓰지 않습니다.
    body = header + "\n\n" + "\n\n".join(blocks)
    if len(body) > max_chars:
        raise DartDataError("직원 현황이 답변 전달 분량을 넘었습니다. 전체 원문 공시에서 확인하세요.")
    seed = f"OpenDART|{snapshot.corp_code}|{snapshot.fiscal_year}|{snapshot.rcept_no}|{body}"
    return [DartEvidence("S" + hashlib.sha256(seed.encode()).hexdigest()[:10], company,
                         snapshot.fiscal_year, "직원 현황 (OpenDART)", "employees",
                         f"{snapshot.corp_name} {snapshot.fiscal_year} 사업보고서 · 직원 현황 (OpenDART)",
                         body, 0, len(body), snapshot.source_url, snapshot.rcept_no, snapshot.fetched_at)]


def connect_employee_evidence(evidence, snapshot: EmployeeSnapshot | None, company: str, year: int,
                             question: str, preset_id: str | None = None):
    if snapshot is None or not is_employee_question(question, preset_id):
        return list(evidence), []
    if snapshot.fiscal_year != year:
        raise DartDataError("선택 연도와 DART 근거의 사업연도가 다릅니다.")
    added = make_employee_evidence(snapshot, company)
    combined = list(evidence)
    if sum(len(e.text) for e in combined + added) > 18000:
        raise DartDataError("공시와 DART 근거의 합계가 전달 분량을 넘었습니다. 질문을 좁혀 주세요.")
    combined.extend(added)
    notes = [
        "DART 직원 현황은 선택 법인·사업연도·사업보고서 종류가 일치하는 저장 자료입니다. 공시 접수번호와 원문 링크를 확인하세요.",
        "DART API의 급여 값은 반환 문자열 그대로이며 금액 단위·상여/성과급 포함 범위를 이 응답만으로 확정하지 않았습니다. 원본 PDF에 명시된 단위·산정 주석이 있을 때만 그 출처의 급여를 설명하세요. API 문자열에 임의의 원·백만원 단위를 붙이거나 변환하지 마세요.",
        "원본 PDF와 DART의 수치·집계 대상·공시 버전이 다를 수 있습니다. 값이 다르면 출처별 차이를 밝히고 동일 기준으로 혼합하거나 새 합계·평균을 계산하지 마세요. PDF 주석을 다른 접수번호의 API 값에 임의 적용하지 마세요.",
        "DART 직원 현황에는 신입 초봉·실제 야근·유연근무·육아지원 사용 현황이 없습니다. 이 표로 해당 내용을 추정하지 마세요.",
    ]
    return combined, notes


def format_collected_at(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M (한국 시간)")
