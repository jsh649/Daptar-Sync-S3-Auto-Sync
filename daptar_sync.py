# -*- coding: utf-8 -*-
"""
Daptar Sync - همگام‌ساز خودکار S3
نیازمندی‌ها:
    pip install boto3 pystray Pillow cryptography watchdog
"""

import os
import sys
import json
import time
import queue
import shutil
import socket
import fnmatch
import datetime
import logging
import logging.handlers
import subprocess
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import ttk, filedialog, messagebox, scrolledtext

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, BotoCoreError
import pystray
from PIL import Image, ImageDraw
from cryptography.fernet import Fernet

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_OK = True
except Exception:
    WATCHDOG_OK = False

APP_NAME = "Daptar Sync"
APP_VERSION = "1.2.1"
APP_ID = "DaptarSync"
TRAY_ARG = '--tray'

RECYCLE_DIR_NAME = '.recycle_bin'
S3_RECYCLE_PREFIX = '.recycle_bin/'
S3_SYS_PREFIX = '.daptar/'
S3_LOCK_KEY = '.daptar/sync.lock'
STATE_FILE_NAME = '.sync_state.json'
RECYCLE_RETENTION_DAYS = 14
LOCK_TTL = 20 * 60
LOCK_REFRESH = 4 * 60
LOCK_PORT = 51724
DEBOUNCE_SEC = 5
POLL_FALLBACK_SEC = 20              # اسکن دوره‌ای وقتی watchdog موجود نیست

DEFAULT_IGNORES = ('_gsdata_, .sync, .daptar, *.tmp, ~$*, *.part, *.crdownload, '
                   '*.daptar.part, Thumbs.db, desktop.ini, .DS_Store')

DIRECTION_LABELS = {
    'bidirectional': 'دومسیره',
    'local_to_s3': 'فقط آپلود (محلی به S3)',
    's3_to_local': 'فقط دانلود (S3 به محلی)',
}
DIRECTION_VALUES = {v: k for k, v in DIRECTION_LABELS.items()}

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

def get_config_dir():
    """مسیر داده‌ها در AppData — همیشه بدون نیاز به ادمین قابل نوشتن است."""
    base = os.getenv('APPDATA') or os.path.expanduser('~')
    d = os.path.join(base, APP_ID)
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return APP_DIR

CONFIG_DIR = get_config_dir()
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.enc')
KEY_FILE = os.path.join(CONFIG_DIR, 'secret.key')
LOG_FILE = os.path.join(CONFIG_DIR, 'daptarsync.log')

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
                    app.call_in_main(lambda: (app.root.deiconify(), app.root.lift()))
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
    _fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1024 * 1024, backupCount=3, encoding='utf-8')
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
            _log_callback(f"[{ts}] {msg}", level)
        except Exception:
            pass

# ================== میان‌برهای Entry (کار با هر چیدمان کیبورد) ==================
def add_entry_shortcuts(widget):
    """Ctrl+V/C/X/A با استفاده از keycode (مستقل از زبان کیبورد) + منوی راست‌کلیک"""
    def on_ctrl(event):
        kc = event.keycode
        if kc == 86:
            widget.event_generate('<<Paste>>'); return 'break'
        if kc == 67:
            widget.event_generate('<<Copy>>'); return 'break'
        if kc == 88:
            widget.event_generate('<<Cut>>'); return 'break'
        if kc == 65:
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

# ================== آیکون پنجره ==================
def set_window_icon(root):
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

