"""OpenDART 기업 고유번호·직원 현황 조회. LLM과 독립된 연결 계층."""
from __future__ import annotations

import io
import json
import re
import tempfile
import unicodedata
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree as ET

BASE_URL = "https://opendart.fss.or.kr/api/"
MAX_RESPONSE = 32 * 1024 * 1024
MAX_CORP_XML = 100 * 1024 * 1024
REPORT_CODE = "11011"
STATUS_MESSAGES = {
    "010": "등록되지 않은 인증키입니다. OpenDART 인증키를 확인하세요.",
    "011": "사용할 수 없는 인증키입니다. OpenDART의 키 사용 상태를 확인하세요.",
    "012": "접근할 수 없는 IP입니다. OpenDART의 허용 IP 설정을 확인하세요.",
    "013": "해당 회사·사업연도·보고서 종류로 조회되는 자료가 없습니다.",
    "014": "요청한 파일이 존재하지 않습니다.",
    "020": "DART 요청 한도를 초과했습니다. 잠시 후 다시 실행하세요.",
    "021": "조회 가능한 회사 수를 초과했습니다.",
    "100": "요청 인자 형식을 확인하세요.",
    "101": "DART가 요청을 허용하지 않았습니다.",
    "800": "DART 시스템 점검 중입니다. 나중에 다시 실행하세요.",
    "900": "DART에서 처리 오류가 발생했습니다.",
    "901": "OpenDART 계정의 개인정보 보유기간이 만료되었습니다. 계정 상태를 확인하세요.",
}


