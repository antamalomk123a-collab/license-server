# -*- coding: utf-8 -*-
"""Premium Plugin Loader for phBot.

Server contract for POST /api/plugins/<plugin_id>:
- Authenticate `session_token` and its HWID on the server for every request.
- Verify the user has an active entitlement for plugin_id.
- Return: status=ok, payload_b64=Base64(zlib.compress(source UTF-8)),
  sha256=SHA-256 of the uncompressed UTF-8 source.
- Never return a `source` field and never accept plugin code from the client.

This prevents a permanent .py cache. It is not absolute source-code protection:
Python must exist in memory while it is executing. Keep credentials, license logic,
and other sensitive operations on your server behind authenticated API endpoints.
"""
from phBot import *
import QtBind
import urllib.request
import urllib.error
import json
import base64
import zlib
import threading
import time
import hashlib
import platform
import os

PLUGIN_NAME = 'PremiumPluginLoader'
VERSION = '1.0'
# ضع رابط سيرفرك HTTPS هنا قبل إرسال الملف للعميل.
SERVER_URL = 'https://YOUR-SERVER.up.railway.app'.rstrip('/')
LOGIN_URL = SERVER_URL + '/api/login'
PLUGINS_URL = SERVER_URL + '/api/plugins'
HEARTBEAT_URL = SERVER_URL + '/api/heartbeat'
HEARTBEAT_SECONDS = 60
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.premium_loader.json')

_token = ''
_logged_in = False
_plugins = []
_name_to_id = {}
_loaded = set()
_loading = set()
_plugin_namespaces = {}
_generation = 0

def _log(message):
    try: log('[%s] %s' % (PLUGIN_NAME, message))
    except: print('[%s] %s' % (PLUGIN_NAME, message))

def _hwid():
    try:
        raw = '%s|%s|%s' % (platform.node(), platform.system(), platform.machine())
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]
    except: return 'unknown-hwid'

def _auth_data():
    return {'session_token': _token, 'hwid': _hwid()}

def _post(url, data):
    try:
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'),
            headers={'Content-Type':'application/json', 'Accept':'application/json',
                     'User-Agent':'%s/%s' % (PLUGIN_NAME, VERSION)}, method='POST')
        response = urllib.request.urlopen(req, timeout=20)
        try: return json.loads(response.read().decode('utf-8')), response.getcode()
        finally: response.close()
    except urllib.error.HTTPError as e:
        try: return json.loads(e.read().decode('utf-8')), e.code
        except: return {'detail':'HTTP %s' % e.code}, e.code
    except Exception as e: return {'detail':str(e)}, 0

def _settings():
    try:
        with open(SETTINGS_FILE, 'r') as f: return json.load(f)
    except: return {}

def _save_settings():
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump({'username': QtBind.text(gui, txt_user), 'autoload': _checked(chk_autoload)}, f)
    except: pass

def _checked(widget):
    try: return bool(QtBind.isChecked(gui, widget))
    except: return False

def _set_status(text):
    try: QtBind.setText(gui, lbl_status, text)
    except: pass
    _log(text)

def _clear_list():
    try: QtBind.clear(gui, list_plugins)
    except: pass

def _fill_plugins():
    global _name_to_id
    _name_to_id = {}
    _clear_list()
    try: QtBind.clear(gui, combo_plugins)
    except: pass
    if not _plugins:
        try: QtBind.append(gui, list_plugins, 'No Premium plugins available')
        except: pass
        return
    for item in _plugins:
        name = item.get('name') or item.get('id')
        pid = item.get('id')
        if not pid: continue
        _name_to_id[name] = pid
        try: QtBind.append(gui, combo_plugins, name)
        except: pass
        try: QtBind.append(gui, list_plugins, name + '  [' + item.get('version','') + ']')
        except: pass

def _refresh_plugins():
    global _plugins
    if not _token: return False
    data, code = _post(PLUGINS_URL, _auth_data())
    if code != 200:
        _set_status('Plugin list error: ' + str(data.get('detail', code)))
        return False
    _plugins = data.get('plugins', [])
    _fill_plugins()
    return True

