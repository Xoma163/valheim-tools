import re
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fastapi.testclient import TestClient
from valheim_admin.app import Settings, create_app


def client():
    return TestClient(create_app(Settings('x' * 48,
                                         origin='http://testserver', cooldown=0)))


def token(c, path='/'):
    return re.search(r'name="csrf-token" content="([^"]+)"', c.get(path).text).group(1)


def test_local_access_and_public_map():
    c = client()
    assert c.get('/api/status').status_code == 200
    assert c.post('/api/control/start').status_code == 403
    assert c.get('/', follow_redirects=False).status_code == 200
    assert c.get('/map').status_code == 200
    assert 'Карта ещё не подключена' in c.get('/map').text
    assert c.get('/api/control/start').status_code == 405


def test_pages_and_mods():
    c = client()
    for path in ['/', '/map', '/guide', '/faq', '/mods']:
        assert c.get(path).status_code == 200
    html = c.get('/mods').text
    assert html.count('<tr data-mod>') == 12
    assert 'https://thunderstore.io/c/valheim/p/bdew/QuickConnect/' in html
    assert 'NetworkPerformanceSystem' in html
    assert 'Zen_ModLib' in html
    assert 'Lumberjacking' not in html
    assert 'test-password' not in c.get('/').text


def test_control_and_csrf():
    c = client()
    csrf = token(c)
    assert c.post('/api/control/start').status_code == 403
    headers = {'X-CSRF-Token': csrf, 'Origin': 'https://evil.invalid'}
    assert c.post('/api/control/start', headers=headers).status_code == 403
    headers['Origin'] = 'http://testserver'
    with patch('valheim_admin.app.subprocess.run') as run:
        for action, state in [('start', True), ('stop', False), ('restart', True)]:
            assert c.post('/api/control/' + action, headers=headers).status_code == 200
            assert c.get('/api/status').json()['online'] is state
        run.assert_not_called()
    assert c.post('/api/control/arbitrary-command', headers=headers).status_code == 400


def test_no_application_login():
    c = client()
    assert c.get('/login').status_code == 404
    assert c.post('/login').status_code == 405
    assert 'name="password"' not in c.get('/').text


def test_cooldown():
    c = client()
    c.app.state.backend.settings.cooldown = 30
    headers = {'X-CSRF-Token': token(c)}
    assert c.post('/api/control/start', headers=headers).status_code == 200
    assert c.post('/api/control/stop', headers=headers).status_code == 429


def test_reference_layout_and_content():
    c = client()
    html = c.get('/').text
    assert 'panel status-card' in html
    assert 'panel control-card' in html
    assert 'server-confirm-modal' in html
    assert 'stat-card"' not in html
    assert 'Import new profile' in c.get('/guide').text
    assert 'Как быстро разложить вещи на базе' in c.get('/faq').text
    assert 'Установите Valheim' not in c.get('/guide').text
    assert c.post('/server/action', data={'action': 'start'}).status_code == 403
    response = c.post('/server/action', data={'action': 'start', 'csrf': token(c)})
    assert response.status_code == 200
    assert c.get('/api/status').json()['online']


def test_guide_and_game_password_visibility():
    password = 'example-game-password'
    settings = Settings('x' * 48, game_password=password, profile_code='example-profile-code')
    c = TestClient(create_app(settings))
    html = c.get('/guide').text
    assert html.count('class="panel guide-step"') == 6
    for text in (settings.profile_code, 'SHTAB-SORTIR',
                 'Start modded', 'нового персонажа', settings.address, password):
        assert text in html
    assert password in c.get('/').text
    for value in (settings.profile_code, 'SHTAB-SORTIR', settings.address, password):
        assert f'data-copy-value="{value}"' in html
    assert f'data-copy-value="{password}"' in c.get('/').text
    assert 'navigator.clipboard.writeText(button.dataset.copyValue)' in html
    assert password not in repr(settings)
    faq = c.get('/faq').text
    assert f'data-copy-value="ШТАБ-СОРТИР:{settings.address}:{password}"' in faq
    assert 'navigator.clipboard.writeText(button.dataset.copyValue)' in faq
    assert 'ВАШ_ПАРОЛЬ' not in faq
    for path in ('/map', '/mods', '/api/status'):
        assert password not in c.get(path).text
    assert 'example-game-password' not in client().get('/guide').text


