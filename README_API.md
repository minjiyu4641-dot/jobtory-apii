# Jobtory v5.0 · OpenDART API로 공시 불러오기

어제의 v4.9 화면을 유지하면서 **기업 설명과 AI 질문의 사업보고서 근거**를 OpenDART API에서 가져오도록 바꿨습니다.

## 2026-11-10 회사명 비교 오류 수정

`(주)에스트래픽`, `에스트래픽(주)`, `㈜에스트래픽` 같은 법인 표기를 앱 기업명과 다르게 판단하던 오류를 수정했습니다. 기업명의 앞뒤에 붙은 법인 표기와 공백·전각 문자만 정규화합니다. 실제 기업명과 DART 고유번호가 다른 경우에는 계속 차단합니다.

v5.0을 이미 실행 중인 경우에는 `report_api.py` 하나만 기존 `app.py`가 있는 폴더에 덮어쓰고, 터미널에서 `Ctrl+C`로 종료한 뒤 `python -m streamlit run app.py`로 다시 실행하세요. `.env`와 기존 자료는 유지하며 패키지를 다시 설치할 필요는 없습니다.

## 달라진 흐름

기업·사업연도 선택 → 공시 목록 API → 최종 사업보고서 접수번호 선택 → 원문 ZIP/XML API → 본문·표 추출 → 질문 관련 근거 검색 → OpenAI API 답변

- 회사 보고서 DB의 `companies`, `filings`, `report_sections`는 조회하지 않습니다.
- 저장된 사업보고서 JSON은 AI 답변의 대체 자료로 사용하지 않습니다.
- API 오류·자료 없음·본문 추출 실패를 구분해서 표시합니다. 원문이 준비되어야 원문 확인·AI 답변 버튼이 활성화됩니다.
- 같은 사업연도의 최종 보고서만 선택합니다. 다른 기업·이전 사업연도의 보고서로 바꾸지 않습니다.
- 근거에는 기업명·사업연도·접수번호·원문 위치·DART 링크를 전달합니다. XML 위치를 PDF 쪽수처럼 표시하지 않습니다.
- API 목록과 원문은 같은 키·기업·연도에 대해 최대 1시간 재사용합니다. **공시 API 새로 불러오기**로 즉시 다시 요청할 수 있습니다.

지원 기업: 이삭엔지니어링, 롯데이노베이트, 인성정보, 링네트, 에스트래픽. 연도 목록은 최근 7년의 접수 기간에서 API로 확인한 사업보고서를 표시합니다. 보고서 원문 제목이 API 응답과 다르거나 본문을 하나로 식별할 수 없으면 답변에 사용하지 않습니다.

## 기존 폴더에 수정본 적용

검증 환경은 Python 3.12입니다. Conda를 사용하는 Windows PC에서 pandas 설치 중 소스 빌드 오류가 발생하면 아래 전용 환경을 만든 뒤 이어서 설치하세요.

```powershell
conda create -n jobtory_api python=3.12 pip -y
conda activate jobtory_api
```

1. 실행 중인 터미널에서 `Ctrl+C`를 누릅니다.
2. `jobtory_api_v50_update.zip`을 풀어 **기존 app.py가 있는 폴더**에 덮어씁니다.
3. 기존 `.env`, `.streamlit/secrets.toml`, 재무 분석 자료와 논문 자료는 유지합니다. `.env.example`은 참고용이며 `.env`를 덮지 않습니다.
4. 터미널에서 그 폴더로 이동한 뒤 실행합니다.

