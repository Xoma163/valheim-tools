import asyncio
import json
import logging
import os
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

import a2s
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT.parent / '.env')
log = logging.getLogger('valheim_admin')

MODS = [
    ('Bagr', 'CrewStats', 'Статистика участников сервера'),
    ('RandyKnapp', 'EquipmentAndQuickSlots', 'Отдельные слоты экипировки и быстрые слоты'),
    ('Riintouge', 'InputTweaks', 'Перенос на Shift, разделение на Ctrl и управление инвентарём'),
    ('BentoG', 'MissingPieces', 'Дополнительные строительные детали'),
    ('MidnightMods', 'NetworkPerformanceSystem', 'Сетевая синхронизация и лимит 15 игроков; на клиенте необязателен'),
    ('Advize', 'PlantEasily', 'Удобная посадка растений'),
    ('bdew', 'QuickConnect', 'Быстрое подключение к серверу с сохранённым паролем; только клиент'),
    ('M2Valheim', 'SocialSystem', 'Социальные функции и группы'),
    ('StonedParadise', 'Stay_Loaded', 'Сохранение заряда оружия при убирании'),
    ('Morda', 'StoreAndCraft', 'Хранение, крафт из сундуков и автоматизация'),
    ('f00d4tehg0dz', 'ValheimWebMap', 'Веб-карта мира'),
    ('ZenDragon', 'Zen_ModLib', 'Библиотека ZenMods в клиентской сборке'),
]


@dataclass
class Settings:
    secret: str
    mode: str = 'demo'
    origin: str = 'http://127.0.0.1:11110'
    secure: bool = False
    name: str = 'ШТАБ-СОРТИР'
    world: str = 'shtab-test'
    address: str = 'game.example.invalid:2456'
    query_host: str = '127.0.0.1'
    query_port: int = 2457
    map_url: str = ''
    cooldown: float = 30
    game_password: str = field(default='', repr=False)
    profile_code: str = field(default='', repr=False)

    @classmethod
    def environment(cls):
        return cls(
            secret=os.getenv('VALHEIM_SESSION_SECRET', ''),
            mode=os.getenv('VALHEIM_MODE', 'demo'),
            origin=os.getenv('VALHEIM_PUBLIC_ORIGIN', 'http://127.0.0.1:11110').rstrip('/'),
            secure=os.getenv('VALHEIM_SECURE_COOKIES', 'false').lower() == 'true',
            name=os.getenv('VALHEIM_SERVER_NAME', 'ШТАБ-СОРТИР'),
            world=os.getenv('VALHEIM_WORLD', 'shtab-test'),
            address=os.getenv('VALHEIM_GAME_ADDRESS', 'game.example.invalid:2456'),
            query_host=os.getenv('VALHEIM_QUERY_HOST', '127.0.0.1'),
            query_port=int(os.getenv('VALHEIM_QUERY_PORT', '2457')),
            map_url=os.getenv('VALHEIM_MAP_URL', ''),
            game_password=os.getenv('VALHEIM_GAME_PASSWORD', ''),
            profile_code=os.getenv('VALHEIM_PROFILE_CODE', ''),
        )


class Backend:
    def __init__(self, settings):
        self.settings = settings
        self.demo_running = False
        self.lock = asyncio.Lock()
        self.last_action = float('-inf')
        self.cached = None
        self.cached_at = 0
        self.status_lock = asyncio.Lock()

    def query(self):
        result = dict(online=False, state='Нет ответа', ping=None, count=None,
                      maximum=None, names=[], names_available=False, service='Неизвестно')
        try:
            p = subprocess.run(['/usr/bin/systemctl', 'is-active', 'valheim.service'],
                               capture_output=True, text=True, timeout=3)
            result['service'] = p.stdout.strip() or 'unknown'
        except (OSError, subprocess.TimeoutExpired):
            pass
        address = (self.settings.query_host, self.settings.query_port)
        try:
            info = a2s.info(address, timeout=2)
            result.update(online=True, state='Онлайн', ping=round(info.ping * 1000),
                          count=info.player_count, maximum=info.max_players)
        except Exception:
            return result
        try:
            players = a2s.players(address, timeout=2)
            result['names'] = [p.name for p in players if p.name]
            result['names_available'] = len(result['names']) == result['count']
        except Exception:
            pass
        if not result['names_available'] and result['count'] > 0:
            try:
                # Fixed loopback endpoint: no player-supplied URLs or public proxy.
                with urlopen('http://127.0.0.1:8021/data/players.json', timeout=1) as response:
                    snapshot = json.loads(response.read(65537))
                if isinstance(snapshot, dict) and isinstance(snapshot.get('players'), list):
                    players = snapshot['players']
                    names = [p['name'].strip() for p in players
                             if isinstance(p, dict) and isinstance(p.get('name'), str)
                             and p['name'].strip()]
                    # Different snapshots can briefly disagree during joins/leaves.
                    # Do not present an incomplete or stale map list as authoritative.
                    if snapshot.get('count') == len(players) == len(names) == result['count']:
                        result['names'] = names
                        result['names_available'] = True
            except (OSError, ValueError):
                pass
        return result

    async def status(self):
        if self.settings.mode == 'demo':
            return dict(online=self.demo_running, state='Онлайн (демо)' if self.demo_running else 'Остановлен (демо)',
                        ping=None, count=0 if self.demo_running else None, maximum=None,
                        names=[], names_available=False, service='Демонстрация')
        async with self.status_lock:
            if self.cached is None or time.monotonic() - self.cached_at > 5:
                self.cached = await asyncio.to_thread(self.query)
                self.cached_at = time.monotonic()
            return self.cached

    async def control(self, action):
        if action not in {'start', 'stop', 'restart'}:
            raise HTTPException(400, 'Неизвестная команда')
        async with self.lock:
            if time.monotonic() - self.last_action < self.settings.cooldown:
                raise HTTPException(429, 'Подождите 30 секунд перед следующей командой')
            if self.settings.mode == 'demo':
                self.demo_running = action != 'stop'
            else:
                try:
                    p = await asyncio.to_thread(subprocess.run,
                        ['/usr/bin/sudo', '-n', '/usr/bin/systemctl', action, '--no-block', 'valheim.service'],
                        capture_output=True, text=True, timeout=10)
                    if p.returncode:
                        log.error('Control %s failed: %s', action, p.stderr)
                        raise HTTPException(502, 'systemd отклонил команду. Проверьте журнал админки.')
                except (OSError, subprocess.TimeoutExpired):
                    raise HTTPException(502, 'Не удалось отправить команду systemd')
            self.last_action = time.monotonic()
            self.cached = None
            log.info('Server control: %s mode=%s', action, self.settings.mode)


