# -*- coding: utf-8 -*-
"""
Daptar Sync - همگام‌ساز خودکار S3
نیازمندی‌ها:
    pip install boto3 pystray Pillow cryptography
"""

import os
import sys
import json
import time
import shutil
import socket
import threading
import datetime
import logging
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, BotoCoreError
import pystray
from PIL import Image, ImageDraw
from cryptography.fernet import Fernet

APP_NAME = "Daptar Sync"
APP_ID = "DaptarSync"

# ================== مسیرها ==================
def get_app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def get_resource_path(name):
    p = os.path.join(get_app_dir(), name)
    if os.path.exists(p):
        return p
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, name)
    return p

APP_DIR = get_app_dir()
CONFIG_FILE = os.path.join(APP_DIR, 'config.enc')
KEY_FILE = os.path.join(APP_DIR, 'secret.key')
LOG_FILE = os.path.join(APP_DIR, 'daptarsync.log')
ICON_FILE = get_resource_path('icon.png')

RECYCLE_DIR_NAME = '.recycle_bin'
S3_RECYCLE_PREFIX = '.recycle_bin/'
STATE_FILE_NAME = '.sync_state.json'
RECYCLE_RETENTION_DAYS = 14
LOCK_PORT = 51724

# ================== قفل تک‌نمونه ==================
_lock_socket = None

def acquire_single_instance():
    global _lock_socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        s.bind(('127.0.0.1', LOCK_PORT))
        s.listen(1)
        _lock_socket = s
        return True
    except OSError:
        return False

def signal_existing_instance():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(('127.0.0.1', LOCK_PORT))
        s.sendall(b'SHOW')
        s.close()
        return True
    except OSError:
        return False

def start_signal_listener(app):
    def listener():
        while _lock_socket:
            try:
                conn, _ = _lock_socket.accept()
                data = conn.recv(16)
                if data == b'SHOW':
                    app.root.after(0, lambda: (app.root.deiconify(), app.root.lift()))
                conn.close()
            except OSError:
                break
            except Exception:
                continue
    threading.Thread(target=listener, daemon=True).start()

# ================== لاگ ==================
logger = logging.getLogger('daptarsync')
logger.setLevel(logging.INFO)
try:
    _fh = logging.FileHandler(LOG_FILE, encoding='utf-8')
    _fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(_fh)
except Exception:
    pass

_log_callback = None

def log(msg, level='info'):
    try:
        if level == 'error':
            logger.error(msg)
        elif level == 'warning':
            logger.warning(msg)
        else:
            logger.info(msg)
    except Exception:
        pass
    if _log_callback:
        ts = datetime.datetime.now().strftime('%H:%M:%S')
        try:
            _log_callback(f"[{ts}] {msg}")
        except Exception:
            pass

# ================== میان‌برهای Entry (کار با هر چیدمان کیبورد) ==================
def add_entry_shortcuts(widget):
    """Ctrl+V/C/X/A با استفاده از keycode (مستقل از زبان کیبورد) + منوی راست‌کلیک"""
    def on_ctrl(event):
        kc = event.keycode
        if kc == 86:      # V - Paste
            widget.event_generate('<<Paste>>')
            return 'break'
        if kc == 67:      # C - Copy
            widget.event_generate('<<Copy>>')
            return 'break'
        if kc == 88:      # X - Cut
            widget.event_generate('<<Cut>>')
            return 'break'
        if kc == 65:      # A - Select All
            try:
                widget.selection_range(0, 'end')
                widget.icursor('end')
            except Exception:
                try:
                    widget.tag_add(tk.SEL, "1.0", tk.END)
                    widget.mark_set(tk.INSERT, "1.0")
                    widget.see(tk.INSERT)
                except Exception:
                    pass
            return 'break'
        return None

    widget.bind('<Control-KeyPress>', on_ctrl, add='+')
    widget.bind('<Shift-Insert>', lambda e: (widget.event_generate('<<Paste>>'), 'break')[1], add='+')

    # --- منوی راست‌کلیک ---
    menu = tk.Menu(widget, tearoff=0)
    menu.add_command(label="Cut", command=lambda: widget.event_generate('<<Cut>>'))
    menu.add_command(label="Copy", command=lambda: widget.event_generate('<<Copy>>'))
    menu.add_command(label="Paste", command=lambda: widget.event_generate('<<Paste>>'))
    menu.add_separator()

    def select_all():
        try:
            widget.selection_range(0, 'end')
            widget.icursor('end')
        except Exception:
            try:
                widget.tag_add(tk.SEL, "1.0", tk.END)
                widget.mark_set(tk.INSERT, "1.0")
            except Exception:
                pass

    menu.add_command(label="Select All", command=select_all)

    def show_menu(event):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    widget.bind('<Button-3>', show_menu)