```powershell
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

상단에 **v5.0**이 표시되는지 확인하세요. 전체본 `jobtory_api_v50_full.zip`은 별도 폴더에 풀어 같은 명령으로 시작할 수 있습니다.

## 필요한 키

PC에서는 `.env`에 아래 두 키를 설정합니다. 키는 따옴표 뒤에 직접 입력하며 채팅이나 공개 코드에는 올리지 마세요.

```dotenv
DART_API_KEY=발급받은_OpenDART_API_키
OPENAI_API_KEY=발급받은_OpenAI_API_키
OPENAI_MODEL=gpt-4.1-mini
```

또는 앱 상단 **공시 API 연결**에 OpenDART 키를 입력하고, **⚙ → LLM 연결 설정**에 OpenAI 키를 입력할 수 있습니다. 화면 입력 키는 해당 세션에서만 사용합니다. OpenDART 키는 공시 조회용, OpenAI 키는 AI 답변 생성용입니다.

### Streamlit Cloud에 배포한 경우

GitHub 저장소에서 수정 ZIP의 파일을 같은 경로에 업로드합니다. 새 파일 **report_api.py**도 반드시 포함합니다. 앱의 진입 파일은 기존처럼 **app.py**입니다.

앱의 Settings → Secrets에 다음을 입력하고 저장합니다.

```toml
DART_API_KEY = "발급받은_OpenDART_API_키"
OPENAI_API_KEY = "발급받은_OpenAI_API_키"
OPENAI_MODEL = "gpt-4.1-mini"
```

기존 논문 DB를 계속 쓴다면 `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, 논문 API를 쓴다면 `NAL_API_KEY`도 유지하세요. 회사 DB용 `REPORT_SUPABASE_URL`, `REPORT_SUPABASE_SECRET_KEY`는 이 버전의 공시 조회에 사용하지 않습니다.

## 연결 확인 순서

1. 기업과 사업연도를 선택합니다.
2. **공시 API 연결됨 · 기업명 · 연도 · 본문 N개 항목**과 접수번호를 확인합니다.
3. 첫 확인에서는 **논문 함께 보기**를 끕니다.
4. 주요 사업 질문에서 **원문 먼저 확인**을 누르고 근거의 `OpenDART API` 표시와 DART 원문 링크를 확인합니다. 이 버튼은 OpenAI를 호출하지 않습니다.
5. **AI 답변 받기**를 눌러 공시 근거를 OpenAI API에 전달하고 답변을 받습니다.

## 유지한 자료와 기능

| 화면·기능 | 자료를 가져오는 곳 |
| --- | --- |
| 기업 개요·기업 설명 카드·AI 질문의 공시 근거 | OpenDART API 원문 |
| 재무 차트·조건 점검 | 기존 저장 재무 분석 JSON |
| 논문 | 기존 논문 DB·저장 자료와 국회도서관 API 설정 |
| AI 답변 생성 | OpenAI API |

**재무 차트는 기존 저장 분석 자료를 사용합니다.** 공시 답변의 기업 사실에는 해당 API 보고서만 사용하며, 저장 재무 분석과 직원 현황 저장본은 자동으로 섞지 않습니다. 논문 DB 설정도 기존 방식대로 유지합니다.

## 오류 표시

| 표시·증상 | 확인할 내용 |
| --- | --- |
| DART_API_KEY 필요 | 상단 입력창, `.env` 또는 Streamlit Secrets 설정 |
| 010·011·901 | OpenDART 인증키 등록·사용 가능·만료 상태 |
| 012 | OpenDART 인증키의 허용 IP 설정 |
| 020 | OpenDART 요청 한도. 잠시 후 다시 시도 |
| 013·014 또는 자료 없음 | 선택 기업·연도의 공시·원문 제공 여부 |
| 800 또는 시간 초과 | 서비스 점검·연결 상태. 새로 불러오기로 재시도 |
| 사업보고서 본문 식별·추출 실패 | 원문 XML 구조가 지원 범위와 다를 수 있음. DART 원문을 확인 |

현재 본문 추출은 일반적인 DART 사업보고서 XML의 제목·문단·표 구조를 대상으로 합니다. 표의 병합 셀·행과 앞뒤 단위를 보존해 전달하지만, 회사별 서식 차이로 추출이 부족할 수 있습니다. 열·단위가 불명확한 숫자는 단정하지 않도록 LLM에 안내합니다.

## 검증 범위

70개 자동 검사를 통과했습니다. 인증키를 사용한 실제 OpenDART/OpenAI 운영 호출은 이 작업 환경에서 실행하지 않았습니다. 모의 API 응답과 원문 ZIP/XML로 5개 기업의 법인 표기 변형·유사 기업명 거부·고유번호 불일치 차단·정정 보고서 선택·기업/연도 불일치 차단·표 분할·출처 전달·오류 시 키 비노출을 검사했습니다. Streamlit 실행 검사에서는 Secrets 키 인식, 원문 확인, 모의 LLM 답변, 새로고침, 회사 전환 후 이전 답변 초기화를 확인했습니다.

```powershell
python -m pip install pytest
python -m pytest -q tests
```

공식 API 문서:

- 공시 목록: https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001
- 공시 원문 ZIP/XML: https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019003
