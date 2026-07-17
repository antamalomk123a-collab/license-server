import phBot
from phBot import *
import QtBind
import threading
import os
import json
import time

# ==================== INFO ====================
pName    = "AutoPLevel"
pVersion = "1.0.0"

# ==================== PATHS ====================
plugin_dir   = os.path.dirname(os.path.realpath(__file__))
scripts_dir  = os.path.join(plugin_dir, "scripts")
_phbot_dir   = os.path.dirname(plugin_dir)
config_dir   = os.path.join(_phbot_dir, "Config", "AutoPLevel")

# ==================== STATE ====================
plugin_enabled      = False
check_timer         = None
party_loop_timer    = None       # timer لـ party check loop المستقل
waiting_party       = False      # بنستنى الـ party
current_script_idx  = 0          # index الـ script الحالي
last_applied_idx    = -1         # آخر script اتطبق
move_check_enabled  = True       # فعّل/وقف فحص الناس في المنطقة
move_cooldown_until = 0          # لما نعمل move مش نعملها تاني فوراً
area_blocked_count  = 0          # كام مرة لقينا المنطقة محجوزة
_last_party_ids     = set()      # آخر IDs لأعضاء الـ party لاكتشاف الجدد

# ==================== CONFIG DATA ====================
# كل entry: { 'name': str, 'script': str, 'target_level': int }
# الـ script بيتشغل من level_start لحد target_level
# لما الـ char يوصل target_level → ينتقل للـ entry التالي
level_entries = []

# ==================== GUI VARS ====================
gui = QtBind.init(__name__, pName)

_y = 8

# --- Control Buttons ---
btn_enable  = QtBind.createButton(gui, 'btn_enable_clicked',  "▶️ Enable  ",  8, _y)
btn_disable = QtBind.createButton(gui, 'btn_disable_clicked', "⏹️ Disable  ", 90, _y)
lbl_status  = QtBind.createLabel(gui, '<font color="#FF4444"><b>● Status: OFF</b></font>', 185, _y + 4)

_y += 30

# --- Party wait checkbox ---
chk_party_wait = QtBind.createCheckBox(gui, 'chk_party_wait_changed', "⏳ Wait For Party (All Members Reach Target Level)", 8, _y)
QtBind.setChecked(gui, chk_party_wait, True)

_y += 24

# --- Move away checkbox ---
chk_move_away = QtBind.createCheckBox(gui, 'chk_move_away_changed', "🐫 Move Away IF Area Occupied By Other Players", 8, _y)
QtBind.setChecked(gui, chk_move_away, True)

_y += 28

# === Level Entries Table ===
QtBind.createLabel(gui, '<font color="#00AAFF"><b>❄️ Level Entries Script Runs Until Target Level is Reached</b></font>', 8, _y)
_y += 18
entries_list = QtBind.createList(gui, 8, _y, 530, 140)
_y += 148

# === Add / Edit Entry ===
QtBind.createLabel(gui, "Script:", 8, _y + 3)
dropdown_script = QtBind.createCombobox(gui, 58, _y, 200, 22)

QtBind.createLabel(gui, "Target Level:", 268, _y + 3)
txt_target_level = QtBind.createLineEdit(gui, "8", 355, _y, 45, 22)

btn_add_entry    = QtBind.createButton(gui, 'btn_add_entry_clicked',    "➕ Add Entry ",    410, _y - 2)
btn_delete_entry = QtBind.createButton(gui, 'btn_delete_entry_clicked', "🗑️ Delete Entry ", 500, _y - 2)

_y += 32

btn_refresh_scripts = QtBind.createButton(gui, 'btn_refresh_scripts_clicked', "🔄 Refresh Scripts ", 8, _y)
btn_move_up         = QtBind.createButton(gui, 'btn_move_up_clicked',         "⬆️ Up Script",        120, _y)
btn_move_down       = QtBind.createButton(gui, 'btn_move_down_clicked',       "⬇️ Down Script",      200, _y)

