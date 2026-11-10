import json
from pathlib import Path
from types import SimpleNamespace

import streamlit as st
from streamlit.testing.v1 import AppTest

from test_report_api import KEY, RECEIPT, Response, filing, xml_report, zipped
from report_api import API_COMPANIES


ROOT = Path(__file__).resolve().parents[1]


def clean_environment(monkeypatch):
    for name in ('DART_API_KEY', 'OPENAI_API_KEY', 'NAL_API_KEY', 'SUPABASE_URL', 'SUPABASE_SECRET_KEY'):
        monkeypatch.delenv(name, raising=False)
    st.cache_data.clear()
    st.cache_resource.clear()


def test_missing_key_disables_answers_and_does_not_use_existing_json(monkeypatch):
    clean_environment(monkeypatch)
    at = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=60)
    at.run()
    assert not at.exception
    assert next(b.disabled for b in at.button if b.key == 'ask')
    assert next(b.disabled for b in at.button if b.key == 'preview')
    assert any('DART_API_KEY' in w.value for w in at.warning)


def test_secrets_api_preview_llm_refresh_and_company_switch(monkeypatch):
    clean_environment(monkeypatch)
    calls, llm_payloads = [], []
    def get(url, *, params, **kwargs):
        calls.append((url, params))
        if url.endswith('list.json'):
            company = next(c for c, code in API_COMPANIES.items() if code == params['corp_code'])
            get.company = company
            return Response({'status': '000', 'total_page': 1, 'list': [filing(company)]})
        return Response(content=zipped(xml_report(get.company)))
    monkeypatch.setattr('report_api.requests.get', get)
    def parse(**kwargs):
        payload = json.loads(kwargs['input'])
        llm_payloads.append(payload)
        claim = {'text': '회사는 공장 자동화 사업을 한다고 설명합니다.',
                 'source_ids': [payload['evidence'][0]['source_id']]}
        return SimpleNamespace(status='completed', output_parsed={
            'status': 'answered', 'summary': claim, 'facts': [claim], 'interpretations': [],
            'follow_up_questions': [], 'limitations': []})
    monkeypatch.setattr('answer_service.OpenAI', lambda **kwargs:
        SimpleNamespace(responses=SimpleNamespace(parse=parse), close=lambda: None))
    at = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=60)
    at.secrets['DART_API_KEY'] = KEY
    at.secrets['OPENAI_API_KEY'] = 'test-openai-key'
    at.run()
    assert not at.exception
    assert any('공시 API 연결됨' in s.value for s in at.success)
    assert len(calls) == 2
    next(t for t in at.toggle if t.key == 'include_papers').set_value(False).run()
    next(b for b in at.button if b.key == 'preview').click().run()
    assert not at.exception
    assert len(calls) == 2  # Reruns reuse this key/company/year's API result.
    assert any('OpenDART API' in c.value for c in at.caption)
    next(b for b in at.button if b.key == 'ask').click().run()
    assert not at.exception
    assert llm_payloads and all(e['source_kind'] == 'opendart_report' for e in llm_payloads[-1]['evidence'])
    assert all(e['rcept_no'] == RECEIPT for e in llm_payloads[-1]['evidence'])
    next(b for b in at.button if b.key == 'report_api_reload').click().run()
    assert not at.exception and len(calls) == 4
    next(s for s in at.selectbox if s.key == 'company').select('인성정보').run()
    assert not at.exception
    assert any('인성정보' in s.value for s in at.success)
    assert at.session_state['report_source_revision'][1] == '인성정보'
    assert 'answer_result' not in at.session_state
    assert calls[-2][1]['corp_code'] == API_COMPANIES['인성정보']