def _selected_id():
    try: name = QtBind.text(gui, combo_plugins)
    except: name = ''
    return _name_to_id.get(name, '')

# Plugins are executed in memory only. No .py copy is written to the Plugins folder.
# The server must return a compressed Base64 payload, not a `source` field:
# {"status":"ok", "payload_b64":"...", "sha256":"<hash of decoded UTF-8 source>"}
def _decode_plugin_payload(data):
    payload = data.get('payload_b64', '')
    expected = (data.get('sha256', '') or '').lower()
    if not payload or not expected:
        raise ValueError('Invalid server response (missing payload_b64 or sha256)')
    try:
        packed = base64.b64decode(payload.encode('ascii'), validate=True)
        source_bytes = zlib.decompress(packed)
        source = source_bytes.decode('utf-8')
    except Exception as e:
        raise ValueError('Could not decode plugin payload: %s' % e)
    actual = hashlib.sha256(source_bytes).hexdigest()
    if actual != expected:
        raise ValueError('Plugin integrity verification failed')
    return source

# The loaded plugin callbacks are kept isolated, then forwarded by the loader.
def _execute_plugin(plugin_id, source, display):
    # Deliberately use a virtual file name: the source is never cached on disk.
    virtual_path = '<premium:%s>' % plugin_id
    ns = {'__file__': virtual_path, '__name__': __name__, '__builtins__': __builtins__,
          'PREMIUM_LOADER_PLUGIN_ID': plugin_id, 'PREMIUM_LICENSE_VALID': True}
    try:
        exec(compile(source, virtual_path, 'exec'), ns)
    except BaseException as e:
        _log('Plugin error [%s]: %s' % (display, e))
        return False
    _plugin_namespaces[plugin_id] = ns
    # phBot button callbacks created by the downloaded plugin must be globally visible.
    reserved = set(['button_login','button_load_selected','button_load_all','button_refresh',
                    'checkbox_autoload_changed','joined_game','teleported','event_loop',
                    'handle_joymax','handle_silkroad','handle_event','handle_chat',
                    'handle_script','bot_started','bot_stopped','script_finished'])
    for name, value in ns.items():
        if callable(value) and not name.startswith('_') and name not in reserved and name not in globals():
            globals()[name] = value
    return True

def _load_worker(plugin_id):
    if plugin_id in _loaded or plugin_id in _loading: return
    _loading.add(plugin_id)
    try:
        item = next((x for x in _plugins if x.get('id') == plugin_id), None)
        display = (item or {}).get('name', plugin_id)
        _set_status('Loading ' + display + '...')
        data, code = _post(PLUGINS_URL + '/' + plugin_id, _auth_data())
        if code != 200 or data.get('status') != 'ok':
            _set_status('Load error: ' + str(data.get('detail', code)))
            return
        try:
            source = _decode_plugin_payload(data)
        except Exception as e:
            _set_status('Load stopped: ' + str(e))
            return
        if _execute_plugin(plugin_id, source, display):
            _loaded.add(plugin_id)
            _set_status('Loaded: ' + display)
            _log('Plugin loaded: ' + display)
    finally:
        _loading.discard(plugin_id)

def _load_all_worker():
    for item in list(_plugins):
        if _logged_in:
            _load_worker(item.get('id'))

def _heartbeat(token, generation):
    global _logged_in, _plugins
    while _logged_in and generation == _generation:
        time.sleep(HEARTBEAT_SECONDS)
        if not _logged_in or generation != _generation: return
        data, code = _post(HEARTBEAT_URL, {'session_token': token, 'hwid': _hwid()})
        if code == 200:
            _plugins = data.get('plugins', _plugins)
            continue
        _logged_in = False
        _set_status('License stopped: ' + str(data.get('detail', code)))
        return