def test_game_password_loaded_from_environment(monkeypatch):
    monkeypatch.setenv('VALHEIM_GAME_PASSWORD', 'example-env-password')
    assert Settings.environment().game_password == 'example-env-password'
    monkeypatch.setenv('VALHEIM_PROFILE_CODE', 'example-profile-code')
    monkeypatch.setenv('VALHEIM_GAME_ADDRESS', 'game.example.invalid:1234')
    assert Settings.environment().profile_code == 'example-profile-code'
    assert Settings.environment().address == 'game.example.invalid:1234'


def test_faq_mods_and_dump_instructions():
    html = client().get('/faq').text
    for name in ('CrewStats', 'EquipmentAndQuickSlots', 'InputTweaks', 'MissingPieces',
                 'PlantEasily', 'SocialSystem', 'Stay Loaded', 'StoreAndCraft', 'ValheimWebMap',
                 'NetworkPerformanceSystem', 'Zen_ModLib', 'QuickConnect'):
        assert re.search(r'<summary>[^<]*' + re.escape(name), html)
    for text in ('Period', 'Mouse2', 'DumpKey', 'Dump skips the hotbar', 'Save',
                 'com.morda.storeandcraft.cfg', 'Устанавливать на клиент не нужно',
                 'Start modded', 'ReplantOnHarvest', 'Party HUD', 'advize.PlantEasily.cfg',
                 'полностью заряди оружие', 'открой меню строительства',
                 'перетащи нужный предмет', 'Factorio'):
        assert text in html
    assert html.count('<details class="faq-item"') == 25
    assert 'QuickConnect 1.7.0 от bdew' in html
    assert 'quick_connect_servers.cfg' in html
    for text in ('Settings → Directories', 'Browse напротив Profile folder',
                 'Reload From Disk', 'Awesome Server', 'ШТАБ-СОРТИР:game.example.invalid:2456:'):
        assert text in html
    assert 'Пароль хранится открытым текстом' in html
    assert not re.search(r'<details\b[^>]*\sopen(?:\s|=|>)', html)
    assert 'Здесь будет ответ' not in html


def test_mod_pages_without_update_notice():
    c = client()
    mods = c.get('/mods').text
    for text in ('NetworkPerformanceSystem', 'CrewStats', 'StoreAndCraft', 'необязателен'):
        assert text in mods
    assert 'Что изменилось' not in mods
    assert '12 МОДОВ И БИБЛИОТЕК' in mods
    assert 'ВЕРСИИ ЕЩЁ НЕ ЗАФИКСИРОВАНЫ' not in mods
    assert 'это пока не готовый профиль' not in mods
    assert 'id="nps"' in c.get('/faq').text
    assert 'update-title' not in c.get('/').text
    assert 'NetworkPerformanceSystem' not in c.get('/guide').text


def test_faq_server_information():
    html = client().get('/faq').text
    for text in ('24/7', 'NetworkPerformanceSystem', '15 игроков', 'i5-12600', '32 ГБ', '3600 МГц',
                 'SSD 512 ГБ', 'AndrewSha', '1 час', '4 часа', '44', '15 минут',
                 'Минимум 1 месяц', 'Бэкапы мира будут выложены для скачивания'):
        assert text in html
    assert 'game.example.invalid:2456' in html
    for removed in ('Это характеристики машины', 'Также в сундуке должно быть место',
                    'История накапливается со временем', 'Второй игрок должен принять приглашение'):
        assert removed not in html


def test_faq_community_rules():
    html = client().get('/faq').text
    for text in ('Начинаем новыми персонажами', 'ресурсы из других миров не переносим',
                 'Нежелательно — пока нет единой позиции', 'В любом случае будет анонс',
                 'всем вместе', 'Discord', 'Деда', 'ValheimWebMap отключён на клиенте'):
        assert text in html