# ================== تنظیم آیکون پنجره ==================
def set_window_icon(root):
    """تنظیم آیکون نوار عنوان پنجره"""
    try:
        ico_path = get_resource_path('icon.ico')
        if os.path.exists(ico_path):
            root.iconbitmap(default=ico_path)
            return
    except Exception:
        pass
    try:
        png_path = get_resource_path('icon.png')
        if os.path.exists(png_path):
            img = tk.PhotoImage(file=png_path)
            root.iconphoto(True, img)
            root._icon_photo = img
    except Exception:
        pass

# ================== رمزنگاری ==================
def get_fernet():
    if not os.path.exists(KEY_FILE):
        with open(KEY_FILE, 'wb') as f:
            f.write(Fernet.generate_key())
    with open(KEY_FILE, 'rb') as f:
        return Fernet(f.read())

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, 'rb') as f:
            return json.loads(get_fernet().decrypt(f.read()).decode('utf-8'))
    except Exception as e:
        log(f"خطا در خواندن تنظیمات: {e}", 'error')
        return {}

def save_config(config):
    try:
        data = json.dumps(config, ensure_ascii=False).encode('utf-8')
        with open(CONFIG_FILE, 'wb') as f:
            f.write(get_fernet().encrypt(data))
    except Exception as e:
        log(f"خطا در ذخیره تنظیمات: {e}", 'error')

# ================== S3 ==================
def create_s3_client(config):
    endpoint = (config.get('endpoint') or '').strip().rstrip('/')
    bucket = (config.get('bucket') or '').strip()

    # اگر پروتکل وارد نشده باشد، https:// اضافه کن
    if endpoint and not endpoint.startswith(('http://', 'https://')):
        endpoint = 'https://' + endpoint

    if endpoint and bucket:
        for prefix in (f"https://{bucket}.", f"http://{bucket}."):
            if endpoint.startswith(prefix):
                endpoint = prefix[:prefix.index('//') + 2] + endpoint[len(prefix):]
                break
        else:
            for suffix in (f"/{bucket}", f"/{bucket}/"):
                if endpoint.endswith(suffix):
                    endpoint = endpoint[: -len(suffix)]
                    break

    boto_cfg = BotoConfig(
        retries={'max_attempts': 5, 'mode': 'adaptive'},
        connect_timeout=30, read_timeout=120, max_pool_connections=20,
        s3={'addressing_style': 'path'},
    )
    return boto3.client(
        's3',
        endpoint_url=endpoint or None,
        aws_access_key_id=config.get('access_key'),
        aws_secret_access_key=config.get('secret_key'),
        config=boto_cfg,
    )

# ================== پیمایش ==================
def list_local_files(local_folder):
    files = {}
    empty_dirs = set()
    for root, dirs, filenames in os.walk(local_folder):
        if RECYCLE_DIR_NAME in dirs:
            dirs.remove(RECYCLE_DIR_NAME)
        rel_root = os.path.relpath(root, local_folder).replace('\\', '/')
        if rel_root == '.':
            rel_root = ''
        if not filenames and not dirs and rel_root:
            empty_dirs.add(rel_root + '/')
        for filename in filenames:
            if filename == STATE_FILE_NAME:
                continue
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, local_folder).replace('\\', '/')
            try:
                st = os.stat(full_path)
            except OSError:
                continue
            files[rel_path] = {'size': st.st_size, 'mtime': st.st_mtime,
                               'full_path': full_path, 'is_dir': False}
    for d in empty_dirs:
        files[d] = {'size': 0, 'mtime': 0, 'full_path': None, 'is_dir': True}
    return files