# GUI
saved = _settings()
gui = QtBind.init(__name__, PLUGIN_NAME)
QtBind.createLabel(gui, 'Username:', 10, 12)
txt_user = QtBind.createLineEdit(gui, saved.get('username',''), 80, 10, 180, 24)
QtBind.createLabel(gui, 'Password:', 10, 47)
txt_pass = QtBind.createLineEdit(gui, '', 80, 45, 180, 24)
btn_login = QtBind.createButton(gui, 'button_login', 'Login', 10, 80)
chk_autoload = QtBind.createCheckBox(gui, 'checkbox_autoload_changed', 'Auto Load all Premium plugins', 90, 82)
try: QtBind.setChecked(gui, chk_autoload, bool(saved.get('autoload', True)))
except: pass
lbl_status = QtBind.createLabel(gui, 'Not logged in', 10, 115)
QtBind.createLabel(gui, 'Premium Plugins:', 10, 145)
combo_plugins = QtBind.createCombobox(gui, 10, 165, 250, 24)
btn_load_one = QtBind.createButton(gui, 'button_load_selected', 'Load selected', 270, 165)
btn_load_all = QtBind.createButton(gui, 'button_load_all', 'Load all', 380, 165)
btn_refresh = QtBind.createButton(gui, 'button_refresh', 'Refresh list', 10, 195)
list_plugins = QtBind.createList(gui, 10, 225, 460, 130)
try: QtBind.setEchoMode(gui, txt_pass, 'Password')
except: pass

def checkbox_autoload_changed(checked=False): _save_settings()

def button_login():
    global _token, _logged_in, _plugins, _generation
    username = QtBind.text(gui, txt_user).strip()
    password = QtBind.text(gui, txt_pass)
    if not username or not password:
        _set_status('Enter username and password first')
        return
    _set_status('Logging in...')
    data, code = _post(LOGIN_URL, {'username':username, 'password':password, 'hwid':_hwid()})
    if code != 200 or data.get('status') != 'ok':
        _set_status('Login error: ' + str(data.get('detail', code)))
        return
    _token = data.get('session_token','')
    _logged_in = bool(_token)
    _plugins = data.get('plugins', [])
    _generation += 1
    _save_settings()
    _fill_plugins()
    _set_status('Premium active - ' + str(len(_plugins)) + ' plugins available')
    _log('Logged in - valid until ' + str(data.get('subscription_end','')))
    threading.Thread(target=_heartbeat, args=(_token, _generation), daemon=True).start()
    if _checked(chk_autoload): threading.Thread(target=_load_all_worker, daemon=True).start()

def button_refresh():
    if not _logged_in: _set_status('Login first'); return
    threading.Thread(target=_refresh_plugins, daemon=True).start()

def button_load_selected():
    if not _logged_in: _set_status('Login first'); return
    pid = _selected_id()
    if not pid: _set_status('Choose a plugin first'); return
    threading.Thread(target=_load_worker, args=(pid,), daemon=True).start()

def button_load_all():
    if not _logged_in: _set_status('Login first'); return
    threading.Thread(target=_load_all_worker, daemon=True).start()

# Forward phBot events to every successfully loaded Premium plugin.
def _dispatch(event, *args):
    for pid, ns in list(_plugin_namespaces.items()):
        fn = ns.get(event)
        if callable(fn):
            try: fn(*args)
            except Exception as e: _log('%s callback error [%s]: %s' % (event, pid, e))

def joined_game(): _dispatch('joined_game')
def teleported(): _dispatch('teleported')
def event_loop(): _dispatch('event_loop')
def bot_started(): _dispatch('bot_started')
def bot_stopped(): _dispatch('bot_stopped')
def script_finished(): _dispatch('script_finished')
def handle_joymax(opcode, data): _dispatch('handle_joymax', opcode, data)
def handle_silkroad(opcode, data): _dispatch('handle_silkroad', opcode, data)
def handle_event(t, data): _dispatch('handle_event', t, data)
def handle_chat(t, player, msg): _dispatch('handle_chat', t, player, msg)
def handle_script(args): _dispatch('handle_script', args)