def test_map_open_separately():
    c = TestClient(create_app(Settings('x' * 48, map_url='/map-proxy/')))
    html = c.get('/map').text
    assert 'map-frame-header' in html
    assert 'Открыть отдельно' in html
    assert 'href="/map-proxy/" target="_blank" rel="noopener noreferrer"' in html
    assert 'src="/map-proxy/"' in html


def test_systemd_exact_command():
    c = client()
    headers = {'X-CSRF-Token': token(c)}
    c.app.state.backend.settings.mode = 'systemd'
    with patch('valheim_admin.app.subprocess.run') as run:
        run.return_value.returncode = 0
        assert c.post('/api/control/start', headers=headers).status_code == 200
        assert run.call_args.args[0] == ['/usr/bin/sudo', '-n', '/usr/bin/systemctl', 'start', '--no-block', 'valheim.service']


def test_unavailable_is_not_zero_players():
    c = client()
    c.app.state.backend.settings.mode = 'systemd'
    with patch('valheim_admin.app.subprocess.run', side_effect=OSError), patch('valheim_admin.app.a2s.info', side_effect=TimeoutError):
        result = c.get('/api/status').json()
        assert not result['online']
        assert result['count'] is None
        assert result['ping'] is None


def test_player_names_from_webmap_and_safe_rendering():
    c = client()
    c.app.state.backend.settings.mode = 'systemd'
    payload = {'count': 1, 'players': [{'name': '<b>AndrewSha</b>', 'hidden': True}]}
    with patch('valheim_admin.app.subprocess.run') as run, \
         patch('valheim_admin.app.a2s.info', return_value=SimpleNamespace(
             ping=.01, player_count=1, max_players=10)), \
         patch('valheim_admin.app.a2s.players', side_effect=TimeoutError), \
         patch('valheim_admin.app.urlopen', return_value=BytesIO(json.dumps(payload).encode())) as request:
        run.return_value.stdout = 'active'
        status = c.get('/api/status').json()
        assert status['names'] == ['<b>AndrewSha</b>']
        assert status['names_available']
        request.assert_called_once_with('http://127.0.0.1:8021/data/players.json', timeout=1)
        html = c.get('/').text
        assert '&lt;b&gt;AndrewSha&lt;/b&gt;' in html
        assert '<b>AndrewSha</b>' not in html


@pytest.mark.parametrize('payload', [
    b'not json', b'[]', b'{"count":1,"players":null}',
    b'{"count":0,"players":[]}',
    b'{"count":1,"players":[{"name":null}]}',
    b'{"count":2,"players":[{"name":"A"}]}',
])
def test_invalid_or_mismatched_webmap_does_not_invent_names(payload):
    c = client()
    with patch('valheim_admin.app.subprocess.run') as run, \
         patch('valheim_admin.app.a2s.info', return_value=SimpleNamespace(
             ping=.01, player_count=1, max_players=10)), \
         patch('valheim_admin.app.a2s.players', return_value=[]), \
         patch('valheim_admin.app.urlopen', return_value=BytesIO(payload)):
        run.return_value.stdout = 'active'
        status = c.app.state.backend.query()
        assert status['online'] and status['count'] == 1
        assert status['names'] == []
        assert not status['names_available']


def test_webmap_not_requested_when_a2s_names_available():
    c = client()
    with patch('valheim_admin.app.subprocess.run'), \
         patch('valheim_admin.app.a2s.info', return_value=SimpleNamespace(
             ping=.01, player_count=1, max_players=10)), \
         patch('valheim_admin.app.a2s.players', return_value=[SimpleNamespace(name='AndrewSha')]), \
         patch('valheim_admin.app.urlopen') as request:
        status = c.app.state.backend.query()
        assert status['names'] == ['AndrewSha']
        assert status['names_available']
        request.assert_not_called()


def test_webmap_unavailable_keeps_player_count():
    c = client()
    with patch('valheim_admin.app.subprocess.run'), \
         patch('valheim_admin.app.a2s.info', return_value=SimpleNamespace(
             ping=.01, player_count=1, max_players=10)), \
         patch('valheim_admin.app.a2s.players', return_value=[]), \
         patch('valheim_admin.app.urlopen', side_effect=OSError):
        status = c.app.state.backend.query()
        assert status['online'] and status['count'] == 1
        assert not status['names_available']
