"""Read-only LoRA Manager adapter and Civitai hash lookup (stdlib only).

Independent implementation: consumes .metadata.json/.civitai.info and the
LoRA Manager SQLite cache; does not import or modify the plugin.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

EXTENSIONS = {'.safetensors', '.ckpt', '.pt', '.bin'}


def model_key(name):
    value = str(name).replace('\\', '/').strip()
    if Path(value).suffix.lower() in EXTENSIONS:
        value = value.rsplit('.', 1)[0]
    return value.casefold()


def search_url(name):
    name = str(name).replace('\\', '/').rsplit('/', 1)[-1]
    if Path(name).suffix.lower() in EXTENSIONS:
        name = name.rsplit('.', 1)[0]
    return 'https://civitai.com/search/models?query=' + quote(name, safe='')


def page_url(data):
    def number(value):
        return str(value) if not isinstance(value, bool) and re.fullmatch(r'[1-9][0-9]{0,19}', str(value)) else None
    mid, vid = number(data.get('modelId')), number(data.get('id'))
    if mid:
        return 'https://civitai.com/models/' + mid + ('?modelVersionId=' + vid if vid else '')
    if vid:
        return 'https://civitai.com/model-versions/' + vid
    return None


def valid_external_url(url):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 32 for c in url):
        return False
    p = urlsplit(url)
    if p.scheme != 'https' or p.netloc not in ('civitai.red', 'civitai.com') or p.fragment:
        return False
    return bool(re.fullmatch(r'/models/[1-9][0-9]*(?:\?modelVersionId=[1-9][0-9]*)?|/model-versions/[1-9][0-9]*|/search/models\?query=[A-Za-z0-9%_.~\-]*', p.path + ('?' + p.query if p.query else '')))


def read_json(path):
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            return {}
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class Resolver:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='cmv-lora')
        self.lock = threading.Lock()
        self.index_lock = threading.Lock()
        self.jobs = {}
        self.index = []
        self.index_time = 0
        self.index_root = None
        self.cache_lock = threading.Lock()

    def resolve_many(self, items, comfy_root, data_dir):
        if not isinstance(items, list) or len(items) > 64:
            raise ValueError('最多查询 64 个 LoRA')
        requests = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str):
                raise ValueError('LoRA 名称格式不正确')
            name = item['name'].strip()
            hint = item.get('hash', '')
            if not name or len(name) > 1024 or not isinstance(hint, str):
                raise ValueError('LoRA 名称或哈希格式不正确')
            hint = hint.strip().lower()
            if hint and not re.fullmatch(r'[0-9a-f]{8,64}', hint):
                raise ValueError('LoRA 哈希格式不正确')
            requests.append((name, hint))
        results = []
        with self.lock:
            now = time.monotonic()
            self.jobs = {k: v for k, v in self.jobs.items() if not v[1].done() or now - v[0] < 300}
            for name, hint in requests:
                key = (str(comfy_root), name, hint)
                job = self.jobs.get(key)
                if job is None and len(self.jobs) < 256:
                    job = (now, self.pool.submit(self._resolve, name, hint, comfy_root, Path(data_dir)))
                    self.jobs[key] = job
                result = {'name': name, 'status': 'pending', 'url': search_url(name)}
                if job is None:
                    result['status'] = 'busy'
                elif job[1].done():
                    try:
                        result.update(job[1].result())
                    except Exception:
                        result['status'] = 'unavailable'
                results.append(result)
        return results

    def _discover(self, comfy_root):
        with self.index_lock:
            if self.index_root == str(comfy_root) and time.monotonic() - self.index_time < 60:
                return self.index
            roots, settings_dirs = [], []
            if comfy_root:
                roots.append(Path(comfy_root) / 'models' / 'loras')
                settings_dirs.append(Path(comfy_root) / 'custom_nodes' / 'comfyui-lora-manager')
            if os.environ.get('LORA_MANAGER_SETTINGS_DIR'):
                settings_dirs.insert(0, Path(os.environ['LORA_MANAGER_SETTINGS_DIR']))
            if os.environ.get('LOCALAPPDATA'):
                settings_dirs.append(Path(os.environ['LOCALAPPDATA']) / 'ComfyUI-LoRA-Manager')
            records = {}
            for directory in settings_dirs:
                settings = read_json(directory / 'settings.json')
                active = settings.get('active_library', 'default')
                libraries = settings.get('libraries', {})
                library = libraries.get(active, {}) if isinstance(libraries, dict) else {}
                for config in (settings, library):
                    folders = config.get('folder_paths', {})
                    values = folders.get('loras', []) if isinstance(folders, dict) else []
                    if isinstance(values, list):
                        roots.extend(Path(p) for p in values if isinstance(p, str))
                    default = config.get('default_lora_root')
                    if isinstance(default, str) and default:
                        roots.append(Path(default))
                # The database supports centralized sidecars and remains read-only.
                dbs = [directory / 'cache' / 'model_cache.sqlite']
                if re.fullmatch(r'[\w-]+', str(active)):
                    dbs.insert(0, directory / 'cache' / 'model' / (str(active) + '.sqlite'))
                for db in dbs:
                    if not db.is_file():
                        continue
                    try:
                        connection = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.2)
                        try:
                            connection.execute('PRAGMA query_only=ON')
                            rows = connection.execute("SELECT file_path, modified, size, sha256, civitai_model_id, civitai_id FROM models WHERE model_type IN ('lora','loras')").fetchall()
                            for path, modified, size, digest, mid, vid in rows:
                                records[os.path.normcase(os.path.abspath(path))] = {'modified': modified, 'size': size, 'sha256': digest, 'civitai': {'modelId': mid, 'id': vid}}
                        finally:
                            connection.close()
                    except (sqlite3.Error, OSError, ValueError):
                        pass
            unique_roots = []
            for root in roots:
                try:
                    root = root.resolve()
                    if root.is_dir() and root not in unique_roots:
                        unique_roots.append(root)
                except OSError:
                    pass
            files = {}
            for root in unique_roots:
                for folder, dirs, names in os.walk(root, followlinks=False):
                    dirs[:] = [d for d in dirs if not d.startswith('.')]
                    for name in names:
                        path = Path(folder) / name
                        if path.suffix.lower() not in EXTENSIONS:
                            continue
                        try:
                            real = path.resolve()
                            if not real.is_relative_to(root):
                                continue
                            key = os.path.normcase(str(real))
                            entry = files.setdefault(key, {'path': real, 'keys': set(), 'metadata': records.get(key, {})})
                            entry['keys'].add(model_key(path.relative_to(root)))
                        except OSError:
                            continue
            self.index = list(files.values())
            self.index_root, self.index_time = str(comfy_root), time.monotonic()
            return self.index

    def _resolve(self, name, hint, comfy_root, data_dir):
        fallback = {'status': 'not_found', 'url': search_url(name)}
        # An image-provided hash identifies its actual model even after local replacement.
        if hint:
            return self._lookup(hint, data_dir, fallback)
        entries = self._discover(comfy_root)
        key = model_key(name)
        matches = [e for e in entries if key in e['keys']]
        if not matches:
            basename = key.rsplit('/', 1)[-1]
            matches = [e for e in entries if model_key(e['path'].name) == basename]
        if len(matches) != 1:
            return dict(fallback, status='ambiguous' if matches else 'missing')
        entry = matches[0]
        path = entry['path']
        before = path.stat()
        metadata = read_json(path.with_suffix('.metadata.json')) or entry['metadata']
        current = (not metadata.get('modified') or abs(float(metadata['modified']) - before.st_mtime) < 2)
        current = current and (not metadata.get('size') or int(metadata['size']) == before.st_size)
        url = page_url(metadata.get('civitai') or {}) if current else None
        if not url:
            legacy = read_json(path.with_suffix('.civitai.info'))
            # Legacy sidecars have no timestamp; verify their SHA256 before trusting IDs.
        else:
            return {'status': 'matched', 'url': url, 'source': 'LoRA Manager'}
        signature = hashlib.sha256((str(path) + str(before.st_size) + str(before.st_mtime_ns)).encode()).hexdigest()
        digest = self._cache_get(data_dir, 'file:' + signature)
        if not isinstance(digest, str):
            h = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                    h.update(block)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                return dict(fallback, status='changed')
            digest = h.hexdigest()
            self._cache_set(data_dir, 'file:' + signature, digest)
        # Moving/restoring models can change timestamps without changing contents.
        if str(metadata.get('sha256', '')).lower() == digest:
            url = page_url(metadata.get('civitai') or {})
            if url:
                return {'status': 'matched', 'url': url, 'source': 'LoRA Manager'}
        for file in legacy.get('files', []) if isinstance(legacy.get('files'), list) else []:
            if isinstance(file, dict) and str((file.get('hashes') or {}).get('SHA256', '')).lower() == digest:
                url = page_url(legacy)
                if url:
                    return {'status': 'matched', 'url': url, 'source': 'LoRA Manager'}
        return self._lookup(digest, data_dir, fallback)

    def _cache_get(self, directory, key):
        with self.cache_lock:
            item = read_json(directory / 'lora-links-cache.json').get(key)
            if isinstance(item, dict) and time.time() - item.get('time', 0) < 7 * 86400:
                return item.get('value')

    def _cache_set(self, directory, key, value):
        with self.cache_lock:
            path = directory / 'lora-links-cache.json'
            cache = read_json(path)
            cache[key] = {'time': time.time(), 'value': value}
            cache = dict(sorted(cache.items(), key=lambda kv: kv[1].get('time', 0))[-2000:])
            try:
                directory.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix('.tmp')
                tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
                tmp.replace(path)
            except OSError:
                pass

    def _lookup(self, digest, directory, fallback):
        cached = self._cache_get(directory, 'hash:' + digest)
        if isinstance(cached, str) and valid_external_url(cached):
            return {'status': 'matched', 'url': cached, 'source': 'Civitai'}
        unavailable = False
        for host in ('civitai.red', 'civitai.com'):
            request = Request('https://' + host + '/api/v1/model-versions/by-hash/' + digest,
                              headers={'User-Agent': 'ComfyMediaViewer/2.13.2', 'Accept': 'application/json'})
            try:
                with urlopen(request, timeout=12) as response:
                    data = json.loads(response.read(4 * 1024 * 1024))
                url = page_url(data) if isinstance(data, dict) else None
                if url:
                    self._cache_set(directory, 'hash:' + digest, url)
                    return {'status': 'matched', 'url': url, 'source': 'Civitai'}
                unavailable = True
            except HTTPError as error:
                unavailable = unavailable or error.code != 404
            except (OSError, ValueError):
                unavailable = True
        return dict(fallback, status='unavailable' if unavailable else 'not_found')


resolver = Resolver()