class DartError(RuntimeError):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class DartNoData(DartError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def normalize_company_name(name: str) -> str:
    value = unicodedata.normalize("NFKC", name).strip()
    value = re.sub(r"\s+", "", value)
    for prefix in ("주식회사", "(주)"):
        if value.startswith(prefix):
            value = value[len(prefix):]
        if value.endswith(prefix):
            value = value[:-len(prefix)]
    return value.casefold()


def validate_key(api_key: str) -> str:
    key = api_key.strip()
    if not key:
        raise DartError("DART_API_KEY가 없습니다. app.py와 같은 폴더의 .env에 저장했는지 확인하세요.")
    if not re.fullmatch(r"[A-Za-z0-9]{40}", key):
        raise DartError("DART_API_KEY 형식을 확인하세요. OpenDART의 40자리 인증키가 필요합니다. 키 값은 출력하지 않았습니다.")
    return key


def _status(code: object) -> None:
    if not isinstance(code, str):
        raise DartError("DART 응답에 정상적인 상태 코드가 없습니다.")
    if code != "000":
        cls = DartNoData if code == "013" else DartError
        safe_code = code if re.fullmatch(r"\d{3}", code) else "unknown"
        raise cls(f"DART 코드 {safe_code}: " + STATUS_MESSAGES.get(code, "정상 처리되지 않은 응답입니다."), safe_code)


def _xml_root(content: bytes):
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise DartError("지원하지 않는 XML 형식입니다.")
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        raise DartError("DART 기업 목록 XML을 읽지 못했습니다.") from None


def _atomic_json(path: Path, data: object) -> Path:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        temporary.replace(path)
        return path
    except OSError:
        raise DartError("자료 파일을 저장하지 못했습니다. 폴더 쓰기 권한과 여유 공간을 확인하세요.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


@dataclass(frozen=True)
class EmployeeSnapshot:
    corp_code: str
    corp_name: str
    fiscal_year: int
    report_code: str
    rcept_no: str
    fetched_at: str
    rows: tuple[dict, ...]

    @property
    def source_url(self) -> str:
        return "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + self.rcept_no

    def to_dict(self) -> dict:
        result = asdict(self)
        result.update(schema_version=1, provider="OpenDART", endpoint="empSttus.json",
                      report_type="사업보고서", source_url=self.source_url,
                      value_policy="API 반환 문자열 그대로 보존. 급여 단위와 산정 기준은 원문 공시에서 확인.")
        return result


class DartClient:
    def __init__(self, api_key: str, timeout: float = 30.0, opener=None):
        self._key = validate_key(api_key)
        self.timeout = timeout
        self._opener = opener if opener is not None else build_opener(_NoRedirect())

    def _get(self, endpoint: str, params: dict | None = None) -> bytes:
        if endpoint not in ("corpCode.xml", "empSttus.json"):
            raise DartError("지원하지 않는 DART API입니다.")
        query = urlencode({**(params or {}), "crtfc_key": self._key})
        request = Request(BASE_URL + endpoint + "?" + query,
                          headers={"User-Agent": "CareerLens-DART-Connector/1.0", "Accept": "*/*"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = response.read(MAX_RESPONSE + 1)
        except HTTPError as exc:
            raise DartError(f"DART HTTP {exc.code}: 서버 연결 상태를 확인하세요. 인증키가 포함된 요청 주소는 출력하지 않았습니다.") from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise DartError("DART에 연결하지 못했거나 응답 시간이 초과되었습니다. 네트워크를 확인한 뒤 다시 실행하세요.") from None
        if len(payload) > MAX_RESPONSE:
            raise DartError("DART 응답 파일이 허용 크기를 초과했습니다.")
        return payload

    def get_companies(self) -> list[dict]:
        payload = self._get("corpCode.xml")
        if not zipfile.is_zipfile(io.BytesIO(payload)):
            # 이 API는 성공 시 ZIP, 인증 오류 등은 XML을 반환할 수 있습니다.
            root = _xml_root(payload)
            _status(root.findtext("status"))
            raise DartError("기업 고유번호 목록이 ZIP 파일로 반환되지 않았습니다.")
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                members = [m for m in archive.infolist() if Path(m.filename).name.casefold() == "corpcode.xml"]
                if len(members) != 1 or members[0].file_size > MAX_CORP_XML:
                    raise DartError("기업 고유번호 ZIP 파일의 내용을 확인할 수 없습니다.")
                with archive.open(members[0]) as stream:
                    xml = stream.read(MAX_CORP_XML + 1)
                if len(xml) > MAX_CORP_XML:
                    raise DartError("기업 고유번호 XML이 허용 크기를 초과했습니다.")
        except (zipfile.BadZipFile, RuntimeError, OSError):
            raise DartError("DART 기업 고유번호 ZIP 파일을 읽지 못했습니다.") from None
        root = _xml_root(xml)
        companies = []
        for item in root.findall("list"):
            row = {k: (item.findtext(k) or "").strip()
                   for k in ("corp_code", "corp_name", "stock_code", "modify_date")}
            if re.fullmatch(r"\d{8}", row["corp_code"]) and row["corp_name"]:
                companies.append(row)
        if not companies:
            raise DartError("DART 기업 목록에 사용할 수 있는 기업이 없습니다.")
        return companies

    def resolve_company(self, company_name: str, companies: list[dict]) -> dict:
        target = normalize_company_name(company_name)
        if not target:
            raise DartError("조회할 회사명을 입력하세요.")
        matches = {r["corp_code"]: r for r in companies
                   if normalize_company_name(r.get("corp_name", "")) == target}
        if len(matches) == 1:
            return next(iter(matches.values()))
        if not matches:
            raise DartError("회사명과 일치하는 법인을 찾지 못했습니다. DART에 등록된 정확한 법인명으로 다시 실행하세요.")
        codes = ", ".join(sorted(matches))
        raise DartError(f"동일한 회사명의 법인이 여러 개입니다(고유번호: {codes}). --corp-code로 대상을 지정하세요.")

    def get_employees(self, corp_code: str, year: int, expected_name: str = "") -> EmployeeSnapshot:
        if not isinstance(corp_code, str) or not re.fullmatch(r"\d{8}", corp_code):
            raise DartError("회사 고유번호는 8자리 문자열이어야 합니다. 종목코드 6자리와 다릅니다.")
        if isinstance(year, bool) or not isinstance(year, int) or not 2015 <= year <= datetime.now().year:
            raise DartError("사업연도는 2015년부터 현재 연도 사이로 지정하세요.")
        payload = self._get("empSttus.json", {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": REPORT_CODE})
        try:
            data = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError):
            raise DartError("DART 직원 현황 응답이 정상적인 JSON이 아닙니다.") from None
        if not isinstance(data, dict):
            raise DartError("DART 직원 현황 응답 형식을 확인할 수 없습니다.")
        _status(data.get("status"))
        rows = data.get("list")
        if not isinstance(rows, list) or not rows:
            raise DartNoData("정상 응답을 받았지만 직원 현황 목록이 비어 있습니다.", "013")
        valid = []
        receipts, names = set(), set()
        for row in rows:
            if not isinstance(row, dict) or row.get("corp_code") != corp_code:
                raise DartError("선택한 회사와 응답의 회사 고유번호가 일치하지 않습니다. 자료를 저장하지 않았습니다.")
            if not isinstance(row.get("corp_name"), str) or not row["corp_name"].strip():
                raise DartError("응답의 회사명을 확인할 수 없습니다.")
            if expected_name and normalize_company_name(row["corp_name"]) != normalize_company_name(expected_name):
                raise DartError("선택한 회사명과 응답의 법인명이 다릅니다. 회사명·고유번호를 확인하세요.")
            if not isinstance(row.get("rcept_no"), str) or not re.fullmatch(r"\d{14}", row["rcept_no"]):
                raise DartError("원문 공시로 연결할 접수번호가 올바르지 않습니다.")
            if any(not isinstance(value, (str, type(None))) for value in row.values()):
                raise DartError("API 값의 형식이 예상과 다릅니다. 수치를 임의 변환하지 않고 중단했습니다.")
            # 인증키가 응답에 우연히 포함되더라도 저장·출력하지 않습니다.
            cleaned = {k: v.replace(self._key, "[REDACTED]") if isinstance(v, str) else v for k, v in row.items()}
            valid.append(cleaned)
            receipts.add(row["rcept_no"])
            names.add(row["corp_name"].strip())
        if len(receipts) != 1 or len(names) != 1:
            raise DartError("직원 현황 응답에 서로 다른 공시·법인이 섞여 있습니다. 자료를 저장하지 않았습니다.")
        return EmployeeSnapshot(corp_code, names.pop(), year, REPORT_CODE, receipts.pop(),
                                datetime.now(timezone.utc).isoformat(), tuple(valid))


def cached_companies(client: DartClient, path: Path, refresh: bool = False) -> list[dict]:
    if not refresh and path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(data["fetched_at"])
            age = (datetime.now(timezone.utc) - fetched).total_seconds()
            rows = data["companies"]
            if 0 <= age < 7 * 86400 and isinstance(rows, list) and rows and all(
                isinstance(r, dict) and re.fullmatch(r"\d{8}", r.get("corp_code", "")) and
                isinstance(r.get("corp_name"), str) and r["corp_name"].strip() for r in rows
            ):
                return rows
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            pass
    rows = client.get_companies()
    _atomic_json(path, {"fetched_at": datetime.now(timezone.utc).isoformat(), "companies": rows})
    return rows


def save_employees(snapshot: EmployeeSnapshot, directory: Path) -> Path:
    filename = f"employees_{snapshot.corp_code}_{snapshot.fiscal_year}_{snapshot.report_code}_{snapshot.rcept_no}.json"
    return _atomic_json(directory / filename, snapshot.to_dict())
