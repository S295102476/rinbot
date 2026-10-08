"""Run legacy plugin tests with synthetic configuration, never personal files."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile

import nonebot
import yaml

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
_original_directory = Path.cwd()
_sandbox = tempfile.TemporaryDirectory(prefix='rinbot-tests-')
_root = Path(_sandbox.name)
_config = yaml.safe_load((SOURCE / 'config.example.yaml').read_text(encoding='utf-8'))
_config['allowed_groups'] = [987654321, 987650001]
_config['ai'].update(api_url='https://api.example.com/v1/chat/completions', api_key='test-api-key', model='test-model')
_config['ai']['antigravity'].update(enabled=True, api_url='https://ag.example.com/v1/chat/completions', api_key='test-ag-key', model='test-ag-model', enable_search=True)
_config['ai']['group_chat']['enabled_groups'] = [987654321]
_config['database'].update(host='127.0.0.1', port=1, password='test-database-password')
_config['redis'].update(host='127.0.0.1', port=1, password='test-redis-password')
_config['meme']['minio'].update(endpoint='127.0.0.1:1', access_key='test-access-key', secret_key='test-secret-key')
_config['agent'].update(active_groups=[987654321], provider_chain=['antigravity'])
_config['agent']['group'].update(owner_user_id=123456789, owner_priority_groups=[987654321],
    decision_recent_messages=300, decision_direct_recent_messages=300,
    decision_context_chars=30000, decision_direct_context_chars=30000, decision_backend_search=True)
_config['agent']['persona']['active_id'] = 'rin'
_config['agent']['dev'].update(enabled=True, admin_users=[123456789], workspace=str(SOURCE))
_config['dev_scope'].update(allowed_groups=[987654321])
_config['group_mode']['chat_only_groups'] = [987650001]
_config['features'].update(pixiv=True, development_agent=True)
_config['setu']['admin_users'] = [123456789]
(_root / 'config.yaml').write_text(yaml.safe_dump(_config, allow_unicode=True), encoding='utf-8')
shutil.copytree(SOURCE / 'persona', _root / 'persona')
for relative in ('data/dutyroster', 'data/game', 'data/gifts', 'data/deer', 'data/minigames/idioms'):
    if (SOURCE / relative).is_dir(): shutil.copytree(SOURCE / relative, _root / relative)
os.chdir(_root)
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver='~fastapi', log_level='ERROR', command_start={''})


def pytest_sessionfinish(session, exitstatus):
    os.chdir(_original_directory)
    _sandbox.cleanup()
