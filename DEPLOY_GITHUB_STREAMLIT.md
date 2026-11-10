# GitHub → Streamlit Cloud 적용

1. 수정 ZIP을 풀고, 기존 GitHub 저장소에 같은 경로로 파일을 덮어씁니다. 새 파일 report_api.py를 포함하세요.
2. 앱의 진입 파일은 app.py입니다. requirements.txt도 함께 갱신합니다.
3. Streamlit Cloud 앱 Settings → Secrets에 DART_API_KEY와 OPENAI_API_KEY를 입력합니다.

```toml
DART_API_KEY = "발급받은_OpenDART_API_키"
OPENAI_API_KEY = "발급받은_OpenAI_API_키"
OPENAI_MODEL = "gpt-4.1-mini"
```

4. 기존 논문 DB·API 키는 계속 사용할 경우 유지합니다. 회사 DB용 REPORT_SUPABASE_URL / REPORT_SUPABASE_SECRET_KEY는 공시 조회에 사용하지 않습니다.
5. 앱이 다시 시작하면 v5.0, 공시 API 연결됨, 공시 접수번호를 확인합니다.

전체 안내는 README_API.md에 있습니다. 실제 배포는 이 파일 제공만으로 변경되지 않습니다.