def create_app(settings=None):
    s = settings or Settings.environment()
    if len(s.secret) < 32:
        raise RuntimeError('Задайте VALHEIM_SESSION_SECRET (не менее 32 символов) в .env')
    if s.mode not in {'demo', 'systemd'}:
        raise RuntimeError('VALHEIM_MODE должен быть demo или systemd')
    if s.map_url and not (s.map_url.startswith('/') and not s.map_url.startswith('//')):
        if urlparse(s.map_url).scheme not in {'http', 'https'}:
            raise RuntimeError('Неверный URL карты')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=s.secret, session_cookie='valheim_session',
                       same_site='strict', https_only=s.secure, max_age=43200)
    templates = Jinja2Templates(directory=ROOT / 'templates')
    backend = Backend(s)
    app.state.backend = backend

    def csrf(request, value):
        expected = request.session.get('csrf', '')
        if not expected or not secrets.compare_digest(str(value or ''), expected):
            raise HTTPException(403, 'Неверный CSRF-токен. Обновите страницу.')
        origin = request.headers.get('origin')
        if origin and origin != s.origin:
            raise HTTPException(403, 'Недопустимый источник запроса')

    def render(request, page, **extra):
        request.session.setdefault('csrf', secrets.token_urlsafe(32))
        name = {'home': 'dashboard.html', 'guide': 'guide.html', 'faq': 'faq.html'}.get(page, 'page.html')
        return templates.TemplateResponse(request=request, name=name, context={
            'page': page, 'current_page': 'dashboard' if page == 'home' else page, 's': s,
            'csrf': request.session['csrf'], 'mods': MODS, **extra})

    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Referrer-Policy'] = 'same-origin'
        return response

    @app.get('/api/status')
    async def status(request: Request):
        return await backend.status()

    @app.post('/api/control/{action}')
    async def control(request: Request, action: str):
        csrf(request, request.headers.get('x-csrf-token'))
        await backend.control(action)
        return {'message': 'Демо-состояние изменено; реальный сервер не затронут.' if s.mode == 'demo'
                else 'Команда отправлена на сервер. Дождитесь изменения статуса.'}

    @app.get('/')
    async def index(request: Request):
        status = await backend.status()
        adapted = dict(online=status['online'], host=s.address, version=None,
                       latency_ms=status['ping'], players_online=status['count'],
                       players_max=status['maximum'] if status['maximum'] is not None else '—',
                       player_names=status['names'])
        return render(request, 'home', server_status=adapted,
                      message=request.query_params.get('message'), error=request.query_params.get('error'))

    @app.post('/server/action')
    async def form_control(request: Request):
        from urllib.parse import urlencode
        form = await request.form()
        csrf(request, form.get('csrf'))
        try:
            await backend.control(str(form.get('action', '')))
            result = {'message': 'Демо-состояние изменено.' if s.mode == 'demo' else 'Команда отправлена на сервер.'}
        except HTTPException as exc:
            result = {'error': str(exc.detail)}
        return RedirectResponse('/?' + urlencode(result), status_code=303)

    @app.get('/{page}')
    async def page(request: Request, page: str):
        if page not in {'map', 'guide', 'faq', 'mods'}:
            raise HTTPException(404)
        return render(request, page)

    return app