def list_s3_objects(s3, bucket):
    objects = {}
    paginator = s3.get_paginator('list_objects_v2')
    try:
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.startswith(S3_RECYCLE_PREFIX):
                    continue
                objects[key] = {
                    'size': obj['Size'],
                    'mtime': obj['LastModified'].timestamp(),
                    'is_dir': key.endswith('/') and obj['Size'] == 0,
                }
    except (ClientError, BotoCoreError) as e:
        log(f"خطا در لیست کردن S3: {e}", 'error')
    return objects

# ================== عملیات ==================
def safe_upload(s3, bucket, local_path, key):
    try:
        s3.upload_file(local_path, bucket, key)
        log(f"آپلود: {key}")
        return True
    except Exception as e:
        log(f"خطا در آپلود {key}: {e}", 'error')
        return False

def safe_download(s3, bucket, key, local_path):
    try:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        s3.download_file(bucket, key, local_path)
        log(f"دانلود: {key}")
        return True
    except Exception as e:
        log(f"خطا در دانلود {key}: {e}", 'error')
        return False

def safe_create_folder_marker(s3, bucket, key):
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=b'')
        log(f"ایجاد نشانگر پوشه: {key}")
        return True
    except Exception as e:
        log(f"خطا در ساخت نشانگر {key}: {e}", 'error')
        return False

def move_local_to_recycle(local_folder, rel_path):
    full_path = os.path.join(local_folder, rel_path)
    if not os.path.exists(full_path) and not os.path.isdir(full_path):
        return
    recycle_dir = os.path.join(local_folder, RECYCLE_DIR_NAME)
    os.makedirs(recycle_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    dest = os.path.join(recycle_dir, f"{ts}__{rel_path.replace('/', '__')}")
    try:
        shutil.move(full_path, dest)
        log(f"بازیافت محلی: {rel_path}")
    except Exception as e:
        log(f"خطا در انتقال به بازیافت {rel_path}: {e}", 'error')

def move_s3_to_recycle(s3, bucket, key):
    rkey = f"{S3_RECYCLE_PREFIX}{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}__{key.replace('/', '__')}"
    try:
        s3.copy_object(Bucket=bucket, CopySource={'Bucket': bucket, 'Key': key}, Key=rkey)
        s3.delete_object(Bucket=bucket, Key=key)
        log(f"بازیافت S3: {key}")
    except Exception as e:
        log(f"خطا در انتقال به بازیافت S3 {key}: {e}", 'error')

def cleanup_local_recycle(local_folder):
    d = os.path.join(local_folder, RECYCLE_DIR_NAME)
    if not os.path.exists(d):
        return
    now = time.time()
    for name in os.listdir(d):
        p = os.path.join(d, name)
        try:
            if os.path.isfile(p) and now - os.path.getmtime(p) > RECYCLE_RETENTION_DAYS * 86400:
                os.remove(p)
                log(f"حذف دائمی محلی: {name}")
        except OSError:
            pass

def cleanup_s3_recycle(s3, bucket):
    paginator = s3.get_paginator('list_objects_v2')
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=S3_RECYCLE_PREFIX):
            for obj in page.get('Contents', []):
                if (now - obj['LastModified']).days > RECYCLE_RETENTION_DAYS:
                    s3.delete_object(Bucket=bucket, Key=obj['Key'])
                    log(f"حذف دائمی S3: {obj['Key']}")
    except Exception as e:
        log(f"خطا در پاکسازی S3: {e}", 'error')

# ================== همگام‌سازی ==================
stop_event = threading.Event()
progress_callback = None

