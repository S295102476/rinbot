import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from plugins.admin_console import create_router
from plugins.agent_upgrade_console import create_upgrade_router
from plugins.console_auth import ConsoleAuth, MemorySessionStore, hash_password


class Memory:
    def __init__(self): self.changes=[]
    async def list_records(self,*args): return []
    async def privacy(self,*args): return {"version":3,"learning":True,"style_learning":True,"evaluation":True}
    async def set_privacy(self,*args,**kwargs):
        if kwargs['expected_version']!=3: raise ValueError('偏好已更新')
        self.changes.append(kwargs)
        return {'version':4}
    async def update_record(self,*args,**kwargs):
        if kwargs['expected_version']!=2: raise ValueError('记忆已更新')
        self.changes.append(kwargs)
        return {'version':3}


def test_memory_api_auth_csrf_versions_and_audit_identity():
    auth=ConsoleAuth('admin',hash_password('long-test-password'),MemorySessionStore())
    memory=Memory()
    app=FastAPI()
    app.include_router(create_router(auth=auth))
    app.include_router(create_upgrade_router(auth=auth,memory=memory))
    with TestClient(app) as client:
        assert client.get('/api/admin/agent/memory').status_code==401
        csrf=client.post('/api/admin/auth/login',json={'username':'admin','password':'long-test-password'},headers={'origin':'http://testserver'}).json()['csrf_token']
        headers={'origin':'http://testserver','x-csrf-token':csrf}
        assert client.patch('/api/admin/agent/memory/1',json={'version':2,'content':'correct'}).status_code==403
        assert client.patch('/api/admin/agent/memory/1',json={'version':1,'content':'correct'},headers=headers).status_code==409
        assert client.patch('/api/admin/agent/memory/1',json={'version':2},headers=headers).status_code==422
        assert client.patch('/api/admin/agent/memory/1',json={'version':2,'content':'correct'},headers=headers).status_code==200
        assert memory.changes[-1]['actor']=='admin'
        assert client.patch('/api/admin/agent/privacy/1',json={'version':2,'evaluation':False},headers=headers).status_code==409
        assert client.patch('/api/admin/agent/privacy/1',json={'version':3,'evaluation':False,'actor':'other'},headers=headers).status_code==422
        assert client.patch('/api/admin/agent/privacy/1',json={'version':3,'evaluation':False},headers=headers).status_code==200
        assert client.get('/api/admin/agent/privacy/-1').status_code==422
