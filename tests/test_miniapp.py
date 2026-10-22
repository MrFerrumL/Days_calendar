import unittest,json,hmac,hashlib,time
from urllib.parse import urlencode
from dataclasses import replace
from datetime import date
from campus_bot.miniapp import verify_init_data,App
from campus_bot.storage import Store
from test_bot import config,Fetch,NOW
from campus_bot.schedule import Schedule

def signed(owner=42,age=0):
    fields={'auth_date':str(int(time.time())-age),'user':json.dumps({'id':owner})}
    secret=hmac.new(b'WebAppData',b'123:fake',hashlib.sha256).digest()
    fields['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(fields.items())).encode(),hashlib.sha256).hexdigest()
    return urlencode(fields)

class AuthTests(unittest.TestCase):
    def test_owner(self):
        verify_init_data(signed(),'123:fake',42)
    def test_tamper(self):
        with self.assertRaises(PermissionError):verify_init_data(signed()+'x','123:fake',42)
    def test_other_user(self):
        with self.assertRaises(PermissionError):verify_init_data(signed(99),'123:fake',42)
    def test_expired(self):
        with self.assertRaises(PermissionError):verify_init_data(signed(age=4000),'123:fake',42)
    def test_empty(self):
        with self.assertRaises(PermissionError):verify_init_data('','123:fake',42)
    def test_duplicate_fields(self):
        with self.assertRaises(PermissionError):verify_init_data(signed()+'&user=x','123:fake',42)

class EditingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s=Store(':memory:');self.app=App(config());self.schedule=Schedule(self.s,config(),Fetch())
    async def asyncTearDown(self):self.s.close()
    async def test_checkbox_reversible(self):
        self.app.mutate(self.s,'check',{'key':'personal:thu14|2026-10-22','done':True})
        self.assertTrue(self.s.checked('personal:thu14|2026-10-22'))
        self.app.mutate(self.s,'check',{'key':'personal:thu14|2026-10-22','done':False})
        self.assertFalse(self.s.checked('personal:thu14|2026-10-22'))
    async def test_override_survives_source_refresh_and_reset(self):
        key='personal:thu14|2026-10-22'
        self.app.mutate(self.s,'override',dict(key=key,title='Перенос',start='2026-10-22T15:00',end='2026-10-22T17:00'))
        events,_,_=await self.schedule.day(NOW.date(),force=True)
        changed=next(e for e in events if e.uid=='personal:thu14')
        self.assertEqual(changed.start.hour,15)
        self.assertEqual(changed.title,'Перенос')
        self.app.mutate(self.s,'reset-override',{'key':key})
        events,_,_=await self.schedule.day(NOW.date())
        self.assertEqual(next(e for e in events if e.uid=='personal:thu14').start.hour,14)
    async def test_task_complete_and_undo(self):
        self.app.mutate(self.s,'task',dict(title='Задача',start='2026-11-02T12:00'))
        uid=self.s.items('task')[0]['id']
        self.app.mutate(self.s,'task-check',dict(id=uid,done=True))
        self.assertEqual(self.s.items('task'),[])
        self.app.mutate(self.s,'task-check',dict(id=uid,done=False))
        self.assertEqual(len(self.s.items('task')),1)
    async def test_bad_duration_and_cross_day_override_rejected(self):
        with self.assertRaises(ValueError):self.app.mutate(self.s,'event',dict(title='bad',start='2026-11-02T12:00',duration=-3))
        with self.assertRaises(ValueError):self.app.mutate(self.s,'override',dict(key='uni:1|2026-10-22',title='bad',start='2026-10-23T12:00',end='2026-10-23T14:00'))
    async def test_rule_update(self):
        r=self.s.rules()[0];r['title']='Новое имя';self.app.mutate(self.s,'rule',r)
        self.assertEqual(next(x for x in self.s.rules() if x['id']==r['id'])['title'],'Новое имя')