# ================== DPAPI ویندوز + رمزنگاری ==================
def _dpapi_call(protect, data):
    """CryptProtectData / CryptUnprotectData — قفل داده به کاربر فعلی ویندوز."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD),
                    ('pbData', ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    in_blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    func = (ctypes.windll.crypt32.CryptProtectData if protect
            else ctypes.windll.crypt32.CryptUnprotectData)
    ok = func(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    if not ok:
        return None
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)

def dpapi_protect(data):
    if os.name != 'nt':
        return None
    try:
        return _dpapi_call(True, data)
    except Exception:
        return None

def dpapi_unprotect(data):
    if os.name != 'nt':
        return None
    try:
        return _dpapi_call(False, data)
    except Exception:
        return None

def _read_key_file():
    try:
        with open(KEY_FILE, 'rb') as f:
            return f.read()
    except OSError:
        return None

def _write_key_file(data):
    with open(KEY_FILE, 'wb') as f:
        f.write(data)

def _load_or_create_fernet_key():
    raw = _read_key_file() if os.path.exists(KEY_FILE) else None
    if raw:
        unwrapped = dpapi_unprotect(raw)
        if unwrapped:
            try:
                Fernet(unwrapped)
                return unwrapped
            except Exception:
                pass
        try:
            Fernet(raw)
            prot = dpapi_protect(raw)
            if prot:
                try:
                    _write_key_file(prot)
                except OSError:
                    pass
            return raw
        except Exception:
            pass
        log("کلید رمزنگاری خوانده نشد (مختص کاربر/سیستم دیگری است)؛ کلید جدید ساخته می‌شود.",
            'warning')
    key = Fernet.generate_key()
    prot = dpapi_protect(key) or key
    try:
        _write_key_file(prot)
    except OSError as e:
        log(f"خطا در ذخیره کلید: {e}", 'error')
    return key

def get_fernet():
    return Fernet(_load_or_create_fernet_key())

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
        return True
    except Exception as e:
        log(f"خطا در ذخیره تنظیمات: {e}", 'error')
        return False

def migrate_legacy_files():
    """انتقال تنظیمات قدیمی (کنار exe) به AppData — فقط یک‌بار"""
    try:
        old_cfg = os.path.join(APP_DIR, 'config.enc')
        old_key = os.path.join(APP_DIR, 'secret.key')
        if (os.path.exists(old_cfg) and not os.path.exists(CONFIG_FILE)
                and os.path.exists(old_key) and not os.path.exists(KEY_FILE)):
            shutil.copy2(old_cfg, CONFIG_FILE)
            shutil.copy2(old_key, KEY_FILE)
            log("تنظیمات قدیمی به مسیر کاربر (AppData) منتقل شد.")
    except Exception as e:
        log(f"خطا در انتقال تنظیمات قدیمی: {e}", 'warning')

# ================== الگوهای نادیده‌گرفتن ==================
def build_ignore_patterns(config):
    raw = config.get('ignore_patterns')
    if raw is None:
        raw = DEFAULT_IGNORES
    return tuple(p.strip() for p in raw.split(',') if p.strip())

def is_ignored(rel_path, patterns):
    if not patterns:
        return False
    parts = rel_path.split('/')
    name = parts[-1]
    for pat in patterns:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel_path, pat):
            return True
        for part in parts[:-1]:
            if fnmatch.fnmatch(part, pat):
                return True
    return False

# ================== S3 ==================
TRANSFER_CFG = TransferConfig(
    multipart_threshold=16 * 1024 * 1024,
    multipart_chunksize=8 * 1024 * 1024,
    max_concurrency=2,
    use_threads=True,
)

def create_s3_client(config):
    endpoint = (config.get('endpoint') or '').strip().rstrip('/')
    bucket = (config.get('bucket') or '').strip()

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

def test_connection(config):
    try:
        s3 = create_s3_client(config)
        bucket = config['bucket'].strip()
        try:
            s3.head_bucket(Bucket=bucket)
            return True, None
        except (ClientError, BotoCoreError):
            s3.list_objects_v2(Bucket=bucket, MaxKeys=1)
            return True, None
    except Exception as e:
        return False, str(e)

# ================== پیمایش ==================
def list_local_files(local_folder, patterns=()):
    files = {}
    empty_dirs = set()
    for root, dirs, filenames in os.walk(local_folder):
        if RECYCLE_DIR_NAME in dirs:
            dirs.remove(RECYCLE_DIR_NAME)
        dirs[:] = [d for d in dirs if not is_ignored(d, patterns)]
        rel_root = os.path.relpath(root, local_folder).replace('\\', '/')
        if rel_root == '.':
            rel_root = ''
        if not filenames and not dirs and rel_root:
            if not is_ignored(rel_root, patterns):
                empty_dirs.add(rel_root + '/')
        for filename in filenames:
            if filename == STATE_FILE_NAME:
                continue
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, local_folder).replace('\\', '/')
            if is_ignored(rel_path, patterns):
                continue
            try:
                st = os.stat(full_path)
            except OSError:
                continue
            files[rel_path] = {'size': st.st_size, 'mtime': st.st_mtime,
                               'full_path': full_path, 'is_dir': False}
    for d in empty_dirs:
        files[d] = {'size': 0, 'mtime': 0, 'full_path': None, 'is_dir': True}
    return files

def list_s3_objects(s3, bucket, patterns=(), lock=None):
    """در صورت خطا None برمی‌گرداند تا همگام‌سازی لغو شود (جلوگیری از حذف انبوه)."""
    objects = {}
    paginator = s3.get_paginator('list_objects_v2')
    try:
        for page in paginator.paginate(Bucket=bucket):
            if lock:
                lock.touch()
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key == STATE_FILE_NAME:
                    continue
                if key.startswith(S3_RECYCLE_PREFIX) or key.startswith(S3_SYS_PREFIX):
                    continue
                if is_ignored(key, patterns):
                    continue
                objects[key] = {
                    'size': obj['Size'],
                    'mtime': obj['LastModified'].timestamp(),
                    'is_dir': key.endswith('/') and obj['Size'] == 0,
                }
    except (ClientError, BotoCoreError) as e:
        log(f"خطا در لیست کردن S3: {e}", 'error')
        return None
    return objects

# ================== عملیات ==================
def safe_upload(s3, bucket, local_path, key):
    try:
        s3.upload_file(local_path, bucket, key, Config=TRANSFER_CFG)
        try:
            rmt = s3.head_object(Bucket=bucket, Key=key)['LastModified'].timestamp()
        except Exception:
            rmt = time.time()
        log(f"آپلود: {key}")
        return True, rmt
    except Exception as e:
        log(f"خطا در آپلود {key}: {e}", 'error')
        return False, None

def safe_download(s3, bucket, key, local_path):
    """دانلود اتمیک: اول فایل موقت، بعد جایگزینی — فایل اصلی هرگز نیمه‌کاره نمی‌شود."""
    tmp = local_path + '.daptar.part'
    try:
        d = os.path.dirname(local_path)
        if d:
            os.makedirs(d, exist_ok=True)
        s3.download_file(bucket, key, tmp, Config=TRANSFER_CFG)
        os.replace(tmp, local_path)
        lmt = os.stat(local_path).st_mtime
        log(f"دانلود: {key}")
        return True, lmt
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        log(f"خطا در دانلود {key}: {e}", 'error')
        return False, None

def safe_create_folder_marker(s3, bucket, key):
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=b'')
        try:
            rmt = s3.head_object(Bucket=bucket, Key=key)['LastModified'].timestamp()
        except Exception:
            rmt = time.time()
        log(f"ایجاد نشانگر پوشه: {key}")
        return True, rmt
    except Exception as e:
        log(f"خطا در ساخت نشانگر {key}: {e}", 'error')
        return False, None

def move_local_to_recycle(local_folder, rel_path):
    full_path = os.path.join(local_folder, rel_path.rstrip('/'))
    if not os.path.exists(full_path):
        return True
    recycle_dir = os.path.join(local_folder, RECYCLE_DIR_NAME)
    os.makedirs(recycle_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    dest = os.path.join(recycle_dir, f"{ts}__{rel_path.replace('/', '__')}")
    try:
        shutil.move(full_path, dest)
        log(f"بازیافت محلی: {rel_path}")
        return True
    except Exception as e:
        log(f"خطا در انتقال به بازیافت {rel_path}: {e}", 'error')
        return False

def move_s3_to_recycle(s3, bucket, key):
    rkey = f"{S3_RECYCLE_PREFIX}{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}__{key.replace('/', '__')}"
    try:
        s3.copy_object(Bucket=bucket, CopySource={'Bucket': bucket, 'Key': key}, Key=rkey)
        s3.delete_object(Bucket=bucket, Key=key)
        log(f"بازیافت S3: {key}")
        return True
    except Exception as e:
        log(f"خطا در انتقال به بازیافت S3 {key}: {e}", 'error')
        return False

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

def cleanup_s3_recycle(s3, bucket, lock=None):
    paginator = s3.get_paginator('list_objects_v2')
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=S3_RECYCLE_PREFIX):
            if lock:
                lock.touch()
            for obj in page.get('Contents', []):
                if (now - obj['LastModified']).days > RECYCLE_RETENTION_DAYS:
                    s3.delete_object(Bucket=bucket, Key=obj['Key'])
                    log(f"حذف دائمی S3: {obj['Key']}")
    except Exception as e:
        log(f"خطا در پاکسازی S3: {e}", 'error')

# ================== قفل توزیع‌شده (چند ماشین) ==================
class S3SyncLock:
    """قفل روی باکت تا دو ماشین هم‌زمان همگام‌سازی نکنند.
    اگر سرویس‌دهنده شرط IfNoneMatch را پشتیبانی نکند، بی‌خطر باز (fail-open) می‌شود."""

    def __init__(self, s3, bucket):
        self.s3 = s3
        self.bucket = bucket
        self.owner = f"{socket.gethostname()}#{os.getpid()}"
        self.owned = False
        self._last_beat = 0.0

    def _body(self):
        return json.dumps({'owner': self.owner, 'ts': time.time()}).encode('utf-8')

    def _read_lock(self):
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=S3_LOCK_KEY)
            info = json.loads(obj['Body'].read().decode('utf-8', 'replace'))
            return info, time.time() - float(info.get('ts') or 0)
        except Exception:
            return None, 0.0

    def acquire(self):
        for _ in range(2):
            try:
                self.s3.put_object(Bucket=self.bucket, Key=S3_LOCK_KEY,
                                   Body=self._body(), IfNoneMatch='*')
                self.owned = True
                self._last_beat = time.time()
                return True
            except ClientError as e:
                code = str(e.response.get('Error', {}).get('Code', ''))
                if code not in ('PreconditionFailed', 'ConditionalRequestConflict', '412'):
                    log("قفل شرطی توسط سرویس پشتیبانی نشد — ادامه بدون قفل.", 'warning')
                    return True
                info, age = self._read_lock()
                if info is None:
                    continue
                if age > LOCK_TTL:
                    try:
                        self.s3.put_object(Bucket=self.bucket, Key=S3_LOCK_KEY,
                                           Body=self._body())
                        self.owned = True
                        self._last_beat = time.time()
                        log(f"قفل کهنهٔ «{info.get('owner', '?')}» در اختیار گرفته شد.", 'warning')
                        return True
                    except Exception:
                        return False
                log(f"ماشین دیگری ({info.get('owner', '?')}) در حال همگام‌سازی است — این دور رد شد.",
                    'warning')
                return False
            except Exception as e:
                log(f"خطا در گرفتن قفل — ادامه بدون قفل: {e}", 'warning')
                return True
        return False

    def touch(self):
        if not self.owned:
            return
        now = time.time()
        if now - self._last_beat < LOCK_REFRESH:
            return
        self._last_beat = now
        try:
            self.s3.put_object(Bucket=self.bucket, Key=S3_LOCK_KEY, Body=self._body())
        except Exception:
            pass

    def release(self):
        if not self.owned:
            return
        self.owned = False
        try:
            info, _ = self._read_lock()
            if info and info.get('owner') == self.owner:
                self.s3.delete_object(Bucket=self.bucket, Key=S3_LOCK_KEY)
        except Exception:
            pass

# ================== اجرای عملیات ==================
def _execute_action(s3, bucket, local_folder, act):
    op = act['op']
    key = act['key']
    if op == 'upload':
        if act.get('recycle_remote_first'):
            move_s3_to_recycle(s3, bucket, key)
        ok, rmt = safe_upload(s3, bucket, act['path'], key)
        return (key, 'upload', ok, act.get('lm', 0), rmt)
    if op == 'download':
        if act.get('recycle_local_first'):
            move_local_to_recycle(local_folder, key)
        ok, lmt = safe_download(s3, bucket, key, act['path'])
        return (key, 'download', ok, lmt, act.get('rm', 0))
    if op == 'marker':
        ok, rmt = safe_create_folder_marker(s3, bucket, key)
        return (key, 'marker', ok, 0, rmt)
    if op == 'mkdir':
        try:
            os.makedirs(act['path'], exist_ok=True)
            log(f"پوشه: {key}")
            return (key, 'mkdir', True, 0, act.get('rm', 0))
        except OSError as e:
            log(f"خطا در ساخت پوشه {key}: {e}", 'error')
            return (key, 'mkdir', False, None, None)
    if op == 'recycle_local':
        ok = move_local_to_recycle(local_folder, key)
        return (key, 'recycle_local', ok, None, None)
    if op == 'recycle_remote':
        ok = move_s3_to_recycle(s3, bucket, key)
        return (key, 'recycle_remote', ok, None, None)
    return (key, 'unknown', False, None, None)

# ================== همگام‌سازی ==================
progress_callback = None

def sync(config, stop=None):
    """
    الگوریتم مبتنی بر state (نسخهٔ ۲):
    تغییر هر طرف نسبت به «آخرین وضعیت ثبت‌شدهٔ همان ماشین» سنجیده می‌شود؛
    هرگز زمان محلی مستقیماً با زمان سرور مقایسه نمی‌شود.
    """
    stop = stop if stop is not None else threading.Event()
    summary = {'uploads': 0, 'downloads': 0, 'folders': 0,
               'recycled_local': 0, 'recycled_remote': 0, 'conflicts': 0,
               'stopped': False, 'error': False, 'locked': False}

    bucket = (config.get('bucket') or '').strip()
    local_folder = (config.get('local_folder') or '').strip()
    if not bucket or not local_folder:
        log("تنظیمات ناقص است.", 'error')
        summary['error'] = True
        return summary
    if not os.path.isdir(local_folder):
        log(f"پوشه محلی وجود ندارد: {local_folder}", 'error')
        summary['error'] = True
        return summary

    direction = config.get('direction', 'bidirectional')
    patterns = build_ignore_patterns(config)
    try:
        workers = max(1, min(8, int(config.get('parallel', 3))))
    except (TypeError, ValueError):
        workers = 3
    can_up = direction in ('bidirectional', 'local_to_s3')
    can_down = direction in ('bidirectional', 's3_to_local')

    s3 = create_s3_client(config)

    lock = S3SyncLock(s3, bucket)
    if not lock.acquire():
        summary['locked'] = True
        return summary

    t0 = time.time()
    log("=== شروع همگام‌سازی ===")
    try:
        local_files = list_local_files(local_folder, patterns)
        remote_files = list_s3_objects(s3, bucket, patterns, lock)
        if remote_files is None:
            log("دریافت لیست S3 ناموفق بود؛ برای جلوگیری از حذف اشتباه، همگام‌سازی لغو شد.",
                'error')
            summary['error'] = True
            return summary

        state_file = os.path.join(local_folder, STATE_FILE_NAME)
        state = {}
        first_run = True
        if os.path.exists(state_file):
            try:
                with open(state_file, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                if isinstance(raw, dict) and raw.get('_v') == 2:
                    state = raw.get('files') or {}
                    first_run = False
                else:
                    log("state قدیمی شناسایی شد — بدون حذفِ ناشی از همگام‌سازی بازسازی می‌شود.",
                        'warning')
            except Exception:
                log("خواندن state ناموفق — بازسازی بدون حذف.", 'warning')
        if first_run:
            log("اجرای اول / بازسازی state — فقط انتقال؛ تعارض محتوایی: نسخهٔ جدیدتر برنده "
                "و قدیمی‌تر به بازیافت می‌رود.")

        # ================= فاز ۱: تصمیم‌گیری =================
        actions = []
        handled = []
        all_keys = sorted(set(local_files) | set(remote_files))
        total = len(all_keys)
        step = max(1, total // 100)
        for i, key in enumerate(all_keys, 1):
            if stop.is_set():
                summary['stopped'] = True
                log("همگام‌سازی متوقف شد.", 'warning')
                break
            handled.append(key)
            if progress_callback and (i % step == 0 or i == total):
                progress_callback('مقایسه', i, total)

            L = local_files.get(key)
            R = remote_files.get(key)
            P = None if first_run else state.get(key)
            lpath = os.path.join(local_folder, key.rstrip('/'))

            # ---------- هر دو طرف ----------
            if L and R:
                if L.get('is_dir') or R.get('is_dir'):
                    continue
                lm, rm = L['mtime'], R['mtime']
                if first_run:
                    if L['size'] == R['size']:
                        continue
                    summary['conflicts'] += 1
                    log(f"محتوای متفاوت (بازسازی state): {key}", 'warning')
                    if lm > rm:
                        if can_up:
                            actions.append({'op': 'upload', 'key': key, 'path': L['full_path'],
                                            'lm': lm, 'rm': rm, 'recycle_remote_first': True})
                    elif can_down:
                        actions.append({'op': 'download', 'key': key, 'path': lpath,
                                        'lm': lm, 'rm': rm, 'recycle_local_first': True})
                    continue
                if P is None:
                    changed_l = changed_r = True
                else:
                    changed_l = abs(lm - (P.get('local_mtime') or 0)) > 2
                    changed_r = abs(rm - (P.get('remote_mtime') or 0)) > 2
                    if not changed_l and not changed_r:
                        continue
                if changed_l and changed_r:
                    summary['conflicts'] += 1
                    log(f"تعارض (تغییر هم‌زمان دو طرف): {key}", 'warning')
                    local_wins = (lm > rm) if direction == 'bidirectional' else can_up
                    if local_wins:
                        if can_up:
                            actions.append({'op': 'upload', 'key': key, 'path': L['full_path'],
                                            'lm': lm, 'rm': rm, 'recycle_remote_first': True})
                    elif can_down:
                        actions.append({'op': 'download', 'key': key, 'path': lpath,
                                        'lm': lm, 'rm': rm, 'recycle_local_first': True})
                elif changed_l:
                    if can_up:
                        actions.append({'op': 'upload', 'key': key, 'path': L['full_path'],
                                        'lm': lm, 'rm': rm})
                else:
                    if can_down:
                        actions.append({'op': 'download', 'key': key, 'path': lpath,
                                        'lm': lm, 'rm': rm})
                continue

            # ---------- فقط محلی ----------
            if L:
                is_dir = bool(L.get('is_dir'))
                if P is None or P.get('remote_mtime') is None:
                    if can_up:
                        if is_dir:
                            actions.append({'op': 'marker', 'key': key})
                        else:
                            actions.append({'op': 'upload', 'key': key, 'path': L['full_path'],
                                            'lm': L['mtime'], 'rm': 0})
                else:
                    local_changed = (not is_dir) and abs(L['mtime'] - (P.get('local_mtime') or 0)) > 2
                    if local_changed and can_up:
                        log(f"حفظ ویرایش در برابر حذف: {key}")
                        actions.append({'op': 'upload', 'key': key, 'path': L['full_path'],
                                        'lm': L['mtime'], 'rm': 0})
                    elif direction == 'bidirectional':
                        actions.append({'op': 'recycle_local', 'key': key, 'path': lpath})
                    elif direction == 'local_to_s3':
                        if is_dir:
                            actions.append({'op': 'marker', 'key': key})
                        else:
                            actions.append({'op': 'upload', 'key': key, 'path': L['full_path'],
                                            'lm': L['mtime'], 'rm': 0})
                continue

            # ---------- فقط سرور ----------
            is_dir = bool(R.get('is_dir'))
            if P is None or P.get('local_mtime') is None:
                if can_down:
                    if is_dir:
                        actions.append({'op': 'mkdir', 'key': key, 'path': lpath, 'rm': R['mtime']})
                    else:
                        actions.append({'op': 'download', 'key': key, 'path': lpath,
                                        'lm': 0, 'rm': R['mtime']})
            else:
                remote_changed = abs(R['mtime'] - (P.get('remote_mtime') or 0)) > 2
                if remote_changed and can_down:
                    log(f"حفظ ویرایش در برابر حذف: {key}")
                    actions.append({'op': 'download', 'key': key, 'path': lpath,
                                    'lm': 0, 'rm': R['mtime']})
                elif direction == 'bidirectional':
                    actions.append({'op': 'recycle_remote', 'key': key})
                elif direction == 's3_to_local':
                    if is_dir:
                        actions.append({'op': 'mkdir', 'key': key, 'path': lpath, 'rm': R['mtime']})
                    else:
                        actions.append({'op': 'download', 'key': key, 'path': lpath,
                                        'lm': 0, 'rm': R['mtime']})

        # ================= فاز ۲: اجرای موازی =================
        updated = {}
        removed = set()
        total_ops = len(actions)
        if total_ops:
            log(f"{total_ops} عملیات (انتقال موازی ×{workers})...")

            def _do(act):
                if stop.is_set():
                    return None
                try:
                    return _execute_action(s3, bucket, local_folder, act)
                except Exception as e:
                    log(f"خطای غیرمنتظره در عملیات {act.get('key')}: {e}", 'error')
                    return (act['key'], 'error', False, None, None)

            done = 0
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(_do, a) for a in actions]
                try:
                    for fut in as_completed(futures):
                        lock.touch()
                        res = fut.result()
                        done += 1
                        if progress_callback:
                            progress_callback('انتقال', done, total_ops)
                        if res is None:
                            summary['stopped'] = True
                            continue
                        key, kind, ok, v1, v2 = res
                        if not ok:
                            continue
                        if kind == 'upload':
                            summary['uploads'] += 1
                            updated[key] = {'local_mtime': v1, 'remote_mtime': v2}
                        elif kind == 'download':
                            summary['downloads'] += 1
                            updated[key] = {'local_mtime': v1, 'remote_mtime': v2}
                        elif kind in ('marker', 'mkdir'):
                            summary['folders'] += 1
                            updated[key] = {'local_mtime': 0, 'remote_mtime': v2}
                        elif kind == 'recycle_local':
                            summary['recycled_local'] += 1
                            removed.add(key)
                        elif kind == 'recycle_remote':
                            summary['recycled_remote'] += 1
                            removed.add(key)
                finally:
                    if stop.is_set():
                        for f in futures:
                            f.cancel()

        # ================= فاز ۳: ذخیرهٔ state =================
        handled_set = set(handled)
        new_state = {}
        for k in all_keys:
            if k in handled_set:
                if k in removed:
                    continue
                if k in updated:
                    new_state[k] = updated[k]
                elif k in state:
                    new_state[k] = state[k]
                else:
                    l = local_files.get(k)
                    r = remote_files.get(k)
                    new_state[k] = {'local_mtime': l['mtime'] if l else None,
                                    'remote_mtime': r['mtime'] if r else None}
            else:
                if k in state:
                    new_state[k] = state[k]
        try:
            tmp_state = state_file + '.tmp'
            with open(tmp_state, 'w', encoding='utf-8') as f:
                json.dump({'_v': 2, 'files': new_state}, f, ensure_ascii=False)
            os.replace(tmp_state, state_file)
        except OSError as e:
            log(f"خطا در ذخیره state: {e}", 'warning')

        cleanup_local_recycle(local_folder)
        cleanup_s3_recycle(s3, bucket, lock)

        if not summary['stopped'] and not summary['error']:
            log(f"=== پایان ({time.time() - t0:.0f}s): {summary['uploads']} آپلود، "
                f"{summary['downloads']} دانلود، {summary['folders']} پوشه، "
                f"{summary['recycled_local'] + summary['recycled_remote']} بازیافت، "
                f"{summary['conflicts']} تعارض ===", 'success')
    except Exception as e:
        log(f"خطای غیرمنتظره: {e}", 'error')
        summary['error'] = True
    finally:
        lock.release()
    return summary

# ================== پوشه‌بینی فوری (watchdog) ==================
if WATCHDOG_OK:
    class _FsEventHandler(FileSystemEventHandler):
        def __init__(self, app):
            self.app = app

        def _mark(self, path):
            try:
                self.app.on_fs_event(path)
            except Exception:
                pass

        def on_created(self, event):
            if not event.is_directory:
                self._mark(event.src_path)

        def on_modified(self, event):
            if not event.is_directory:
                self._mark(event.src_path)

        def on_deleted(self, event):
            self._mark(event.src_path)

        def on_moved(self, event):
            self._mark(event.src_path)
            self._mark(getattr(event, 'dest_path', '') or '')

# ================== GUI ==================
class SyncApp:
    def __init__(self, root, start_in_tray=False):
        global _log_callback, progress_callback
        _log_callback = self._queue_log
        progress_callback = self.update_progress

        self.root = root
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        set_window_icon(self.root)
        self.root.geometry("880x700")
        self.root.minsize(800, 620)

        try:
            style = ttk.Style(self.root)
            if 'vista' in style.theme_names():
                style.theme_use('vista')
        except Exception:
            pass

        self.config = load_config()
        self.auto_sync = False
        self.auto_thread = None
        self.sync_thread = None
        self.sync_stop = None
        self.tray_icon = None

        self._observer = None
        self._poll_stop = None
        self._poll_thread = None
        self._poll_snapshot = None
        self._watchdog_warned = False
        self._fs_dirty = False
        self._last_fs_event = 0.0
        self._ignore_cache = ()
        self._last_sync_text = '—'
        self._next_sync_at = None
        self._auto_token = 0
        self._sync_lock = threading.Lock()
        self._log_queue = queue.Queue()
        self._main_queue = queue.Queue()
        self._status_cache = ''

        self.create_widgets()
        self.load_config_to_ui()
        self._ignore_cache = build_ignore_patterns(self.config)
        self.check_autostart()
        self.setup_tray()
        self._restart_watcher()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        if start_in_tray and self.config.get('bucket'):
            self.root.withdraw()
            log("برنامه در حالت سینی اجرا شد.")

        self.root.after(200, self._ui_pump)
        self.root.after(1500, self._instant_poll)
        self.root.after(800, self.maybe_start_auto)

    def maybe_start_auto(self):
        if self.config.get('auto_sync') and not self.auto_sync:
            log("ادامهٔ حالت خودکار از اجرای قبلی...")
            self.start_auto()

    # ---------- widgets ----------
    def create_widgets(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        form = ttk.LabelFrame(main, text="تنظیمات اتصال", padding=10)
        form.pack(fill=tk.X)

        ttk.Label(form, text="Endpoint:").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.entry_endpoint = ttk.Entry(form)
        self.entry_endpoint.grid(row=0, column=1, columnspan=2, sticky=tk.EW, pady=3, padx=(5, 0))
        add_entry_shortcuts(self.entry_endpoint)

        ttk.Label(form, text="Access Key:").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.entry_access = ttk.Entry(form)
        self.entry_access.grid(row=1, column=1, columnspan=2, sticky=tk.EW, pady=3, padx=(5, 0))
        add_entry_shortcuts(self.entry_access)

        ttk.Label(form, text="Secret Key:").grid(row=2, column=0, sticky=tk.W, pady=3)
        sf = ttk.Frame(form)
        sf.grid(row=2, column=1, columnspan=2, sticky=tk.EW, pady=3, padx=(5, 0))
        self.entry_secret = ttk.Entry(sf, show="•")
        self.entry_secret.pack(side=tk.LEFT, fill=tk.X, expand=True)
        add_entry_shortcuts(self.entry_secret)
        self.var_show_secret = tk.BooleanVar(value=False)
        ttk.Checkbutton(sf, text="نمایش", variable=self.var_show_secret,
                        command=self._toggle_secret).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(form, text="نام باکت:").grid(row=3, column=0, sticky=tk.W, pady=3)
        self.entry_bucket = ttk.Entry(form)
        self.entry_bucket.grid(row=3, column=1, columnspan=2, sticky=tk.EW, pady=3, padx=(5, 0))
        add_entry_shortcuts(self.entry_bucket)

        ttk.Label(form, text="پوشه محلی:").grid(row=4, column=0, sticky=tk.W, pady=3)
        self.entry_folder = ttk.Entry(form)
        self.entry_folder.grid(row=4, column=1, sticky=tk.EW, pady=3, padx=(5, 0))
        add_entry_shortcuts(self.entry_folder)
        ttk.Button(form, text="انتخاب...", command=self.choose_folder).grid(row=4, column=2, padx=5)

        ttk.Label(form, text="جهت همگام‌سازی:").grid(row=5, column=0, sticky=tk.W, pady=3)
        self.combo_direction = ttk.Combobox(form, values=list(DIRECTION_LABELS.values()),
                                            state="readonly", width=30)
        self.combo_direction.grid(row=5, column=1, sticky=tk.W, pady=3, padx=(5, 0))
        self.combo_direction.set(DIRECTION_LABELS['bidirectional'])

        ttk.Label(form, text="فاصله زمانی (دقیقه):").grid(row=6, column=0, sticky=tk.W, pady=3)
        self.entry_interval = ttk.Entry(form, width=10)
        self.entry_interval.grid(row=6, column=1, sticky=tk.W, pady=3, padx=(5, 0))
        add_entry_shortcuts(self.entry_interval)
        self.entry_interval.insert(0, "10")

        ttk.Label(form, text="انتقال موازی (۱ تا ۸):").grid(row=7, column=0, sticky=tk.W, pady=3)
        self.entry_parallel = ttk.Entry(form, width=10)
        self.entry_parallel.grid(row=7, column=1, sticky=tk.W, pady=3, padx=(5, 0))
        add_entry_shortcuts(self.entry_parallel)
        self.entry_parallel.insert(0, "3")

        ttk.Label(form, text="الگوهای نادیده‌گرفتن:").grid(row=8, column=0, sticky=tk.W, pady=3)
        self.entry_ignore = ttk.Entry(form)
        self.entry_ignore.grid(row=8, column=1, columnspan=2, sticky=tk.EW, pady=3, padx=(5, 0))
        add_entry_shortcuts(self.entry_ignore)

        self.var_autostart = tk.BooleanVar()
        ttk.Checkbutton(form, text="اجرای خودکار با ویندوز (شروع در سینی)",
                        variable=self.var_autostart,
                        command=self.toggle_autostart).grid(
            row=9, column=0, columnspan=3, sticky=tk.W, pady=(5, 0))

        self.var_instant = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="همگام‌سازی فوری هنگام تغییر فایل (چند ثانیه تأخیر)",
                        variable=self.var_instant,
                        command=self._restart_watcher).grid(
            row=10, column=0, columnspan=3, sticky=tk.W, pady=2)

        form.columnconfigure(1, weight=1)

        btns = ttk.Frame(main)
        btns.pack(fill=tk.X, pady=8)
        self.btn_sync = ttk.Button(btns, text="همگام‌سازی دستی", command=self.toggle_sync)
        self.btn_sync.pack(side=tk.RIGHT, padx=3)
        self.btn_auto = ttk.Button(btns, text="شروع خودکار", command=self.toggle_auto)
        self.btn_auto.pack(side=tk.RIGHT, padx=3)
        self.btn_test = ttk.Button(btns, text="تست اتصال", command=self.test_connection)
        self.btn_test.pack(side=tk.RIGHT, padx=3)
        ttk.Button(btns, text="ذخیره تنظیمات", command=self.save_settings).pack(side=tk.RIGHT, padx=3)

        pf = ttk.Frame(main)
        pf.pack(fill=tk.X, pady=(5, 0))
        self.progress = ttk.Progressbar(pf, mode='determinate')
        self.progress.pack(fill=tk.X, side=tk.LEFT, expand=True)
        self.lbl_progress = ttk.Label(pf, text="", width=18)
        self.lbl_progress.pack(side=tk.LEFT, padx=5)

        self.lbl_status = ttk.Label(main, text="", anchor=tk.W)
        self.lbl_status.pack(anchor=tk.W, pady=(6, 0))

        lh = ttk.Frame(main)
        lh.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(lh, text="گزارش عملیات:").pack(side=tk.LEFT)
        ttk.Button(lh, text="پاک کردن", command=self._clear_log).pack(side=tk.RIGHT, padx=2)
        ttk.Button(lh, text="پوشه لاگ", command=self._open_log_folder).pack(side=tk.RIGHT, padx=2)

        self.log_text = scrolledtext.ScrolledText(main, height=12, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        add_entry_shortcuts(self.log_text)
        for tag, color in (('error', '#c62828'), ('warning', '#e65100'), ('success', '#2e7d32')):
            self.log_text.tag_config(tag, foreground=color)

    def _toggle_secret(self):
        self.entry_secret.config(show="" if self.var_show_secret.get() else "•")

    # ---------- پمپ UI: تنها نقطهٔ مجاز دستکاری ویجت‌ها ----------
    def call_in_main(self, fn):
        """اجرای fn در ترد اصلی — تنها راه مجاز دستکاری ویجت‌ها از تردهای پس‌زمینه."""
        if threading.current_thread() is threading.main_thread():
            try:
                fn()
            except Exception:
                pass
        else:
            self._main_queue.put(fn)

    def _queue_log(self, msg, level='info'):
        self._log_queue.put((msg, level))

    def _ui_pump(self):
        """هر ۲۰۰ms: اجرای کارهای تردهای دیگر + لاگ + تازه‌سازی نوار وضعیت."""
        try:
            for _ in range(200):
                try:
                    fn = self._main_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn()
                except Exception:
                    pass
            dirty = False
            for _ in range(300):
                try:
                    msg, level = self._log_queue.get_nowait()
                except queue.Empty:
                    break
                self.log_text.config(state=tk.NORMAL)
                if level in ('error', 'warning', 'success'):
                    self.log_text.insert(tk.END, msg + "\n", level)
                else:
                    self.log_text.insert(tk.END, msg + "\n")
                dirty = True
            if dirty:
                self.log_text.see(tk.END)
                self.log_text.config(state=tk.DISABLED)
            self._refresh_status()
        except Exception:
            pass
        try:
            self.root.after(200, self._ui_pump)
        except Exception:
            pass

    @staticmethod
    def _fmt_interval(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            f = 10.0
        return str(int(f)) if f == int(f) else f"{f:g}"

    def _refresh_status(self):
        try:
            if self.sync_running:
                base = "در حال همگام‌سازی..."
            elif self.auto_sync:
                base = (f"خودکار: فعال "
                        f"(هر {self._fmt_interval(self.config.get('sync_interval', 10))} دقیقه)")
                if self._next_sync_at:
                    rem = max(0, int(self._next_sync_at - time.time()))
                    base += f" | بعدی: {rem // 60}:{rem % 60:02d}"
            else:
                base = "خودکار: غیرفعال"
            if self.var_instant.get():
                base += " | فوری: فعال"
            text = f"{base}    |    آخرین همگام‌سازی: {self._last_sync_text}"
            if text != self._status_cache:
                self._status_cache = text
                self.lbl_status.config(text=text)
        except Exception:
            pass

    def _clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete('1.0', tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _open_log_folder(self):
        try:
            os.startfile(CONFIG_DIR)
        except Exception as e:
            log(f"باز کردن پوشه لاگ ناموفق: {e}", 'error')

    def update_progress(self, phase, d, t):
        def _do():
            self.progress['maximum'] = max(t, 1)
            self.progress['value'] = d
            self.lbl_progress.config(text=f"{phase} {d}/{t}")
        self.call_in_main(_do)

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
        self.combo_direction.set(
            DIRECTION_LABELS.get(c.get('direction', 'bidirectional'), DIRECTION_LABELS['bidirectional']))
        self.entry_interval.delete(0, tk.END)
        self.entry_interval.insert(0, self._fmt_interval(c.get('sync_interval', 10)))
        self.entry_parallel.delete(0, tk.END)
        self.entry_parallel.insert(0, str(c.get('parallel', 3)))
        self.entry_ignore.delete(0, tk.END)
        self.entry_ignore.insert(0, c.get('ignore_patterns', DEFAULT_IGNORES))
        self.var_instant.set(bool(c.get('instant', True)))

    def get_config_from_ui(self):
        try:
            iv = float((self.entry_interval.get() or '10').replace(',', '.'))
        except ValueError:
            iv = 10
        iv = max(0.25, iv)
        try:
            par = int(self.entry_parallel.get() or 3)
        except ValueError:
            par = 3
        return {
            'endpoint': self.entry_endpoint.get().strip(),
            'access_key': self.entry_access.get().strip(),
            'secret_key': self.entry_secret.get().strip(),
            'bucket': self.entry_bucket.get().strip(),
            'local_folder': self.entry_folder.get().strip(),
            'direction': DIRECTION_VALUES.get(self.combo_direction.get(), 'bidirectional'),
            'sync_interval': iv,
            'parallel': max(1, min(8, par)),
            'ignore_patterns': self.entry_ignore.get().strip(),
            'instant': self.var_instant.get(),
            'autostart': self.var_autostart.get(),
            'auto_sync': self.auto_sync,
        }

    def save_settings(self):
        prev_folder = self.config.get('local_folder')
        prev_instant = self.config.get('instant', True)
        prev_iv = self.config.get('sync_interval')
        self.config = self.get_config_from_ui()
        if save_config(self.config):
            log("تنظیمات ذخیره شد.", 'success')
            self._ignore_cache = build_ignore_patterns(self.config)
            if (prev_folder != self.config.get('local_folder')
                    or bool(prev_instant) != bool(self.config.get('instant'))):
                self._restart_watcher()
            if self.auto_sync and prev_iv != self.config.get('sync_interval'):
                log(f"فاصله زمانی جدید ({self._fmt_interval(self.config['sync_interval'])} دقیقه) "
                    "ظرف چند ثانیه اعمال می‌شود.", 'success')
        else:
            messagebox.showerror("خطا", "ذخیره تنظیمات ناموفق بود. جزئیات در فایل لاگ.")

    # ---------- همگام‌سازی ----------
    @property
    def sync_running(self):
        return self.sync_thread is not None and self.sync_thread.is_alive()

    def toggle_sync(self):
        if self.sync_running:
            self.stop_sync()
            return
        cfg = self.get_config_from_ui()
        if not (cfg['bucket'] and cfg['local_folder']):
            messagebox.showwarning("توجه", "ابتدا نام باکت و پوشه محلی را وارد و ذخیره کنید.")
            return
        self.sync_now()

    def sync_now(self, reread_ui=True):
        """فقط از ترد اصلی صدا زده شود (دکمه/ترای/تایمر)."""
        if self.sync_running:
            return
        if reread_ui:
            self.config = self.get_config_from_ui()
            save_config(self.config)
            self._ignore_cache = build_ignore_patterns(self.config)
        self._start_sync_thread()

    def _start_sync_thread(self):
        """ایمن از هر تردی — به‌روزرسانی UI از طریق صف ترد اصلی انجام می‌شود."""
        with self._sync_lock:
            if self.sync_running:
                return False
            self.sync_stop = threading.Event()
            stop = self.sync_stop
            cfg = self.config
            self.sync_thread = threading.Thread(
                target=self._run_sync, args=(cfg, stop), daemon=True)
            self._ui_sync_started()
            self.sync_thread.start()
            return True

    def _ui_sync_started(self):
        def _do():
            self.btn_sync.config(text="توقف")
            self.progress['value'] = 0
            self.lbl_progress.config(text="آماده‌سازی...")
        self.call_in_main(_do)

    def _run_sync(self, cfg, stop):
        result = None
        try:
            result = sync(cfg, stop)
        except Exception as e:
            log(f"خطای همگام‌سازی: {e}", 'error')
        finally:
            self.call_in_main(lambda: self._sync_finished(result))

    def stop_sync(self):
        if self.sync_stop:
            self.sync_stop.set()
        log("درخواست توقف ارسال شد...", 'warning')
        try:
            self.btn_sync.config(text="در حال توقف...")
        except Exception:
            pass

    def _sync_finished(self, result):
        try:
            self.btn_sync.config(text="همگام‌سازی دستی")
            if not result:
                self._reset_progress()
                return
            if result.get('locked'):
                self._reset_progress()
                self._last_sync_text = 'ماشین دیگر در حال همگام‌سازی بود'
                self._set_tray_title(self._last_sync_text)
                if self.auto_sync or self.var_instant.get():
                    self.root.after(60000, self._retry_sync_if_idle)
                return
            if result.get('stopped'):
                self._reset_progress()
                self._last_sync_text = 'متوقف‌شده'
            elif result.get('error'):
                self._reset_progress()
                self._last_sync_text = 'خطا'
            else:
                self.progress['value'] = self.progress['maximum']
                self._last_sync_text = (
                    f"{datetime.datetime.now():%H:%M:%S} — "
                    f"{result['uploads']}↑ {result['downloads']}↓ "
                    f"{result['recycled_local'] + result['recycled_remote']}↩")
                self.root.after(2500, self._reset_progress)
            self._set_tray_title(self._last_sync_text)
        except Exception:
            pass

    def _reset_progress(self):
        try:
            if self.sync_running:
                return
            self.progress['value'] = 0
            self.lbl_progress.config(text="")
        except Exception:
            pass

    def _set_tray_title(self, text):
        try:
            if self.tray_icon:
                self.tray_icon.title = f"{APP_NAME} — {text}"
        except Exception:
            pass

    def _retry_sync_if_idle(self):
        try:
            if (self.auto_sync or self.var_instant.get()) and not self.sync_running:
                self._start_sync_thread()
        except Exception:
            pass

    def test_connection(self):
        cfg = self.get_config_from_ui()
        if not (cfg['endpoint'] and cfg['bucket']):
            messagebox.showwarning("توجه", "ابتدا Endpoint و نام باکت را وارد کنید.")
            return
        self.btn_test.config(state=tk.DISABLED, text="...")

        def worker():
            ok, err = test_connection(cfg)

            def done():
                try:
                    self.btn_test.config(state=tk.NORMAL, text="تست اتصال")
                except Exception:
                    pass
                if ok:
                    log("تست اتصال: موفق", 'success')
                    messagebox.showinfo("نتیجه", "اتصال با موفقیت برقرار شد.")
                else:
                    log(f"تست اتصال: ناموفق — {err}", 'error')
                    messagebox.showerror("نتیجه", f"اتصال ناموفق:\n\n{err}")
            self.call_in_main(done)

        threading.Thread(target=worker, daemon=True).start()

    # ---------- حالت خودکار ----------
    def _interval_seconds(self):
        try:
            iv = float(self.config.get('sync_interval', 10))
        except (TypeError, ValueError):
            iv = 10
        return max(15, iv * 60)

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
        self._ignore_cache = build_ignore_patterns(self.config)
        self.btn_auto.config(text="توقف خودکار")
        log(f"همگام‌سازی خودکار هر {self._fmt_interval(self.config['sync_interval'])} دقیقه "
            f"شروع شد.", 'success')
        self._auto_token += 1
        self.auto_thread = threading.Thread(
            target=self._auto_loop, args=(self._auto_token,), daemon=True)
        self.auto_thread.start()

    def stop_auto(self):
        if not self.auto_sync:
            return
        self.auto_sync = False
        self._next_sync_at = None
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

    def _auto_loop(self, token):
        """زمان‌بندی پویا: تغییر «فاصله زمانی» در تنظیمات ظرف ~۱ ثانیه اعمال می‌شود.
        توکن جلوی هم‌زمانی دو حلقه (توقف/شروع سریع) را می‌گیرد."""
        try:
            while self.auto_sync and self._auto_token == token:
                self._next_sync_at = None
                self._start_sync_thread()
                while (self.auto_sync and self._auto_token == token
                       and self.sync_running):
                    time.sleep(0.5)
                if not (self.auto_sync and self._auto_token == token):
                    return
                cycle_start = time.time()
                last_iv = None
                while self.auto_sync and self._auto_token == token:
                    iv = self._interval_seconds()
                    deadline = cycle_start + iv
                    if iv != last_iv:
                        if last_iv is None:
                            log(f"همگام‌سازی بعدی تا {self._fmt_interval(iv / 60)} دقیقه دیگر.")
                        else:
                            log(f"فاصله زمانی جدید اعمال شد: همگام‌سازی بعدی "
                                f"{self._fmt_interval(iv / 60)} دقیقه دیگر.")
                        last_iv = iv
                    if time.time() >= deadline:
                        break
                    self._next_sync_at = deadline
                    time.sleep(1)
        except Exception as e:
            log(f"حلقهٔ همگام‌سازی خودکار متوقف شد: {e}", 'error')

    # ---------- پوشه‌بینی فوری ----------
    def _stop_watcher(self):
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=3)
            except Exception:
                pass
            self._observer = None
        if self._poll_thread is not None:
            if self._poll_stop is not None:
                self._poll_stop.set()
            try:
                self._poll_thread.join(timeout=5)
            except Exception:
                pass
            self._poll_thread = None
            self._poll_stop = None
        self._poll_snapshot = None

    def _restart_watcher(self):
        self._stop_watcher()
        if not self.var_instant.get():
            return
        folder = (self.config.get('local_folder') or '').strip()
        if not folder or not os.path.isdir(folder):
            return
        if WATCHDOG_OK:
            try:
                obs = Observer(timeout=1)
                obs.schedule(_FsEventHandler(self), folder, recursive=True)
                obs.daemon = True
                obs.start()
                self._observer = obs
                log("پایش تغییرات فایل فعال شد (همگام‌سازی فوری).", 'success')
                return
            except Exception as e:
                log(f"پوشه‌بینی ناموفق ({e}) — اسکن دوره‌ای جایگزین می‌شود.", 'warning')
        self._start_poll_fallback()

    def _start_poll_fallback(self):
        if not self._watchdog_warned:
            log("کتابخانه watchdog موجود نیست؛ تغییرات با اسکن دوره‌ای (هر ۲۰ ثانیه) پایش می‌شود. "
                "برای پایش لحظه‌ای، هنگام ساخت «pip install watchdog» را اجرا و دوباره بیلد بگیرید.",
                'warning')
            self._watchdog_warned = True
        self._poll_stop = threading.Event()
        stop = self._poll_stop
        self._poll_thread = threading.Thread(
            target=self._poll_loop, args=(stop,), daemon=True)
        self._poll_thread.start()

    def _poll_loop(self, stop):
        while not stop.is_set():
            try:
                if not self.sync_running:
                    folder = (self.config.get('local_folder') or '').strip()
                    if folder and os.path.isdir(folder):
                        snap = self._scan_snapshot(folder)
                        if self._poll_snapshot is not None and snap != self._poll_snapshot:
                            self._fs_dirty = True
                            self._last_fs_event = time.time()
                        self._poll_snapshot = snap
            except Exception:
                pass
            stop.wait(POLL_FALLBACK_SEC)

    def _scan_snapshot(self, folder):
        files = list_local_files(folder, self._ignore_cache)
        return {k: (v['size'], int(v['mtime']), bool(v.get('is_dir')))
                for k, v in files.items()}

    def on_fs_event(self, path):
        """از ترد watchdog صدا زده می‌شود — فقط پرچم می‌گذارد."""
        folder = self.config.get('local_folder')
        if not folder or not path:
            return
        try:
            rel = os.path.relpath(path, folder).replace('\\', '/')
        except ValueError:
            return
        if rel == STATE_FILE_NAME or rel.startswith(RECYCLE_DIR_NAME):
            return
        if rel.endswith('.daptar.part'):
            return
        if is_ignored(rel, self._ignore_cache):
            return
        self._fs_dirty = True
        self._last_fs_event = time.time()

    def _instant_poll(self):
        try:
            if (self.var_instant.get() and self._fs_dirty and not self.sync_running
                    and (time.time() - self._last_fs_event) >= DEBOUNCE_SEC):
                self._fs_dirty = False
                log("تغییر فایل شناسایی شد — همگام‌سازی فوری...")
                self.sync_now(reread_ui=False)
        except Exception:
            pass
        try:
            self.root.after(1500, self._instant_poll)
        except Exception:
            pass

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

    def _tray_show(self, i=None, it=None):
        self.call_in_main(self.root.deiconify)

    def _tray_sync(self, i=None, it=None):
        self.call_in_main(self.toggle_sync)

    def _tray_start(self, i=None, it=None):
        self.call_in_main(self.start_auto)

    def _tray_stop(self, i=None, it=None):
        self.call_in_main(self.stop_auto)

    def _tray_quit(self, i=None, it=None):
        self.call_in_main(self._quit)

    def _quit(self):
        self.auto_sync = False
        if self.sync_stop:
            self.sync_stop.set()
        self._stop_watcher()
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.root.destroy()

    def on_close(self):
        """بستن پنجره = انتقال به سینی. برای خروج کامل از منوی سینی استفاده کنید."""
        self.root.withdraw()
        log("پنجره به سینی سیستم منتقل شد. برای خروج کامل: راست‌کلیک روی آیکون سینی → خروج کامل.")

    # ---------- autostart ----------
    def _startup_dir(self):
        return os.path.join(os.getenv('APPDATA', ''),
                            r'Microsoft\Windows\Start Menu\Programs\Startup')

    def _startup_paths(self):
        d = self._startup_dir()
        return [os.path.join(d, f'{APP_ID}{ext}') for ext in ('.lnk', '.bat')]

    def check_autostart(self):
        self.var_autostart.set(any(os.path.exists(p) for p in self._startup_paths()))

    def toggle_autostart(self):
        enable = self.var_autostart.get()
        try:
            if enable:
                if self._create_startup_shortcut():
                    bat = os.path.join(self._startup_dir(), f'{APP_ID}.bat')
                    if os.path.exists(bat):
                        try:
                            os.remove(bat)
                        except OSError:
                            pass
                    log("اجرای خودکار با ویندوز فعال شد (شروع در سینی).", 'success')
                else:
                    self._create_startup_bat()
                    log("اجرای خودکار با روش جایگزین (bat) فعال شد.", 'warning')
            else:
                for p in self._startup_paths():
                    if os.path.exists(p):
                        os.remove(p)
                log("اجرای خودکار با ویندوز غیرفعال شد.")
        except Exception as e:
            log(f"خطا در تغییر اجرای خودکار: {e}", 'error')
        self.check_autostart()

    def _autostart_target(self):
        if getattr(sys, 'frozen', False):
            return sys.executable, TRAY_ARG
        pyw = sys.executable
        if pyw.lower().endswith('python.exe'):
            cand = os.path.join(os.path.dirname(pyw), 'pythonw.exe')
            if os.path.exists(cand):
                pyw = cand
        return pyw, f'"{os.path.abspath(__file__)}" {TRAY_ARG}'

    @staticmethod
    def _ps_quote(s):
        return s.replace("'", "''")

    def _create_startup_shortcut(self):
        """شورتکات استاندارد Startup با آیکون برنامه — بدون فلش پنجره کنسول"""
        try:
            target, args = self._autostart_target()
            lnk = os.path.join(self._startup_dir(), f'{APP_ID}.lnk')
            ico = get_resource_path('icon.ico')
            icon = ico if os.path.exists(ico) else target
            ps = (
                "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
                "$s.TargetPath='%s';"
                "$s.Arguments='%s';"
                "$s.WorkingDirectory='%s';"
                "$s.IconLocation='%s';"
                "$s.WindowStyle=7;"
                "$s.Save()"
            ) % (self._ps_quote(lnk), self._ps_quote(target), self._ps_quote(args),
                 self._ps_quote(get_app_dir()), self._ps_quote(icon))
            subprocess.run(
                ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps],
                capture_output=True, timeout=20,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            return os.path.exists(lnk)
        except Exception as e:
            log(f"ساخت شورتکات ناموفق بود ({e}) — از روش جایگزین استفاده می‌شود.", 'warning')
            return False

    def _create_startup_bat(self):
        target, args = self._autostart_target()
        bat = os.path.join(self._startup_dir(), f'{APP_ID}.bat')
        try:
            open(bat, 'w', encoding='mbcs').close()
            enc = 'mbcs'
        except Exception:
            enc = 'utf-8'
        with open(bat, 'w', encoding=enc, newline='') as f:
            f.write(f'@echo off\r\nstart "" "{target}" {args}\r\n')
        return os.path.exists(bat)

# ================== main ==================
def main():
    start_in_tray = TRAY_ARG in sys.argv or '--minimized' in sys.argv

    if not acquire_single_instance():
        if not start_in_tray:
            signal_existing_instance()
        sys.exit(0)

    migrate_legacy_files()
    log(f"{APP_NAME} {APP_VERSION} اجرا شد.")

    root = tk.Tk()
    app = SyncApp(root, start_in_tray=start_in_tray)
    start_signal_listener(app)
    root.mainloop()

if __name__ == '__main__':
    main()