_y += 32

# === Status Info ===
QtBind.createLabel(gui, '<font color="#FFAA00"><b>Current Script:</b></font>', 8, _y + 3)
lbl_current_script = QtBind.createLabel(gui, '<font color="#FFFFFF">-</font>', 110, _y + 3)

_y += 22
QtBind.createLabel(gui, '<font color="#FFAA00"><b>My Level:</b></font>', 8, _y + 3)
lbl_my_level = QtBind.createLabel(gui, '<font color="#FFFFFF">-</font>', 75, _y + 3)

QtBind.createLabel(gui, '<font color="#FFAA00"><b>  Party Min Level:</b></font>', 130, _y + 3)
lbl_party_level = QtBind.createLabel(gui, '<font color="#FFFFFF">-</font>', 255, _y + 3)

_y += 22
QtBind.createLabel(gui, '<font color="#FFAA00"><b>Waiting for party:</b></font>', 8, _y + 3)
lbl_waiting = QtBind.createLabel(gui, '<font color="#00CC44">No</font>', 115, _y + 3)

_y += 22
QtBind.createLabel(gui, '<font color="#FFAA00"><b>Area blocked moves:</b></font>', 8, _y + 3)
lbl_blocked_count = QtBind.createLabel(gui, '<font color="#FFFFFF">0</font>', 125, _y + 3)

# ==================== HELPERS ====================
def get_config_path():
    char = get_character_data()
    if char and char.get('name') and char.get('server'):
        return os.path.join(config_dir, f"{char['server']}_{char['name']}.json")
    return os.path.join(config_dir, "default.json")