def sync(config):
    if not config.get('bucket') or not config.get('local_folder'):
        log("تنظیمات ناقص است.", 'error')
        return
    if not os.path.isdir(config['local_folder']):
        log(f"پوشه محلی وجود ندارد: {config['local_folder']}", 'error')
        return

    s3 = create_s3_client(config)
    bucket = config['bucket']
    local_folder = config['local_folder']
    direction = config.get('direction', 'bidirectional')

    log("=== شروع همگام‌سازی ===")
    try:
        local_files = list_local_files(local_folder)
        remote_files = list_s3_objects(s3, bucket)

        state_file = os.path.join(local_folder, STATE_FILE_NAME)
        state = {}
        first_run = not os.path.exists(state_file)
        if not first_run:
            try:
                with open(state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
            except Exception:
                state = {}

        if first_run:
            log("اجرای اول - فقط انتقال فایل‌ها بدون حذف.")

        all_keys = set(local_files) | set(remote_files)
        total = len(all_keys)
        done = up = down = rec = 0

        for key in all_keys:
            if stop_event.is_set():
                log("متوقف شد.")
                break
            done += 1
            if progress_callback:
                progress_callback(done, total)

            L = local_files.get(key)
            R = remote_files.get(key)
            P = state.get(key)

            if L and R:
                if L.get('is_dir') and R.get('is_dir'):
                    continue
                lm, rm = L['mtime'], R['mtime']
                if abs(lm - rm) < 2:
                    continue
                if P and not first_run:
                    pl = P.get('local_mtime') or 0
                    pr = P.get('remote_mtime') or 0
                    if lm > pl + 1 and rm > pr + 1:
                        log(f"تعارض: {key}")
                        if direction == 'local_to_s3':
                            move_s3_to_recycle(s3, bucket, key)
                            safe_upload(s3, bucket, L['full_path'], key); up += 1
                        elif direction == 's3_to_local':
                            move_local_to_recycle(local_folder, key)
                            safe_download(s3, bucket, key, L['full_path']); down += 1
                        else:
                            if lm > rm:
                                move_s3_to_recycle(s3, bucket, key)
                                safe_upload(s3, bucket, L['full_path'], key); up += 1
                            else:
                                move_local_to_recycle(local_folder, key)
                                safe_download(s3, bucket, key, L['full_path']); down += 1
                        continue
                if lm > rm:
                    if direction in ('bidirectional', 'local_to_s3'):
                        if safe_upload(s3, bucket, L['full_path'], key):
                            up += 1
                else:
                    if direction in ('bidirectional', 's3_to_local'):
                        if safe_download(s3, bucket, key, L['full_path']):
                            down += 1

            elif L:
                if first_run or not P:
                    if direction in ('bidirectional', 'local_to_s3'):
                        if L.get('is_dir'):
                            if safe_create_folder_marker(s3, bucket, key):
                                up += 1
                        else:
                            if safe_upload(s3, bucket, L['full_path'], key):
                                up += 1
                else:
                    if direction == 'bidirectional':
                        move_local_to_recycle(local_folder, key); rec += 1
                    elif direction == 'local_to_s3':
                        if L.get('is_dir'):
                            safe_create_folder_marker(s3, bucket, key)
                        else:
                            safe_upload(s3, bucket, L['full_path'], key); up += 1

            elif R:
                if first_run or not P:
                    if direction in ('bidirectional', 's3_to_local'):
                        if R.get('is_dir'):
                            try:
                                os.makedirs(os.path.join(local_folder, key), exist_ok=True)
                                log(f"پوشه: {key}")
                            except OSError:
                                pass
                        else:
                            if safe_download(s3, bucket, key, os.path.join(local_folder, key)):
                                down += 1
                else:
                    if direction == 'bidirectional':
                        move_s3_to_recycle(s3, bucket, key); rec += 1
                    elif direction == 's3_to_local':
                        if R.get('is_dir'):
                            try:
                                os.makedirs(os.path.join(local_folder, key), exist_ok=True)
                            except OSError:
                                pass
                        else:
                            if safe_download(s3, bucket, key, os.path.join(local_folder, key)):
                                down += 1

        new_state = {}
        for k in set(local_files) | set(remote_files):
            l = local_files.get(k); r = remote_files.get(k)
            new_state[k] = {
                'local_mtime': l['mtime'] if l else None,
                'remote_mtime': r['mtime'] if r else None,
            }
        try:
            with open(state_file, 'w', encoding='utf-8') as f:
                json.dump(new_state, f, ensure_ascii=False)
        except OSError as e:
            log(f"خطا در ذخیره state: {e}", 'warning')

        cleanup_local_recycle(local_folder)
        cleanup_s3_recycle(s3, bucket)

        log(f"=== پایان: {up} آپلود، {down} دانلود، {rec} بازیافت ===")
    except Exception as e:
        log(f"خطای غیرمنتظره: {e}", 'error')

# ================== GUI ==================
class SyncApp:
    def __init__(self, root):
        global _log_callback, progress_callback
        _log_callback = self.append_log
        progress_callback = self.update_progress

        self.root = root
        self.root.title(APP_NAME)
        set_window_icon(self.root)
        self.root.geometry("840x640")
        self.root.minsize(760, 560)
        self.config = load_config()
        self.auto_sync = False
        self.auto_thread = None
        self.sync_thread = None
        self.tray_icon = None

        self.create_widgets()
        self.load_config_to_ui()
        self.setup_tray()
        self.check_autostart()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.root.after(800, self.maybe_start_auto)

    def maybe_start_auto(self):
        if self.config.get('auto_sync') and not self.auto_sync:
            log("شروع خودکار همگام‌سازی از اجرای قبلی...")
            self.start_auto()

    def create_widgets(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        form = ttk.LabelFrame(main, text="تنظیمات اتصال", padding=10)
        form.pack(fill=tk.X)

        rows = [
            ("Endpoint:", 'entry_endpoint', False),
            ("Access Key:",         'entry_access',   False),
            ("Secret Key:",         'entry_secret',   True),
            ("نام باکت:",           'entry_bucket',   False),
        ]
        for i, (label, attr, secret) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky=tk.W, pady=3)
            e = ttk.Entry(form, width=55, show="*" if secret else "")
            e.grid(row=i, column=1, pady=3, sticky=tk.EW, padx=(5, 0))
            add_entry_shortcuts(e)
            setattr(self, attr, e)

        ttk.Label(form, text="پوشه محلی:").grid(row=4, column=0, sticky=tk.W, pady=3)
        self.entry_folder = ttk.Entry(form, width=55)
        self.entry_folder.grid(row=4, column=1, pady=3, sticky=tk.EW, padx=(5, 0))
        add_entry_shortcuts(self.entry_folder)
        ttk.Button(form, text="انتخاب...", command=self.choose_folder).grid(row=4, column=2, padx=5)

        ttk.Label(form, text="جهت همگام‌سازی:").grid(row=5, column=0, sticky=tk.W, pady=3)
        self.combo_direction = ttk.Combobox(
            form, values=["bidirectional", "local_to_s3", "s3_to_local"],
            state="readonly", width=20)
        self.combo_direction.grid(row=5, column=1, pady=3, sticky=tk.W, padx=(5, 0))
        self.combo_direction.set("bidirectional")

        ttk.Label(form, text="فاصله زمانی (دقیقه):").grid(row=6, column=0, sticky=tk.W, pady=3)
        self.entry_interval = ttk.Entry(form, width=10)
        self.entry_interval.grid(row=6, column=1, pady=3, sticky=tk.W, padx=(5, 0))
        add_entry_shortcuts(self.entry_interval)
        self.entry_interval.insert(0, "10")

        self.var_autostart = tk.BooleanVar()
        ttk.Checkbutton(form, text="اجرای خودکار با ویندوز",
                        variable=self.var_autostart,
                        command=self.toggle_autostart).grid(
            row=7, column=0, columnspan=3, sticky=tk.W, pady=5)

        form.columnconfigure(1, weight=1)

        btns = ttk.Frame(main)
        btns.pack(fill=tk.X, pady=8)
        ttk.Button(btns, text="ذخیره تنظیمات", command=self.save_settings).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="همگام‌سازی دستی", command=self.sync_now).pack(side=tk.LEFT, padx=3)
        self.btn_auto = ttk.Button(btns, text="شروع خودکار", command=self.toggle_auto)
        self.btn_auto.pack(side=tk.LEFT, padx=3)
        self.btn_stop = ttk.Button(btns, text="توقف", command=self.stop_sync, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=3)

        pf = ttk.Frame(main)
        pf.pack(fill=tk.X, pady=(5, 0))
        self.progress = ttk.Progressbar(pf, mode='determinate')
        self.progress.pack(fill=tk.X, side=tk.LEFT, expand=True)
        self.lbl_progress = ttk.Label(pf, text="0/0", width=12)
        self.lbl_progress.pack(side=tk.LEFT, padx=5)

        ttk.Label(main, text="گزارش عملیات:").pack(anchor=tk.W, pady=(10, 0))
        self.log_text = scrolledtext.ScrolledText(main, height=14, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        add_entry_shortcuts(self.log_text)

    def append_log(self, msg):
        def _do():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    def update_progress(self, d, t):
        def _do():
            self.progress['maximum'] = max(t, 1)
            self.progress['value'] = d
            self.lbl_progress.config(text=f"{d}/{t}")
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    def choose_folder(self):
        f = filedialog.askdirectory()
        if f:
            self.entry_folder.delete(0, tk.END)
            self.entry_folder.insert(0, f)

    def load_config_to_ui(self):
        c = self.config
        for attr, key in [('entry_endpoint', 'endpoint'), ('entry_access', 'access_key'),
                          ('entry_secret', 'secret_key'), ('entry_bucket', 'bucket'),
                          ('entry_folder', 'local_folder')]:
            getattr(self, attr).delete(0, tk.END)
            getattr(self, attr).insert(0, c.get(key, ''))
        self.combo_direction.set(c.get('direction', 'bidirectional'))
        self.entry_interval.delete(0, tk.END)
        self.entry_interval.insert(0, str(c.get('sync_interval', 10)))

    def get_config_from_ui(self):
        try:
            iv = int(self.entry_interval.get() or 10)
        except ValueError:
            iv = 10
        return {
            'endpoint': self.entry_endpoint.get().strip(),
            'access_key': self.entry_access.get().strip(),
            'secret_key': self.entry_secret.get().strip(),
            'bucket': self.entry_bucket.get().strip(),
            'local_folder': self.entry_folder.get().strip(),
            'direction': self.combo_direction.get(),
            'sync_interval': iv,
            'autostart': self.var_autostart.get(),
            'auto_sync': self.auto_sync,
        }

    def save_settings(self):
        self.config = self.get_config_from_ui()
        save_config(self.config)
        log("تنظیمات ذخیره شد.")

    def sync_now(self):
        if self.sync_thread and self.sync_thread.is_alive():
            log("همگام‌سازی در حال اجراست.", 'warning'); return
        self.config = self.get_config_from_ui()
        save_config(self.config)
        stop_event.clear()
        self.btn_stop.config(state=tk.NORMAL)
        self.sync_thread = threading.Thread(target=self._run_sync, daemon=True)
        self.sync_thread.start()

    def _run_sync(self):
        try:
            sync(self.config)
        finally:
            try:
                self.root.after(0, lambda: self.btn_stop.config(state=tk.DISABLED))
            except Exception:
                pass

    def stop_sync(self):
        stop_event.set()
        log("درخواست توقف ارسال شد...")

    def start_auto(self):
        if self.auto_sync:
            return
        self.config = self.get_config_from_ui()
        if not (self.config['bucket'] and self.config['local_folder']):
            messagebox.showwarning("توجه", "ابتدا نام باکت و پوشه محلی را وارد کنید.")
            return
        self.auto_sync = True
        self.config['auto_sync'] = True
        save_config(self.config)
        self.btn_auto.config(text="توقف خودکار")
        stop_event.clear()
        self.auto_thread = threading.Thread(target=self._auto_loop, daemon=True)
        self.auto_thread.start()
        log(f"همگام‌سازی خودکار هر {self.config['sync_interval']} دقیقه شروع شد.")

    def stop_auto(self):
        self.auto_sync = False
        self.config = self.get_config_from_ui()
        self.config['auto_sync'] = False
        save_config(self.config)
        self.btn_auto.config(text="شروع خودکار")
        log("همگام‌سازی خودکار متوقف شد.")

    def toggle_auto(self):
        if self.auto_sync:
            self.stop_auto()
        else:
            self.start_auto()

    def _auto_loop(self):
        while self.auto_sync and not stop_event.is_set():
            self.sync_now()
            iv = self.config.get('sync_interval', 10) * 60
            for _ in range(iv):
                if not self.auto_sync or stop_event.is_set():
                    return
                time.sleep(1)

    # ---------- tray ----------
    def _make_icon(self):
        for fname in ('icon.ico', 'icon.png'):
            p = get_resource_path(fname)
            if os.path.exists(p):
                try:
                    img = Image.open(p).convert('RGBA')
                    if img.size != (64, 64):
                        img = img.resize((64, 64), Image.LANCZOS)
                    return img
                except Exception:
                    continue
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill=(30, 100, 200, 255))
        d.rectangle((22, 26, 42, 30), fill=(255, 255, 255, 255))
        d.rectangle((22, 34, 42, 38), fill=(255, 255, 255, 255))
        return img

    def setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem('باز کردن پنجره', self._tray_show, default=True),
            pystray.MenuItem('همگام‌سازی الآن', self._tray_sync),
            pystray.MenuItem('شروع خودکار', self._tray_start),
            pystray.MenuItem('توقف خودکار', self._tray_stop),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('خروج کامل', self._tray_quit),
        )
        self.tray_icon = pystray.Icon(APP_ID, self._make_icon(), APP_NAME, menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _tray_show(self, i=None, it=None): self.root.after(0, self.root.deiconify)
    def _tray_sync(self, i=None, it=None): self.root.after(0, self.sync_now)
    def _tray_start(self, i=None, it=None): self.root.after(0, self.start_auto)
    def _tray_stop(self, i=None, it=None): self.root.after(0, self.stop_auto)
    def _tray_quit(self, i=None, it=None):
        stop_event.set()
        self.auto_sync = False
        if self.tray_icon:
            try: self.tray_icon.stop()
            except Exception: pass
        self.root.after(0, self.root.destroy)

    def on_close(self):
        """بستن پنجره = انتقال به سینی. برای خروج کامل از منوی سینی استفاده کنید."""
        self.root.withdraw()
        log("پنجره به سینی سیستم منتقل شد. برای خروج کامل: راست‌کلیک روی آیکون سینی → خروج کامل.")

    # ---------- autostart ----------
    def _startup_bat(self):
        folder = os.path.join(os.getenv('APPDATA', ''),
                              r'Microsoft\Windows\Start Menu\Programs\Startup')
        return os.path.join(folder, f'{APP_ID}.bat')

    def check_autostart(self):
        self.var_autostart.set(os.path.exists(self._startup_bat()))

    def toggle_autostart(self):
        enable = self.var_autostart.get()
        bat = self._startup_bat()
        try:
            if enable:
                if getattr(sys, 'frozen', False):
                    cmd = f'start "" "{sys.executable}"'
                else:
                    pyw = sys.executable
                    if pyw.lower().endswith('python.exe'):
                        cand = pyw[:-len('python.exe')] + 'pythonw.exe'
                        if os.path.exists(cand):
                            pyw = cand
                    script = os.path.abspath(__file__)
                    cmd = f'start "" "{pyw}" "{script}"'
                with open(bat, 'w', encoding='utf-8') as f:
                    f.write(f'@echo off\r\n{cmd}\r\n')
                log("اجرای خودکار با ویندوز فعال شد.")
            else:
                if os.path.exists(bat):
                    os.remove(bat)
                log("اجرای خودکار با ویندوز غیرفعال شد.")
        except Exception as e:
            log(f"خطا در تغییر اجرای خودکار: {e}", 'error')

# ================== main ==================
def main():
    if not acquire_single_instance():
        if signal_existing_instance():
            sys.exit(0)
    root = tk.Tk()
    app = SyncApp(root)
    start_signal_listener(app)
    root.mainloop()

if __name__ == '__main__':
    main()