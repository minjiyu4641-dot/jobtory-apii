"""Resolve company-report credentials independently from the paper database."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


class DatabaseSettingsError(ValueError):
    pass


@dataclass(frozen=True)
class ReportConnection:
    url: str = field(repr=False)
    secret_key: str = field(repr=False)
    explicit: bool = False

    @property
    def connection_id(self) -> str:
        return hashlib.sha256((self.url + '|' + self.secret_key).encode()).hexdigest()

    @property
    def label(self) -> str:
        return '회사 DB 전용 설정' if self.explicit else '논문 DB와 공용 설정'


def resolve_report_connection(get_setting) -> ReportConnection:
    """A partial explicit pair must never be mixed with credentials from another DB."""
    url = str(get_setting('REPORT_SUPABASE_URL') or '').strip().rstrip('/')
    key = str(get_setting('REPORT_SUPABASE_SECRET_KEY') or '').strip()
    explicit = bool(url or key)
    if explicit and not (url and key):
        raise DatabaseSettingsError('회사 DB 전용 설정에는 REPORT_SUPABASE_URL과 '
                                    'REPORT_SUPABASE_SECRET_KEY를 모두 입력해 주세요. 논문 DB 값과 섞어 사용하지 않습니다.')
    if not explicit:
        url = str(get_setting('SUPABASE_URL') or '').strip().rstrip('/')
        key = str(get_setting('SUPABASE_SECRET_KEY') or '').strip()
    if key.startswith('sb_publishable_'):
        variable = 'REPORT_SUPABASE_SECRET_KEY' if explicit else 'SUPABASE_SECRET_KEY'
        raise DatabaseSettingsError(f'{variable}에 Publishable key가 설정되어 있습니다. '
                                    '회사 DB의 서버용 Secret key 또는 기존 service_role 키를 사용해 주세요.')
    return ReportConnection(url, key, explicit)