def save_config():
    try:
        os.makedirs(config_dir, exist_ok=True)
        data = {
            'level_entries':       level_entries,
            'plugin_enabled':      plugin_enabled,
            'party_wait':          QtBind.isChecked(gui, chk_party_wait),
            'move_away':           QtBind.isChecked(gui, chk_move_away),
            'current_script_idx':  current_script_idx,
        }
        with open(get_config_path(), 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log(f"[AutoPLevel] save_config error: {e}")

def load_config():
    global level_entries, plugin_enabled, current_script_idx, last_applied_idx
    try:
        path = get_config_path()
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            level_entries      = data.get('level_entries', [])
            current_script_idx = data.get('current_script_idx', 0)
            last_applied_idx   = -1
            party_wait = data.get('party_wait', True)
            move_away  = data.get('move_away',  True)
            QtBind.setChecked(gui, chk_party_wait, party_wait)
            QtBind.setChecked(gui, chk_move_away,  move_away)
            was_enabled = data.get('plugin_enabled', False)
            if was_enabled:
                log("[AutoPLevel] Was enabled → auto-resuming...")
                start_plugin()
            refresh_entries_list()
    except Exception as e:
        log(f"[AutoPLevel] load_config error: {e}")

def refresh_entries_list():
    try:
        QtBind.clear(gui, entries_list)
        for i, entry in enumerate(level_entries):
            marker = " ◀ ACTIVE" if i == current_script_idx and plugin_enabled else ""
            QtBind.append(gui, entries_list, f"[{i+1}] Script: {entry['script']}  👺  Until Level {entry['target_level']}{marker}")
    except Exception as e:
        log(f"[AutoPLevel] refresh_entries_list error: {e}")

def refresh_scripts_dropdown():
    try:
        QtBind.clear(gui, dropdown_script)
        if not os.path.exists(scripts_dir):
            os.makedirs(scripts_dir, exist_ok=True)
        for f in os.listdir(scripts_dir):
            if f.endswith('.txt'):
                QtBind.append(gui, dropdown_script, f)
    except Exception as e:
        log(f"[AutoPLevel] refresh_scripts error: {e}")

def update_status_labels():
    try:
        char = get_character_data()
        my_level = char['level'] if char else 0
        QtBind.setText(gui, lbl_my_level, f'<font color="#00FF88">{my_level}</font>')

        # Party min level
        party = get_party()
        if party:
            levels = [m['level'] for m in party.values() if m.get('level', 0) > 0]
            levels.append(my_level)
            min_lvl = min(levels) if levels else my_level
            QtBind.setText(gui, lbl_party_level, f'<font color="#00FF88">{min_lvl}</font>')
        else:
            QtBind.setText(gui, lbl_party_level, f'<font color="#00FF88">{my_level}</font>')

        # Current script
        if level_entries and 0 <= current_script_idx < len(level_entries):
            script_name = level_entries[current_script_idx]['script']
            QtBind.setText(gui, lbl_current_script, f'<font color="#00DDFF">{script_name}</font>')
        else:
            QtBind.setText(gui, lbl_current_script, '<font color="#AAAAAA">-</font>')

        if waiting_party:
            QtBind.setText(gui, lbl_waiting, '<font color="#FFAA00">Yes ⏳</font>')
        else:
            QtBind.setText(gui, lbl_waiting, '<font color="#00CC44">No</font>')

        QtBind.setText(gui, lbl_blocked_count, f'<font color="#FF8800">{area_blocked_count}</font>')
    except Exception as e:
        pass  # silently fail on GUI updates

# ==================== CORE LOGIC ====================
def get_effective_level():
    """
    يرجع الـ level الفعلي حسب الـ settings:
    - لو party wait مفعّل وفي party → يرجع أقل level في الـ party
    - غير كده → level الشخصية الحالية
    """
    char = get_character_data()
    my_level = char['level'] if char else 0

    if not QtBind.isChecked(gui, chk_party_wait):
        return my_level

    party = get_party()
    if not party:
        return my_level

    levels = [m['level'] for m in party.values() if m.get('level', 0) > 0]
    levels.append(my_level)
    return min(levels)

def preprocess_script(script_path):
    """
    يقرأ الـ script ويعمل نسخة مؤقتة فيها walk من الـ current position
    قبل أي سطر return أو reverse — عشان phBot ما يتعطلش.
    بيرجع مسار الـ script المعدّل.
    """
    try:
        char = get_character_data()
        cx = char.get('x', 0) if char else 0
        cy = char.get('y', 0) if char else 0
        cz = char.get('z', 0) if char else 0

        with open(script_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        new_lines = []
        last_walk = None  # آخر walk شفناه في الـ script

        for line in lines:
            stripped = line.strip().lower()

            if stripped.startswith('return') or stripped.startswith('reverse'):
                # حدد الـ walk اللي هنحطها قبل الـ return/reverse
                if last_walk:
                    # كرّر آخر walk في الـ script
                    new_lines.append(last_walk)
                else:
                    # مفيش walk في الـ script قبلها → استخدم الـ position الحالي
                    walk_line = f'walk,{int(cx)},{int(cy)},{int(cz)}\n'
                    new_lines.append(walk_line)

            new_lines.append(line)

            # تتبع آخر walk
            if line.strip().lower().startswith('walk,'):
                last_walk = line if line.endswith('\n') else line + '\n'

        # احفظ الـ script المعدّل في temp folder
        temp_dir = os.path.join(plugin_dir, '_temp_scripts')
        os.makedirs(temp_dir, exist_ok=True)
        base_name = os.path.basename(script_path)
        temp_path = os.path.join(temp_dir, base_name)

        with open(temp_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)

        return temp_path

    except Exception as e:
        log(f"[AutoPLevel] preprocess_script error: {e}")
        return script_path  # fallback على الـ original لو في error

def apply_script(idx):
    """يطبق الـ script الموجود في الـ level_entries[idx]"""
    global last_applied_idx
    if idx < 0 or idx >= len(level_entries):
        return
    entry = level_entries[idx]
    script_path = os.path.join(scripts_dir, entry['script'])
    if not os.path.exists(script_path):
        log(f"[AutoPLevel] Script not found: {script_path}")
        return
    try:
        stop_bot()
    except: pass

    # preprocess: حط walk قبل أي return/reverse
    processed_path = preprocess_script(script_path)

    set_training_script(processed_path)
    start_bot()
    last_applied_idx = idx
    log(f"[AutoPLevel] Script applied: {entry['script']}  (target level: {entry['target_level']})")
    refresh_entries_list()

def check_area_for_players():
    """
    يتحقق إذا في players في نفس منطقة الـ training area
    لو في ناس → يعمل move لمكان تاني
    """
    global move_cooldown_until, area_blocked_count, move_check_enabled

    if not move_check_enabled:
        return
    if time.time() < move_cooldown_until:
        return

    try:
        training = get_training_area()
        if not training:
            return

        tx = training.get('x', 0)
        ty = training.get('y', 0)
        radius = training.get('radius', 50.0)

        # جيب الـ players القريبين
        players = get_players()
        if not players:
            return

        # تحقق من وجود players داخل نطاق الـ training area
        blocked = False
        for pid, pdata in players.items():
            px = pdata.get('x', 0)
            py = pdata.get('y', 0)
            dist = ((px - tx)**2 + (py - ty)**2) ** 0.5
            if dist <= radius + 20:  # +20 margin
                blocked = True
                break

        if not blocked:
            return

        # ===================== AREA IS BLOCKED =====================
        area_blocked_count += 1
        log(f"[AutoPLevel] Area occupied by players! (#{area_blocked_count}) Looking for free spot...")

        # ابحث عن مكان فيه نفس الـ mobs بس فاضي
        free_spot = find_free_spot_same_mobs(tx, ty, radius, players)

        if free_spot:
            nx, ny = free_spot
            log(f"[AutoPLevel] Moving to free spot: ({nx:.0f}, {ny:.0f})")
            try:
                stop_bot()
            except: pass
            move_to(nx, ny, 0)
            # بعد الـ move، ضبط الـ training area على المكان الجديد
            threading.Timer(3.0, lambda: _update_training_area_after_move(nx, ny, training)).start()
            move_cooldown_until = time.time() + 30  # cooldown 30 ثانية
        else:
            log("[AutoPLevel] No free spot found nearby, staying...")
            move_cooldown_until = time.time() + 15

    except Exception as e:
        log(f"[AutoPLevel] check_area_for_players error: {e}")

def _update_training_area_after_move(nx, ny, old_training):
    """بعد الـ move يضبط الـ training area ويشغل البوت"""
    try:
        region = old_training.get('region', 0)
        radius = old_training.get('radius', 50.0)
        set_training_position(region, nx, ny, radius)
        start_bot()
        log(f"[AutoPLevel] Training area updated to free spot.")
        QtBind.setText(gui, lbl_blocked_count, str(area_blocked_count))
    except Exception as e:
        log(f"[AutoPLevel] _update_training_area_after_move error: {e}")

def find_free_spot_same_mobs(cx, cy, radius, players):
    """
    يبحث عن spot بعيد عن الـ players بـ offset مختلفة
    بيحاول اتجاهات مختلفة حتى يلاقي مكان فيه مobs وما فيهوش players
    """
    offsets = [
        (200, 0), (-200, 0), (0, 200), (0, -200),
        (150, 150), (-150, 150), (150, -150), (-150, -150),
        (300, 0), (-300, 0), (0, 300), (0, -300),
    ]

    for dx, dy in offsets:
        nx = cx + dx
        ny = cy + dy

        # تحقق ما فيهوش players قريبين
        occupied = False
        for pid, pdata in players.items():
            px = pdata.get('x', 0)
            py = pdata.get('y', 0)
            dist = ((px - nx)**2 + (py - ny)**2) ** 0.5
            if dist <= radius + 30:
                occupied = True
                break

        if not occupied:
            return (nx, ny)

    return None

def party_check_loop():
    """
    Loop مستقل بيشتغل كل 5 ثواني خصيصاً لمراقبة الـ party.
    بيكتشف لو حد جديد دخل الـ party متأخر ويعمل re-evaluate
    للـ waiting state حتى لو الـ main tick مش شايله.
    """
    global waiting_party, last_applied_idx, _last_party_ids, party_loop_timer

    if not plugin_enabled:
        return

    try:
        if QtBind.isChecked(gui, chk_party_wait):
            party = get_party()
            current_ids = set(party.keys()) if party else set()
            char = get_character_data()
            my_level = char['level'] if char else 0

            if current_script_idx < len(level_entries):
                target_level = level_entries[current_script_idx]['target_level']

                # اكتشف أعضاء جدد دخلوا الـ party
                new_members = current_ids - _last_party_ids
                if new_members and _last_party_ids:
                    pass  # عضو جديد دخل، هنتحقق منه تلقائياً

                # تحقق من كل أعضاء الـ party الحاليين
                if party:
                    still_waiting = []
                    for mid, mdata in party.items():
                        mlevel = mdata.get('level', 0)
                        mname  = mdata.get('name', '?')
                        if mlevel > 0 and mlevel < target_level:
                            still_waiting.append(f"{mname}(lv{mlevel})")

                    if still_waiting:
                        # في ناس لسه ما وصلوش → ابقى waiting
                        if not waiting_party:
                            waiting_party = True
                    else:
                        # كل الـ party وصلت أو مفيش حد ناقص
                        if waiting_party:
                            # كنا waiting → دلوقتي كل الناس وصلت
                            if my_level >= target_level:
                                waiting_party = False
                                log(f"[AutoPLevel] ✅ All party members reached level {target_level}! Advancing...")
                                advance_to_next_script()
                        else:
                            # مش waiting بس ممكن الـ main tick فاته → نتحقق هنا كمان
                            if my_level >= target_level:
                                advance_to_next_script()
                else:
                    # مفيش party دلوقتي
                    if waiting_party:
                        waiting_party = False

            _last_party_ids = current_ids

    except Exception as e:
        log(f"[AutoPLevel] party_check_loop error: {e}")

    # Schedule next party check
    party_loop_timer = threading.Timer(5.0, party_check_loop)
    party_loop_timer.daemon = True
    party_loop_timer.start()

def main_check_tick():
    """الـ main loop بيشتغل كل ثانية"""
    global current_script_idx, last_applied_idx, waiting_party, check_timer

    if not plugin_enabled:
        return

    try:
        # ---- تحديث الـ GUI labels ----
        update_status_labels()

        # ---- تحقق من الـ level ----
        if not level_entries:
            schedule_next_tick()
            return

        # لو وصلنا آخر entry → وقفنا
        if current_script_idx >= len(level_entries):
            log("[AutoPLevel] All levels done! Plugin stopping.")
            stop_plugin()
            return

        entry        = level_entries[current_script_idx]
        target_level = entry['target_level']
        char         = get_character_data()
        my_level     = char['level'] if char else 0

        # ---- طبّق الـ script لو لسه ما اتطبقش ----
        if last_applied_idx != current_script_idx:
            apply_script(current_script_idx)

        # ---- تحقق لو وصلنا الـ target level ----
        effective_level = get_effective_level()

        if effective_level >= target_level:
            # وصلنا الـ target → ننتقل للـ entry التالي
            party_wait = QtBind.isChecked(gui, chk_party_wait)
            party      = get_party()

            if party_wait and party:
                # تحقق من الـ party members
                waiting_for = []
                for member_id, mdata in party.items():
                    mlevel = mdata.get('level', 0)
                    mname  = mdata.get('name', '?')
                    if mlevel > 0 and mlevel < target_level:
                        waiting_for.append(f"{mname}(lv{mlevel})")

                if waiting_for:
                    # في ناس لسه ما وصلوش
                    waiting_party = True
                    log(f"[AutoPLevel] My level {my_level} ≥ {target_level} ✓  Waiting for party: {', '.join(waiting_for)}")
                    schedule_next_tick()
                    return
                else:
                    # كل الـ party وصلت
                    waiting_party = False
                    log(f"[AutoPLevel] All party members reached level {target_level}! Moving to next script...")
                    advance_to_next_script()
            else:
                # مفيش party أو party wait مطفي
                waiting_party = False
                log(f"[AutoPLevel] Reached level {target_level}! Moving to next script...")
                advance_to_next_script()
        else:
            waiting_party = False

        # ---- تحقق من الـ area ----
        if QtBind.isChecked(gui, chk_move_away):
            check_area_for_players()

    except Exception as e:
        log(f"[AutoPLevel] main_check_tick error: {e}")

    schedule_next_tick()

def schedule_next_tick():
    global check_timer
    check_timer = threading.Timer(1.0, main_check_tick)
    check_timer.daemon = True
    check_timer.start()

def advance_to_next_script():
    global current_script_idx, last_applied_idx
    current_script_idx += 1
    last_applied_idx    = -1
    save_config()
    refresh_entries_list()

    if current_script_idx >= len(level_entries):
        log("[AutoPLevel] 🎉 All level scripts completed!")
        stop_plugin()
    else:
        next_entry = level_entries[current_script_idx]
        log(f"[AutoPLevel] Next script: {next_entry['script']} (until level {next_entry['target_level']})")
        apply_script(current_script_idx)

# ==================== PLUGIN START/STOP ====================
def start_plugin():
    global plugin_enabled, last_applied_idx, check_timer, party_loop_timer, _last_party_ids
    if plugin_enabled:
        return
    plugin_enabled  = True
    last_applied_idx = -1
    _last_party_ids  = set()
    save_config()
    QtBind.setText(gui, lbl_status, '<font color="#00CC44"><b>● Status: ON ✓</b></font>')
    log(f"[AutoPLevel] Plugin ENABLED (entry {current_script_idx+1}/{len(level_entries)})")
    if check_timer:
        check_timer.cancel()
    if party_loop_timer:
        party_loop_timer.cancel()
    schedule_next_tick()
    # شغّل الـ party check loop المستقل
    party_loop_timer = threading.Timer(5.0, party_check_loop)
    party_loop_timer.daemon = True
    party_loop_timer.start()

def stop_plugin():
    global plugin_enabled, check_timer, waiting_party, party_loop_timer, _last_party_ids
    plugin_enabled = False
    waiting_party  = False
    _last_party_ids = set()
    if check_timer:
        check_timer.cancel()
        check_timer = None
    if party_loop_timer:
        party_loop_timer.cancel()
        party_loop_timer = None
    try:
        stop_bot()
    except: pass
    save_config()
    QtBind.setText(gui, lbl_status, '<font color="#FF4444"><b>● Status: OFF</b></font>')
    QtBind.setText(gui, lbl_waiting, '<font color="#00CC44">No</font>')
    refresh_entries_list()
    cleanup_temp_scripts()
    log("[AutoPLevel] Plugin DISABLED")

def cleanup_temp_scripts():
    """يمسح الـ temp scripts المعدّلة"""
    try:
        temp_dir = os.path.join(plugin_dir, '_temp_scripts')
        if os.path.exists(temp_dir):
            import shutil
            shutil.rmtree(temp_dir)
            log("[AutoPLevel] Temp scripts cleaned.")
    except Exception as e:
        log(f"[AutoPLevel] cleanup_temp_scripts error: {e}")

# ==================== GUI CALLBACKS ====================
def btn_enable_clicked():
    if not level_entries:
        log("[AutoPLevel] No level entries configured! Add scripts first.")
        return
    start_plugin()

def btn_disable_clicked():
    stop_plugin()

def chk_party_wait_changed(checked=None):
    save_config()
    state = "ON" if checked else "OFF"
    log(f"[AutoPLevel] Party wait: {state}")

def chk_move_away_changed(checked=None):
    global move_check_enabled
    move_check_enabled = bool(checked)
    save_config()
    state = "ON" if checked else "OFF"
    log(f"[AutoPLevel] Move away: {state}")

def btn_refresh_scripts_clicked():
    refresh_scripts_dropdown()
    log("[AutoPLevel] Scripts refreshed.")

def btn_add_entry_clicked():
    try:
        script = QtBind.text(gui, dropdown_script).strip()
        if not script:
            log("[AutoPLevel] Select a script first.")
            return
        try:
            target = int(QtBind.text(gui, txt_target_level).strip())
        except:
            log("[AutoPLevel] Invalid target level.")
            return
        level_entries.append({'script': script, 'target_level': target})
        save_config()
        refresh_entries_list()
        log(f"[AutoPLevel] Added: {script} → until level {target}")
    except Exception as e:
        log(f"[AutoPLevel] btn_add_entry error: {e}")

def btn_delete_entry_clicked():
    global current_script_idx, last_applied_idx
    try:
        selected = QtBind.text(gui, entries_list)
        if not selected:
            return
        # parse index from "[N] Script: ..."
        idx = int(selected.split(']')[0].replace('[', '').strip()) - 1
        if 0 <= idx < len(level_entries):
            removed = level_entries.pop(idx)
            if current_script_idx >= len(level_entries) and level_entries:
                current_script_idx = len(level_entries) - 1
            last_applied_idx = -1
            save_config()
            refresh_entries_list()
            log(f"[AutoPLevel] Removed: {removed['script']}")
    except Exception as e:
        log(f"[AutoPLevel] btn_delete_entry error: {e}")

def btn_move_up_clicked():
    global current_script_idx, last_applied_idx
    try:
        selected = QtBind.text(gui, entries_list)
        if not selected:
            return
        idx = int(selected.split(']')[0].replace('[', '').strip()) - 1
        if idx > 0:
            level_entries[idx], level_entries[idx-1] = level_entries[idx-1], level_entries[idx]
            last_applied_idx = -1
            save_config()
            refresh_entries_list()
    except Exception as e:
        log(f"[AutoPLevel] btn_move_up error: {e}")

def btn_move_down_clicked():
    global current_script_idx, last_applied_idx
    try:
        selected = QtBind.text(gui, entries_list)
        if not selected:
            return
        idx = int(selected.split(']')[0].replace('[', '').strip()) - 1
        if idx < len(level_entries) - 1:
            level_entries[idx], level_entries[idx+1] = level_entries[idx+1], level_entries[idx]
            last_applied_idx = -1
            save_config()
            refresh_entries_list()
    except Exception as e:
        log(f"[AutoPLevel] btn_move_down error: {e}")

# ==================== PHBOT EVENTS ====================
def joined_game():
    """بيتنادى لما الشخصية تدخل اللعبة"""
    log(f"[AutoPLevel] Joined game — loading config...")
    refresh_scripts_dropdown()
    load_config()

def teleported():
    """بيتنادى بعد كل teleport"""
    pass

# ==================== INIT ====================
try:
    os.makedirs(scripts_dir,  exist_ok=True)
    os.makedirs(config_dir,   exist_ok=True)
    refresh_scripts_dropdown()
    # لو الشخصية دخلت بالفعل → حمّل الـ config
    char = get_character_data()
    if char and char.get('name') and char.get('server'):
        load_config()
        log(f"[{pName}] v{pVersion} loaded (char: {char['name']}).")
    else:
        log(f"[{pName}] v{pVersion} loaded.")
except Exception as e:
    log(f"[{pName}] Init error: {e}")
