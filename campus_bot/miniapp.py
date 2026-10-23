"""Private Telegram Mini App sharing the bot's SQLite database."""
import asyncio
from dataclasses import asdict
from datetime import date, datetime, timedelta
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import parse_qsl, urlsplit, parse_qs
from uuid import uuid4
from . import hackathons
from .mail import masked
from .models import clock
from .schedule import Schedule
from .storage import Store
from .weather import Weather

STATIC = Path(__file__).resolve().parent / 'web'


def verify_init_data(raw, token, owner_id, now=None):
    if not raw or len(raw) > 12000:
        raise PermissionError('Откройте приложение через меню Telegram-бота.')
    pairs = parse_qsl(raw, keep_blank_values=True)
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise PermissionError('Повторяющиеся поля')
    received = fields.pop('hash', '')
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, '\n'.join(f'{k}={v}' for k,v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received, signature):
        raise PermissionError('Неверная подпись Telegram')
    age = (time.time() if now is None else now) - int(fields.get('auth_date','0'))
    if not -30 <= age <= 3600:
        raise PermissionError('Сессия истекла. Откройте приложение заново.')
    if not owner_id or json.loads(fields.get('user','{}')).get('id') != owner_id:
        raise PermissionError('Это личное приложение владельца.')


def title(value):
    if not isinstance(value,str) or not 1 <= len(value.strip()) <= 300:
        raise ValueError('Название: 1–300 символов')
    return value.strip()


def moment(value,tz):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)


class App:
    def __init__(self,config):
        self.config = config

    async def state(self,store,first,last):
        schedule = Schedule(store,self.config)
        events,notes = {},[]
        day = first
        while day <= last:
            rows,note,_ = await schedule.day(day)
            if note and note not in notes:
                notes.append(note)
            for e in rows:
                key = e.uid+'|'+e.start.date().isoformat()
                row = asdict(e)
                row.update(start=e.start.isoformat(),end=e.end.isoformat(),key=key,done=store.checked(key))
                events[key] = row
            day += timedelta(days=1)
        now = datetime.now(self.config.timezone)
        dropped = {p['id'] for p in store.projects() if p.get('status')=='dropped'}
        return dict(events=list(events.values()),tasks=[t for t in store.items('task',include_done=True) if t['project'] not in dropped],
                    rules=store.rules(),group=store.get('group_name'),notes=notes,weather=await Weather(store,self.config.timezone).today(),
                    settings={k:store.get(k) for k in ('digest_times','weather_times','notifications')},
                    hackathons=hackathons.overview(store,now),clashes=hackathons.clash_lines(store,now),
                    statuses=hackathons.STATUSES,kinds=hackathons.KINDS,mail=self.mail(store))

    def mail(self,store):
        configured = bool(self.config.mail_user and self.config.mail_password)
        return dict(configured=configured,enabled=store.get('mail_enabled',True),checked=store.get('mail_checked'),
                    address=masked(self.config.mail_user) if configured else '',error=store.get('mail_error'))

    def project(self,store,body):
        uid = body.get('id') or 'h-'+uuid4().hex[:6]
        if not hackathons.PROJECT_ID.fullmatch(uid) or body.get('status') not in hackathons.STATUSES or body.get('kind') not in hackathons.KINDS:
            raise ValueError('Проверьте статус и тип')
        links = body.get('links',[])
        if not isinstance(links,list) or len(links)>8 or any(not isinstance(l.get('url'),str) or len(l['url'])>500
                                                             or not l['url'].startswith(('https://','http://')) for l in links):
            raise ValueError('До восьми ссылок http(s)')
        text = lambda name,limit: str(body.get(name) or '').strip()[:limit]
        store.put_project(dict(id=uid,title=title(body['title']),kind=body['kind'],status=body['status'],organizer=text('organizer',200),
                               role=text('role',60),summary=text('summary',1000),prize=text('prize',300),
                               links=[dict(title=title(str(l.get('title') or 'Ссылка'))[:60],url=l['url']) for l in links]))
        return uid

    def stage(self,store,body):
        tz = self.config.timezone
        project,kind = body['project'],body['kind']
        if not store.project(project) or kind not in ('task','event','allday'):
            raise ValueError('Неизвестный хакатон или тип этапа')
        if kind == 'allday':
            days = int(body.get('days',1))
            if not 1 <= days <= 14:
                raise ValueError('От 1 до 14 дней')
            start,duration = datetime.combine(date.fromisoformat(body['date']),datetime.min.time(),tzinfo=tz),days*1440
        else:
            start,duration = moment(body['start'],tz),int(body.get('duration',60)) if kind == 'event' else 0
            if kind == 'event' and not 5 <= duration <= 1440:
                raise ValueError('Длительность: 5–1440 минут')
        row = ('task' if kind == 'task' else 'event',title(body['title']),start.isoformat(),duration,
               str(body.get('note') or '').strip()[:500],int(kind == 'allday'))
        if body.get('id'):
            with store.db:
                store.db.execute('UPDATE items SET kind=?,title=?,due=?,duration=?,note=?,all_day=? WHERE id=? AND project=?',
                                 row+(int(body['id']),project))
        else:
            store.add_item(row[0],row[1],row[2],row[3],project=project,note=row[4],all_day=row[5])

    def mutate(self,store,action,body):
        tz = self.config.timezone
        if action == 'check':
            if type(body['done']) is not bool or not isinstance(body['key'],str) or len(body['key'])>150:
                raise ValueError('Неверная отметка')
            store.check(body['key'],body['done'])
        elif action == 'task-check':
            if type(body['done']) is not bool:
                raise ValueError('Неверная отметка')
            with store.db:
                store.db.execute("UPDATE items SET done=? WHERE id=? AND kind='task'",(int(body['done']),int(body['id'])))
        elif action in ('task','event'):
            start = moment(body['start'],tz)
            duration = int(body.get('duration',120)) if action == 'event' else 0
            if action == 'event' and not 5 <= duration <= 1440:
                raise ValueError('Длительность: 5–1440 минут')
            kind = 'event' if action == 'event' else 'task'
            text = title(body['title'])
            if body.get('id'):
                with store.db:
                    store.db.execute('UPDATE items SET title=?,due=?,duration=? WHERE id=? AND kind=?',
                                     (text,start.isoformat(),duration,int(body['id']),kind))
            else:
                store.add_item(kind,text,start.isoformat(),duration)
        elif action == 'override':
            key = body['key']
            start,end = moment(body['start'],tz),moment(body['end'],tz)
            if len(key)>150 or start.date() != date.fromisoformat(key.rsplit('|',1)[1]) or not timedelta(minutes=5)<=end-start<=timedelta(days=1):
                raise ValueError('Измените время внутри выбранного дня, длительность 5–1440 минут')
            store.put_override(key,dict(title=title(body['title']),start=start.isoformat(),end=end.isoformat(),cancelled=bool(body.get('cancelled',False))))
        elif action == 'reset-override':
            with store.db:
                store.db.execute('DELETE FROM overrides WHERE key=?',(body['key'],))
        elif action == 'delete-task':
            with store.db:
                store.db.execute("DELETE FROM items WHERE id=? AND kind='task'",(int(body['id']),))
        elif action == 'rule':
            days = sorted(set(body['weekdays']))
            duration = int(body['duration_minutes'])
            if not days or any(type(d) is not int or not 0<=d<=6 for d in days) or not 5<=duration<=1440 or body['cycle'] not in ('all','a','b'):
                raise ValueError('Проверьте дни, цикл и длительность')
            store.put_rule(dict(id=body.get('id') or 'custom-'+uuid4().hex[:8],weekdays=days,time=clock(body['time']),
                                duration_minutes=duration,cycle=body['cycle'],title=title(body['title'])))
        elif action == 'delete-rule':
            store.delete_rule(body['id'])
        elif action == 'project':
            return {'ok':True,'id':self.project(store,body)}
        elif action == 'hack-status':
            project = store.project(body['id'])
            if not project or body['status'] not in hackathons.STATUSES:
                raise ValueError('Неверный статус')
            project['status'] = body['status']
            store.put_project(project)
        elif action == 'delete-project':
            store.delete_project(body['id'])
        elif action == 'stage':
            self.stage(store,body)
        elif action == 'stage-check':
            if type(body['done']) is not bool:
                raise ValueError('Неверная отметка')
            item = next((i for i in store.items(include_done=True) if i['id'] == int(body['id']) and i['project']),None)
            if not item:
                raise ValueError('Этап не найден')
            hackathons.set_done(store,item,body['done'])
        elif action == 'delete-stage':
            with store.db:
                store.db.execute('DELETE FROM items WHERE id=? AND project IS NOT NULL',(int(body['id']),))
        elif action == 'settings':
            for name in ('digest_times','weather_times'):
                if name in body:
                    if not isinstance(body[name],list) or len(body[name])>6:
                        raise ValueError('До шести отправок')
                    store.set(name,sorted({clock(t) for t in body[name]}))
            if 'mail_enabled' in body:
                if type(body['mail_enabled']) is not bool:
                    raise ValueError('Неверная настройка')
                store.set('mail_enabled',body['mail_enabled'])
            if 'notifications' in body:
                if type(body['notifications']) is not bool:
                    raise ValueError('Неверная настройка')
                store.set('notifications',body['notifications'])
        else:
            raise ValueError('Неизвестное действие')
        return {'ok':True}

    def handler(self):
        app = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass

            def respond(self,status,data,kind='application/json; charset=utf-8'):
                payload = json.dumps(data,ensure_ascii=False).encode() if 'json' in kind else data
                self.send_response(status)
                for k,v in [('Content-Type',kind),('Content-Length',str(len(payload))),('Cache-Control','no-store'),
                            ('X-Content-Type-Options','nosniff'),('Referrer-Policy','no-referrer')]:
                    self.send_header(k,v)
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                route = urlsplit(self.path)
                static = {'/':('index.html','text/html'),'/app.js':('app.js','application/javascript'),'/style.css':('style.css','text/css')}
                if route.path in static:
                    name,kind = static[route.path]
                    return self.respond(200,(STATIC/name).read_bytes(),kind+'; charset=utf-8')
                if route.path != '/api/state':
                    return self.respond(404,{'error':'Не найдено'})
                self.api('state',parse_qs(route.query))

            def do_POST(self):
                path = urlsplit(self.path).path
                if not path.startswith('/api/'):
                    return self.respond(404,{'error':'Не найдено'})
                try:
                    size = int(self.headers.get('Content-Length','0'))
                    if not 0<size<=16000:
                        raise ValueError()
                    body = json.loads(self.rfile.read(size))
                except (ValueError,TypeError):
                    return self.respond(400,{'error':'Неверный запрос'})
                self.api(path[5:],body)

            def api(self,action,body):
                store = None
                try:
                    verify_init_data(self.headers.get('X-Telegram-Init-Data',''),app.config.token,app.config.owner_id)
                    store = Store(app.config.data_dir/'bot.sqlite3')
                    if action=='state':
                        first,last = date.fromisoformat(body['from'][0]),date.fromisoformat(body['to'][0])
                        if not 0<=(last-first).days<=41:
                            raise ValueError()
                        result = asyncio.run(app.state(store,first,last))
                    else:
                        result = app.mutate(store,action,body)
                    self.respond(200,result)
                except PermissionError as exc:
                    self.respond(403,{'error':str(exc)})
                except (ValueError,KeyError,TypeError,IndexError):
                    self.respond(400,{'error':'Проверьте поля, дату и время.'})
                except Exception:
                    self.respond(500,{'error':'Не удалось загрузить или сохранить. Повторите запрос.'})
                finally:
                    if store:
                        store.close()
        return Handler


def start_server(config):
    server = ThreadingHTTPServer((os.getenv('MINI_APP_HOST','127.0.0.1'),int(os.getenv('MINI_APP_PORT','8765'))),App(config).handler())
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server
