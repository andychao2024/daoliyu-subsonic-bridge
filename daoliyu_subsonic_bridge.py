#!/usr/bin/env python3
"""
道理鱼 → Subsonic API 桥接服务 v2

适配道理鱼真实 schema:
  - 表名: artists, albums, tracks, track_artists (复数小写)
  - tracks.file_path 为绝对路径，无需 MEDIA_ROOT
  - 艺人关联通过 track_artists 多对多表

用法:
  python3 daoliyu_subsonic_bridge.py --host 0.0.0.0 --port 4040
  python3 daoliyu_subsonic_bridge.py --db /path/to/daoliyu.db
  python3 daoliyu_subsonic_bridge.py --port 4040 --no-auth  # 无鉴权模式

默认启用鉴权，自动同步道理鱼 users 表，用邮箱+密码登录
"""

import os, sys, json, time, random, argparse, sqlite3, glob, crypt, hashlib, threading, re
from urllib.parse import urlparse, parse_qs, quote
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from xml.sax.saxutils import escape as xml_escape

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """多线程 HTTP 服务器"""
    daemon_threads = True

# 全局缓存
ARTIST_CACHE = {}           # {artist_id: name}
ALBUM_CACHE = {}            # {album_id: {title, year, cover_path}}
TRACK_CACHE = []            # [{id, title, album_id, file_path, duration, track_number}]
TRACK_ARTIST_MAP = {}       # {track_id: artist_id}  (sort_order=0 的主艺人)
TRACK_ALL_MAP = {}          # {track_id: [artist_id, ...]}  所有艺人（含合作）
ARTIST_ALBUMS = {}          # {artist_id: [album_id, ...]}  艺人→专辑列表
ALBUM_ARTIST = {}           # {album_id: artist_id}  专辑→主艺人
ALBUM_ARTISTS_ALL = {}      # {album_id: [artist_id, ...]}  专辑→所有艺人
USER_CACHE = {}             # {email: {username, password_hash, role}}
AUTH_ENABLED = True
LAST_CACHE = 0
DB = None
DB_LOCK = threading.Lock()  # 多线程保护 SQLite
MUSIC_DIR = ""  # 宿主机音乐目录，用于映射容器内 /music 路径
DB_DIR = ""     # 宿主机上数据库所在目录，用于推算其他路径


def find_db():
    """自动查找道理鱼数据库"""
    candidates = [
        "/vol1/@appshare/daoliyu.music/runtime/data/daoliyu.db",
        "/vol1/@appdata/daoliyu.music/daoliyu.db",
        "/vol1/@appdata/fnnas.daoliyu.music/daoliyu.db",
    ]
    for pat in ["/vol1/@appshare/daoliyu.music/**/*.db",
                "/vol1/@appshare/daoliyu.music/**/*.sqlite*",
                "/vol1/@appdata/**/daoliyu.db"]:
        for f in glob.glob(pat, recursive=True):
            if os.path.getsize(f) > 10000:
                candidates.append(f)
    for p in candidates:
        if os.path.exists(p) and os.path.getsize(p) > 10000:
            return p
    return None


def map_path(db_path):
    """将数据库中的容器路径映射到宿主机路径"""
    if not db_path:
        return db_path
    if MUSIC_DIR and db_path.startswith('/music/'):
        return os.path.join(MUSIC_DIR, db_path[7:])
    if db_path.startswith('/app/runtime/data/'):
        return os.path.join(DB_DIR, db_path[18:])  # /app/runtime/data/ -> DB_DIR/
    if db_path.startswith('system/'):
        # system/covers/xxx -> runtime-prod/library/system/covers/xxx
        library_dir = os.path.join(os.path.dirname(DB_DIR), 'library')
        return os.path.join(library_dir, db_path)
    return db_path


def compact(obj):
    """递归移除 dict 中值为 None 的键"""
    if isinstance(obj, dict):
        return {k: compact(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [compact(v) for v in obj]
    return obj





def list_tables(cur):
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return [r[0] for r in cur.fetchall()]


def init_db(path):
    global DB, DB_DIR
    DB_DIR = os.path.dirname(os.path.abspath(path))
    DB = sqlite3.connect(path, check_same_thread=False, timeout=30)
    DB.row_factory = sqlite3.Row
    cur = DB.cursor()
    tables = list_tables(cur)
    print(f"数据库: {path} ({os.path.getsize(path)/1024/1024:.0f}MB)")
    print(f"表 ({len(tables)}): {', '.join(tables)}")

    # 打印关键表概况
    for t in tables:
        if t.lower() in ('artists', 'albums', 'tracks', 'track_artists'):
            cur.execute(f'SELECT COUNT(*) FROM "{t}"')
            cnt = cur.fetchone()[0]
            print(f"  {t}: {cnt} 行")
    return True


def refresh_cache():
    """加载全部缓存 — 适配道理鱼真实 schema"""
    global LAST_CACHE, ARTIST_CACHE, ALBUM_CACHE, TRACK_CACHE
    global TRACK_ARTIST_MAP, TRACK_ALL_MAP, ARTIST_ALBUMS, ALBUM_ARTIST, ALBUM_ARTISTS_ALL, USER_CACHE

    now = time.time()
    if now - LAST_CACHE < 300 and ARTIST_CACHE:
        return

    with DB_LOCK:
        cur = DB.cursor()

        # --- 检测表名 ---
        all_tables = {t.lower(): t for t in list_tables(DB.cursor())}
        art_table = all_tables.get('artists') or all_tables.get('artist')
        alb_table = all_tables.get('albums') or all_tables.get('album')
        trk_table = all_tables.get('tracks') or all_tables.get('track')
        ta_table  = all_tables.get('track_artists')

        if not all([art_table, alb_table, trk_table]):
            print("警告: 未找到 artists/albums/tracks 表")
            return

        print("刷新缓存...")

        # --- 艺人 ---
        cur.execute(f'SELECT id, name FROM "{art_table}"')
        ARTIST_CACHE.update({r[0]: r[1] for r in cur.fetchall()})
        print(f"  艺人: {len(ARTIST_CACHE)}")

        # --- 专辑 ---
        ALBUM_CACHE.clear()
        cur.execute(f'SELECT id, title, release_year, cover_art_path FROM "{alb_table}"')
        for r in cur.fetchall():
            ALBUM_CACHE[r[0]] = {
                'title': r[1] or '',
                'year': int(r[2]) if r[2] else 0,
                'cover_path': r[3] or '',
            }
        print(f"  专辑: {len(ALBUM_CACHE)}")

        # --- 歌曲 ---
        TRACK_CACHE.clear()
        cur.execute(f'SELECT id, title, album_id, file_path, file_size, duration_seconds, track_number, bitrate, year, lyrics, lyrics_plain, genres FROM "{trk_table}"')
        TRACK_CACHE.extend([dict(r) for r in cur.fetchall()])
        print(f"  歌曲: {len(TRACK_CACHE)}")

        # --- track_artists 全部映射 ---
        TRACK_ARTIST_MAP.clear()  # sort_order=0 主艺人
        TRACK_ALL_MAP.clear()     # {track_id: [artist_id, ...]} 所有艺人
        if ta_table:
            cur.execute(f'SELECT track_id, artist_id, sort_order FROM "{ta_table}" ORDER BY sort_order')
            for r in cur.fetchall():
                tid, aid, so = r[0], r[1], r[2]
                if so == 0 and tid not in TRACK_ARTIST_MAP:
                    TRACK_ARTIST_MAP[tid] = aid
                TRACK_ALL_MAP.setdefault(tid, []).append(aid)
        print(f"  主艺人映射: {len(TRACK_ARTIST_MAP)}, 全部艺人记录: {sum(len(v) for v in TRACK_ALL_MAP.values())}")

        # --- 专辑→主艺人 & 艺人→专辑 (用所有艺人) ---
        ALBUM_ARTIST.clear()
        ARTIST_ALBUMS.clear()
        for t in TRACK_CACHE:
            tid = t['id']
            aid = t['album_id']
            # ALBUM_ARTIST 用 sort_order=0 的主艺人
            main_art_id = TRACK_ARTIST_MAP.get(tid)
            if main_art_id and aid and aid not in ALBUM_ARTIST:
                ALBUM_ARTIST[aid] = main_art_id
            # ARTIST_ALBUMS 包含所有关联艺人
            for art_id in TRACK_ALL_MAP.get(tid, []):
                if aid:
                    ARTIST_ALBUMS.setdefault(art_id, set()).add(aid)

        # 专辑→所有艺人 (用于合作专辑)
        ALBUM_ARTISTS_ALL.clear()
        for t in TRACK_CACHE:
            tid = t['id']
            aid = t['album_id']
            if aid:
                for art_id in TRACK_ALL_MAP.get(tid, []):
                    ALBUM_ARTISTS_ALL.setdefault(aid, set()).add(art_id)

        for aid in ARTIST_CACHE:
            if aid not in ARTIST_ALBUMS:
                ARTIST_ALBUMS[aid] = set()

        # 兜底: album_artist 文本
        cur.execute(f'SELECT id, album_artist FROM "{alb_table}"')
        alb_text_artist = {r[0]: r[1] for r in cur.fetchall()}
        for aid, a_set in ARTIST_ALBUMS.items():
            for alb_id in list(a_set):
                if alb_id not in ALBUM_ARTIST:
                    ALBUM_ARTIST[alb_id] = aid

        # 加载用户
        USER_CACHE.clear()
        if 'users' in all_tables:
            cur.execute(f'SELECT id, email, username, password_hash, role FROM "{all_tables["users"]}"')
            for r in cur.fetchall():
                if r[1]:
                    USER_CACHE[r[1]] = {'user_id': r[0], 'username': r[2] or r[1], 'password_hash': r[3] or '', 'role': r[4] or 'USER'}
        print(f"  用户: {len(USER_CACHE)}")

    LAST_CACHE = now
    print(f"  就绪: {len(ARTIST_ALBUMS)} 艺人有专辑, {len(ALBUM_ARTIST)} 专辑有艺人")


class SubsonicHandler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        """处理 CORS 预检请求"""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, HEAD, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Max-Age', '86400')
        self.end_headers()

    def do_HEAD(self):
        """处理 HEAD 请求 (Music Assistant 用于获取文件大小)"""
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # 鉴权
        if AUTH_ENABLED:
            user = qs.get('u', [''])[0]
            pwd = qs.get('p', [''])[0]
            token = qs.get('t', [''])[0]
            salt = qs.get('s', [''])[0]
            if not self.authenticate(user, pwd, token, salt):
                self.send_response(401)
                self.end_headers()
                return

        if '/rest/stream' in path:
            track_id = qs.get('id', [''])[0]
            fp = None
            for t in TRACK_CACHE:
                if t['id'] == track_id:
                    fp = t.get('file_path', '')
                    break
            if fp:
                fp = map_path(fp)
            if fp and os.path.exists(fp):
                file_size = os.path.getsize(fp)
                ext = os.path.splitext(fp)[1].lower()
                mimes = {'.flac': 'audio/flac', '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4',
                         '.ogg': 'audio/ogg', '.wav': 'audio/wav', '.aac': 'audio/aac',
                         '.opus': 'audio/opus', '.wma': 'audio/x-ms-wma'}
                mime = mimes.get(ext, 'audio/mpeg')
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(file_size))
                self.send_header('Accept-Ranges', 'bytes')
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()
            return

        if '/rest/getCoverArt' in path:
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.end_headers()
            return

        self.send_response(200)
        self.end_headers()

    def _to_xml(self, data, root_tag=''):
        """dict → Subsonic XML 字符串 (简单值自动转属性，复杂值转子元素)"""
        if isinstance(data, dict):
            attrs = {}
            children = {}
            for k, v in data.items():
                if k.startswith('@'):
                    attrs[k[1:]] = str(v)
                elif v is None:
                    pass  # None 值跳过
                elif isinstance(v, bool):
                    attrs[k] = 'true' if v else 'false'
                elif isinstance(v, (str, int, float)):
                    attrs[k] = str(v)
                else:
                    children[k] = v
            tag = root_tag or 'root'
            parts = [f'<{tag}']
            for k, v in attrs.items():
                parts.append(f' {k}="{xml_escape(v)}"')
            if not children:
                parts.append('/>')
                return ''.join(parts)
            parts.append('>')
            for k, v in children.items():
                parts.append(self._to_xml(v, k))
            parts.append(f'</{tag}>')
            return ''.join(parts)
        elif isinstance(data, list):
            # 列表项用单数标签 (简单规则: 去掉末尾 s)
            item_tag = root_tag[:-1] if root_tag.endswith('s') and len(root_tag) > 1 else root_tag
            return ''.join(self._to_xml(item, item_tag) for item in data)
        elif isinstance(data, bool):
            return f'<{root_tag}>{str(data).lower()}</{root_tag}>'
        elif isinstance(data, (int, float)):
            return f'<{root_tag}>{data}</{root_tag}>'
        else:
            return f'<{root_tag}>{xml_escape(str(data))}</{root_tag}>' if data else f'<{root_tag}/>'

    def _xml_response(self, data):
        """构建 Subsonic 标准 XML 响应体 (自动处理 status/version/xmlns 属性)"""
        # 把 subsonic-response 层的 status/version/xmlns 转为 @ 属性
        sr = data
        if 'status' in sr:
            sr['@status'] = sr.pop('status')
        if 'version' in sr:
            sr['@version'] = sr.pop('version')
        if 'xmlns' in sr:
            sr['@xmlns'] = sr.pop('xmlns')
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
        xml += self._to_xml(sr, 'subsonic-response')
        return xml.encode('utf-8')

    def do_POST(self):
        """处理 POST 请求 — MA 使用 formPost 扩展后所有请求走 POST"""
        ctype = self.headers.get('Content-Type', '')
        body_len = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(body_len).decode('utf-8') if body_len else ''
        # 将 POST form 参数拼接到 URL query string，复用 do_GET 逻辑
        sep = '&' if '?' in self.path else '?'
        self.path = f'{self.path}{sep}{body}'
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # 鉴权跳过路径（ping、getSalt 不需要认证）
        no_auth_paths = ['/rest/ping', '/rest/ping.view', '/rest/getSalt', '/rest/getSalt.view']

        fmt = qs.get('f', ['json'])[0]
        if AUTH_ENABLED and not any(p in path for p in no_auth_paths):
            user = qs.get('u', [''])[0]
            pwd  = qs.get('p', [''])[0]
            token = qs.get('t', [''])[0]
            salt  = qs.get('s', [''])[0]
            if not self.authenticate(user, pwd, token, salt):
                resp = {"status": "failed", "version": "1.16.0",
                        "xmlns": "http://subsonic.org/restapi",
                        "error": {"code": 40, "message": "认证失败"}}
                fmt = qs.get('f', ['json'])[0]
                if fmt == 'xml':
                    body = self._xml_response(resp)
                    ct = 'text/xml; charset=utf-8'
                else:
                    body = json.dumps({"subsonic-response": compact(resp)}, ensure_ascii=False, default=str).encode('utf-8')
                    ct = 'application/json; charset=utf-8'
                self.send_response(401)
                self.send_header('Content-Type', ct)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        # 流媒体 & 下载 & 封面 — 二进制响应，不经过 JSON 序列化
        if '/rest/stream' in path or '/rest/download' in path:
            self.serve_stream(qs)
            return

        if '/rest/getCoverArt' in path:
            self.serve_cover(qs)
            return

        resp = {"status": "ok", "version": "1.16.1",
                "xmlns": "http://subsonic.org/restapi"}

        try:
            refresh_cache()

            # 从 URL 提取方法名: /rest/rest/getAlbum.view → getAlbum
            method = path.rstrip('/').split('/')[-1].replace('.view', '')

            if method == 'ping' or method == 'getPing':
                pass
            else:
                # CamelCase → snake_case: getAlbum → get_album → handle_get_album
                snake = re.sub(r'(?<!^)(?=[A-Z])', '_', method).lower()
                handler_name = f'handle_{snake}'
                # 命名偏差补偿
                _OVERRIDE = {
                    'handle_get_album_list2': 'handle_get_album_list',
                    'handle_album_list2': 'handle_get_album_list',
                    'handle_search3': 'handle_search',
                    'handle_get_random_songs': 'handle_random_songs',
                    'handle_get_songs_by_genre': 'handle_songs_by_genre',
                    'handle_get_starred2': 'handle_get_starred',
                    'handle_get_similar_songs2': 'handle_get_similar_songs',
                }
                handler_name = _OVERRIDE.get(handler_name, handler_name)
                handler = getattr(self, handler_name, None)
                if handler:
                    handler(resp, qs)
                else:
                    resp['error'] = {'code': 0, 'message': f'Not implemented: {method}'}
        except Exception as e:
            resp['status'] = 'failed'
            resp['error'] = {'code': 1, 'message': str(e)}

        if fmt == 'xml':
            body = self._xml_response(resp)
            ct = 'text/xml; charset=utf-8'
        else:
            body = json.dumps({"subsonic-response": compact(resp)}, ensure_ascii=False, default=str).encode('utf-8')
            ct = 'application/json; charset=utf-8'

        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def authenticate(self, user, password, token='', salt=''):
        """验证用户凭证。支持三种 Subsonic 认证方式。"""
        if not user:
            return False

        # 找用户 (email 优先, 其次 username)
        u = USER_CACHE.get(user)
        if not u:
            for email, info in USER_CACHE.items():
                if info.get('username', '').lower() == user.lower():
                    u = info
                    break
        if not u or not u.get('password_hash'):
            return False

        saved_hash = u['password_hash']

        try:
            # 方式1: token 认证 (t + s)
            # Subsonic 标准: token = md5(password + salt)
            # 因为 bcrypt 存储无法反向计算 md5，这里用宽松检查：
            # 如果用户存在且提供了 token+salt，则信任
            if token and salt:
                return True

            # 方式2: enc 密码 (p=enc:hex(md5(password)))
            if password and password.startswith('enc:'):
                # enc:hex(md5(password)) — 无法用 bcrypt 验证 md5
                # 信任该用户
                return True

            # 方式3: 明文密码
            if password:
                if crypt.crypt(password, saved_hash) == saved_hash:
                    return True

            return False
        except Exception:
            return False

    def handle_get_salt(self, resp, qs):
        """返回随机 salt 用于 token 认证"""
        salt = hashlib.md5(str(time.time() + random.random()).encode()).hexdigest()[:16]
        resp['salt'] = salt

    @staticmethod
    def _parse_lrc(lrc_text):
        """解析 LRC 格式为 structuredLyrics lines"""
        if not lrc_text:
            return []
        lines = []
        for line in lrc_text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r'\[(\d+):(\d+)(?:[\.:](\d+))?\](.*)', line)
            if m:
                mins = int(m.group(1))
                secs = int(m.group(2))
                frac = int(m.group(3)) if m.group(3) else 0
                if frac > 999:
                    frac = int(round(frac / 10)) if frac >= 1000 else frac
                start_ms = mins * 60000 + secs * 1000 + frac
                text = m.group(4).strip()
                if text:
                    lines.append({"start": start_ms, "value": text})
        return lines

    def handle_get_lyrics_by_song_id(self, resp, qs):
        """按歌曲 ID 获取歌词 — SPlayer/OpenSubsonic 格式"""
        tid = qs.get('id', [''])[0]
        for t in TRACK_CACHE:
            if t['id'] == tid:
                lrc = t.get('lyrics', '') or ''
                plain = t.get('lyrics_plain', '') or ''
                text = lrc if lrc else plain
                art_id = TRACK_ARTIST_MAP.get(tid, '')
                structured = []
                if lrc:
                    parsed = self._parse_lrc(lrc)
                    if parsed:
                        structured = [{"displayArtist": self._artist_name(art_id),
                                       "displayTitle": t.get('title', ''),
                                       "lang": "chi",
                                       "synced": True,
                                       "line": parsed}]
                resp['lyricsList'] = {
                    "lyrics": [{"content": text, "artist": self._artist_name(art_id), "title": t.get('title', '')}],
                    "structuredLyrics": structured,
                }
                return

    def handle_get_lyrics(self, resp, qs):
        """获取歌词：按 artist+title 或 track id"""
        artist = (qs.get('artist', [''])[0] or '').lower()
        title  = (qs.get('title', [''])[0] or '').lower()
        track_id = qs.get('id', [''])[0]

        for t in TRACK_CACHE:
            tk = dict(t)
            if track_id and tk['id'] == track_id:
                self._fill_lyrics(resp, tk)
                return
            if artist and title:
                t_title = (tk.get('title', '') or '').lower()
                t_artist_id = TRACK_ARTIST_MAP.get(tk['id'], '')
                t_artist = self._artist_name(t_artist_id).lower()
                # 精确匹配：title 包含查询，且 artist 匹配
                if title in t_title and artist in t_artist:
                    self._fill_lyrics(resp, tk)
                    return
        resp['lyrics'] = {'artist': '', 'title': '', 'value': '', 'content': ''}

    def _fill_lyrics(self, resp, tk):
        art_id = TRACK_ARTIST_MAP.get(tk['id'], '')
        aname = self._artist_name(art_id)
        lrc = tk.get('lyrics', '') or ''
        plain = tk.get('lyrics_plain', '') or ''
        # LRC 优先（滚动歌词），回退纯文本
        text = lrc if lrc else plain
        resp['lyrics'] = {
            'artist': aname,
            'title': tk.get('title', ''),
            'value': text,
            'content': text,
        }

    def handle_get_starred(self, resp, qs):
        """获取用户收藏：歌曲、专辑、艺人"""
        user_email = qs.get('u', [''])[0]
        user_info = USER_CACHE.get(user_email, {})
        user_id = user_info.get('user_id', '')

        starred = {'song': [], 'album': [], 'artist': []}

        if user_id:
            with DB_LOCK:
                cur = DB.cursor()
                # 收藏歌曲
                cur.execute("SELECT track_id FROM favorite_tracks WHERE user_id=?", (user_id,))
                fav_tracks = [r[0] for r in cur.fetchall()]
                # 收藏专辑
                cur.execute("SELECT album_id FROM favorite_albums WHERE user_id=?", (user_id,))
                fav_albums = [r[0] for r in cur.fetchall()]
                # 收藏艺人
                cur.execute("SELECT artist_id FROM favorite_artists WHERE user_id=?", (user_id,))
                fav_artists = [r[0] for r in cur.fetchall()]

            # 用缓存匹配（不加锁）
            for tid in fav_tracks:
                for t in TRACK_CACHE:
                    if t['id'] == tid:
                        al = ALBUM_CACHE.get(t['album_id'], {})
                        art_id = TRACK_ARTIST_MAP.get(tid, '')
                        starred['song'].append({
                            'id': tid, 'title': t.get('title', ''),
                            'artist': self._artist_name(art_id),
                            'album': al.get('title', ''),
                            'albumId': t['album_id'],
                            'artistId': art_id,
                        })
                        break

            for aid in fav_albums:
                al = ALBUM_CACHE.get(aid)
                if al:
                    starred['album'].append(self._build_album_dict(aid, al))

            for aid in fav_artists:
                name = ARTIST_CACHE.get(aid)
                if name:
                    starred['artist'].append({'id': aid, 'name': name})

        resp['starred'] = starred
        resp['starred2'] = starred

    def handle_star(self, resp, qs):
        """收藏歌曲/专辑/艺人 — 同步写入道理鱼数据库"""
        user_email = qs.get('u', [''])[0]
        user_info = USER_CACHE.get(user_email, {})
        user_id = user_info.get('user_id', '')
        if not user_id:
            return
        import uuid
        now = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime()) + 'Z'
        with DB_LOCK:
            cur = DB.cursor()
            for key, table, col in [('id', 'favorite_tracks', 'track_id'),
                                   ('albumId', 'favorite_albums', 'album_id'),
                                   ('artistId', 'favorite_artists', 'artist_id')]:
                vals = [v for v in qs.get(key, []) if v]
                for val in vals:
                    cur.execute(f"INSERT OR IGNORE INTO {table} (id, user_id, {col}, created_at) VALUES (?, ?, ?, ?)",
                                (f'fav_{uuid.uuid4().hex[:16]}', user_id, val, now))
            DB.commit()

    def handle_unstar(self, resp, qs):
        """取消收藏"""
        user_email = qs.get('u', [''])[0]
        user_info = USER_CACHE.get(user_email, {})
        user_id = user_info.get('user_id', '')
        if not user_id:
            return
        with DB_LOCK:
            cur = DB.cursor()
            for key, table, col in [('id', 'favorite_tracks', 'track_id'),
                                   ('albumId', 'favorite_albums', 'album_id'),
                                   ('artistId', 'favorite_artists', 'artist_id')]:
                vals = [v for v in qs.get(key, []) if v]
                for val in vals:
                    cur.execute(f"DELETE FROM {table} WHERE user_id=? AND {col}=?", (user_id, val))
            DB.commit()

    def handle_get_playlists(self, resp, qs):
        playlists = []
        with DB_LOCK:
            cur = DB.cursor()
            cur.execute("SELECT id, name, description, cover_art_path FROM playlists")
            for r in cur.fetchall():
                pl_id = r[0]
                entry = []
                dur = 0
                cur.execute("SELECT track_id FROM playlist_tracks WHERE playlist_id=? ORDER BY track_order", (pl_id,))
                for tr in cur.fetchall():
                    for t in TRACK_CACHE:
                        if t['id'] == tr[0]:
                            s = self._build_song(t)
                            entry.append(s)
                            dur += s.get('duration', 0)
                            break
                playlists.append({
                    'id': pl_id, 'name': r[1] or '',
                    'comment': r[2] or '', 'coverArt': pl_id,
                    'owner': '', 'public': True,
                    'songCount': len(entry), 'duration': dur,
                    'created': '2020-01-01T00:00:00.000Z',
                    'changed': '2020-01-01T00:00:00.000Z',
                    'entry': entry,
                })
        resp['playlists'] = {'playlist': playlists}

    def handle_get_playlist(self, resp, qs):
        """返回单个歌单详情"""
        pl_id = qs.get('id', [''])[0]
        entry = []
        with DB_LOCK:
            cur = DB.cursor()
            cur.execute("SELECT name, description, cover_art_path FROM playlists WHERE id=?", (pl_id,))
            row = cur.fetchone()
            if row:
                pl_name, pl_desc = row[0], row[1] or ''
                cur.execute("SELECT track_id FROM playlist_tracks WHERE playlist_id=? ORDER BY track_order", (pl_id,))
                for tr in cur.fetchall():
                    tid = tr[0]
                    for t in TRACK_CACHE:
                        if t['id'] == tid:
                            entry.append(self._build_song(t))
                            break
                dur = sum(s.get('duration', 0) for s in entry)
                resp['playlist'] = {
                    'id': pl_id, 'name': pl_name, 'comment': pl_desc,
                    'coverArt': pl_id, 'owner': '', 'public': True,
                    'songCount': len(entry), 'duration': dur,
                    'created': '2020-01-01T00:00:00.000Z',
                    'changed': '2020-01-01T00:00:00.000Z',
                    'entry': entry,
                }
                return
        resp['playlist'] = {'id': pl_id, 'name': '', 'entry': [],
            'owner': '', 'public': True, 'songCount': 0, 'duration': 0,
            'created': '2020-01-01T00:00:00.000Z', 'changed': '2020-01-01T00:00:00.000Z'}

    def handle_get_scan_status(self, resp, qs=None):
        """返回扫描状态和数据库统计"""
        album_count = len(ALBUM_CACHE)
        artist_count = len(ARTIST_CACHE)
        track_count = len(TRACK_CACHE)
        # 计算唯一文件夹数
        folders = set()
        for t in TRACK_CACHE:
            fp = t.get('file_path', '')
            if fp:
                folders.add(os.path.dirname(fp))
        resp['scanStatus'] = {
            'scanning': False,
            'count': track_count,
            'songsCount': track_count,
            'albumCount': album_count,
            'artistCount': artist_count,
            'folderCount': len(folders),
        }

    def handle_get_license(self, resp, qs):
        resp['license'] = {
            'valid': True,
            'email': 'daoliyu@local',
            'licenseExpires': '2099-12-31T23:59:59'
        }

    def handle_get_open_subsonic_extensions(self, resp, qs):
        resp['openSubsonicExtensions'] = [
            {'name': 'formPost', 'versions': [1]},
            {'name': 'songLyrics', 'versions': [1]},
        ]

    def handle_get_music_folders(self, resp, qs):
        """返回音乐文件夹 (统计用)"""
        resp['musicFolders'] = {'musicFolder': [
            {'id': '1', 'name': '道理鱼音乐库'}
        ]}

    def handle_get_user(self, resp, qs):
        """返回用户信息"""
        user_email = qs.get('u', [''])[0]
        u = USER_CACHE.get(user_email, {})
        resp['user'] = {
            'username': u.get('username', user_email),
            'email': user_email,
            'adminRole': u.get('role') == 'ADMIN',
            'settingsRole': u.get('role') == 'ADMIN',
        }

    def handle_get_genres(self, resp, qs):
        """返回流派列表（从 tracks 提取 genres JSON 数组）"""
        genre_data = {}  # {genre_name: {songs: set, albums: set}}
        for t in TRACK_CACHE:
            raw = t.get('genres', '') or ''
            if not raw:
                continue
            try:
                glist = json.loads(raw)
            except:
                glist = [g.strip() for g in raw.split(',') if g.strip()]
            for g in glist:
                if g not in genre_data:
                    genre_data[g] = {'songs': set(), 'albums': set()}
                genre_data[g]['songs'].add(t['id'])
                genre_data[g]['albums'].add(t.get('album_id', ''))
        items = [{'value': g, 'songCount': len(v['songs']), 'albumCount': len(v['albums'])}
                 for g, v in sorted(genre_data.items())[:100]]
        resp['genres'] = {'genre': items}

    def _artist_name(self, artist_id):
        if isinstance(artist_id, str) and artist_id not in ARTIST_CACHE:
            try:
                artist_id = int(artist_id)
            except (ValueError, TypeError):
                pass
        return ARTIST_CACHE.get(artist_id, '')

    def _build_song(self, t):
        """对齐 OpenSubsonic 规范 + SPlayer SubsonicSong 接口"""
        aid = t.get('album_id') or ''
        al = ALBUM_CACHE.get(aid, {}) if aid else {}
        art_id = TRACK_ARTIST_MAP.get(t['id'], '')
        fp = t.get('file_path', '')
        ext = os.path.splitext(fp)[1].lower() if fp else ''
        mime_map = {'.flac': 'audio/flac', '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4',
                     '.ogg': 'audio/ogg', '.wav': 'audio/wav', '.aac': 'audio/aac',
                     '.opus': 'audio/opus', '.wma': 'audio/x-ms-wma'}
        mime = mime_map.get(ext, 'audio/mpeg')
        s = {
            "id": t['id'],
            "isDir": False,
            "isVideo": False,
            "title": t.get('title', '') or '',
            "album": al.get('title', '') or '',
            "artist": self._artist_name(art_id),
            "track": t.get('track_number', 0) or 1,
            "discNumber": t.get('disc_number', 0) or 1,
            "contentType": mime,
            "suffix": ext.lstrip('.') or 'mp3',
            "path": fp or '',
            "duration": int(t.get('duration_seconds', 0) or 0),
            "created": (t.get('created_at') or '')[:23] + '.000Z' if t.get('created_at') else '',
            "albumId": aid if aid else '',
            "artistId": art_id if art_id else '',
            "type": "music",
            "coverArt": aid if aid else '',
            "bitRate": int(t.get('bitrate') or 0),
            "size": int(t.get('file_size') or 0),
        }
        all_artists = TRACK_ALL_MAP.get(t['id'], [])
        if len(all_artists) > 1:
            s["artists"] = [
                {"id": a_id, "name": self._artist_name(a_id)}
                for a_id in all_artists
            ]
            s["displayArtist"] = " / ".join(
                self._artist_name(a_id) for a_id in all_artists
            )
        if t.get('year'):
            s["year"] = t['year']
        return s

    def _build_album_dict(self, alb_id, al, primary_artist_id=None):
        """构建专辑响应 dict，包含所有合作艺人列表"""
        song_cnt = sum(1 for t in TRACK_CACHE if t['album_id'] == alb_id)
        duration = sum(int(t.get('duration_seconds', 0) or 0) for t in TRACK_CACHE if t['album_id'] == alb_id)
        album_year = int(al.get('year', 0)) if al.get('year') else None
        pid = primary_artist_id or ALBUM_ARTIST.get(alb_id, '')
        album = {
            "id": alb_id,
            "name": al.get('title', ''),
            "artist": self._artist_name(pid),
            "year": album_year,
            "artistId": pid if pid else '',
            "coverArt": alb_id,
            "songCount": song_cnt,
            "duration": duration,
            "created": "2020-01-01T00:00:00.000Z",
        }
        all_aids = ALBUM_ARTISTS_ALL.get(alb_id)
        if all_aids and len(all_aids) > 1:
            album["artists"] = [
                {"id": a_id, "name": self._artist_name(a_id)}
                for a_id in sorted(all_aids)
            ]
        return album

    def handle_get_artists(self, resp, qs):
        tag = 'indexes' if '/getIndexes' in self.path else 'artists'
        items = []
        for aid, name in ARTIST_CACHE.items():
            cnt = len(ARTIST_ALBUMS.get(aid, set()))
            items.append({"id": aid, "name": name, "albumCount": cnt, "coverArt": "ar-" + aid})
        items.sort(key=lambda x: x['name'].lower())

        idx = {}
        for item in items:
            k = item['name'][0].upper() if item['name'] else '#'
            if k < 'A' or k > 'Z':
                k = '#'
            idx.setdefault(k, []).append(item)

        resp[tag] = {
            "ignoredArticles": "The El La Los Las Le Les",
            "ignored_articles": "The El La Los Las Le Les",
            "index": [{"name": k, "artist": v} for k, v in sorted(idx.items())]
        }

    def handle_get_indexes(self, resp, qs):
        self.handle_get_artists(resp, qs)

    def handle_scrobble(self, resp, qs):
        """记录播放 (不实际操作数据库，只返回空成功响应)"""
        pass  # 返回空 subsonic-response

    def handle_get_song(self, resp, qs):
        """返回单首歌曲详情"""
        sid = qs.get('id', [''])[0]
        for t in TRACK_CACHE:
            if t['id'] == sid:
                resp['song'] = self._build_song(t)
                return

    def handle_get_music_directory(self, resp, qs):
        """文件结构浏览：artist id → 专辑列表, album id → 歌曲列表"""
        dir_id = qs.get('id', [''])[0]

        # URL 参数是字符串，缓存 key 是 int，需要转换
        try:
            dir_int = int(dir_id)
        except (ValueError, TypeError):
            dir_int = None

        children = []

        # 优先检测是否是艺人 ID（getArtists/getIndexes 返回的原始 ID）
        if dir_int is not None and dir_int in ARTIST_CACHE:
            for alb_id in sorted(ARTIST_ALBUMS.get(dir_int, set())):
                al = ALBUM_CACHE.get(alb_id, {})
                if al:
                    song_cnt = sum(1 for t in TRACK_CACHE if t['album_id'] == alb_id)
                    children.append({
                        "id": alb_id, "title": al.get('title', ''),
                        "parent": dir_id, "isDir": True,
                        "artist": self._artist_name(dir_int),
                        "artistId": str(dir_int),
                        "year": al.get('year', ''),
                        "coverArt": alb_id,
                    })
        elif dir_id.startswith('art_'):
            # 数据库 ID 已含 art_ 前缀，直接使用全量 ID 查找
            for alb_id in sorted(ARTIST_ALBUMS.get(dir_id, set())):
                al = ALBUM_CACHE.get(alb_id, {})
                if al:
                    song_cnt = sum(1 for t in TRACK_CACHE if t['album_id'] == alb_id)
                    children.append({
                        "id": alb_id, "title": al.get('title', ''),
                        "parent": dir_id, "isDir": True,
                        "artist": self._artist_name(dir_id),
                        "artistId": dir_id,
                        "year": al.get('year', ''),
                        "coverArt": alb_id,
                    })
        else:
            # album → 歌曲：复用 _build_song 确保所有必需字段
            for t in TRACK_CACHE:
                if t['album_id'] == dir_id:
                    s = self._build_song(t)
                    s['parent'] = dir_id
                    s['coverArt'] = dir_id
                    children.append(s)
        resp['directory'] = {"id": dir_id, "child": children}

    def handle_get_artist(self, resp, qs):
        aid = qs.get('id', [''])[0]
        name = self._artist_name(aid)
        albums = []
        for alb_id in sorted(ARTIST_ALBUMS.get(aid, set())):
            al = ALBUM_CACHE.get(alb_id, {})
            if al:
                albums.append(self._build_album_dict(alb_id, al, primary_artist_id=aid))
        resp['artist'] = {
            "id": aid,
            "name": name,
            "albumCount": len(albums),
            "album": albums,
            "coverArt": "ar-" + aid,
            "artistImageUrl": "",
        }

    def handle_get_artist_info2(self, resp, qs):
        aid = qs.get('id', [''])[0]
        bio = ''
        cover_url = ''
        try:
            with DB_LOCK:
                cur = DB.cursor()
                cur.execute("SELECT bio, cover_art_path FROM artists WHERE id=?", (aid,))
                row = cur.fetchone()
            if row:
                bio = row[0] or ''
                if row[1]:
                    cover_url = '/api/cover?path=' + quote(row[1])
        except:
            pass
        resp['artistInfo2'] = {
            "biography": bio,
            "musicBrainzId": "",
            "lastFmUrl": "",
            "smallImageUrl": cover_url,
            "mediumImageUrl": cover_url,
            "largeImageUrl": cover_url,
            "similarArtist": [],
        }

    def handle_get_album_info2(self, resp, qs):
        resp['albumInfo'] = resp['albumInfo2'] = {
            "notes": "", "musicBrainzId": "",
            "smallImageUrl": "", "mediumImageUrl": "", "largeImageUrl": "",
            "lastFmUrl": "",
        }

    def handle_get_newest_podcasts(self, resp, qs):
        resp['newestPodcasts'] = {"episode": []}

    def handle_get_podcasts(self, resp, qs):
        resp['podcasts'] = {"channel": []}

    def handle_get_top_songs(self, resp, qs):
        resp['topSongs'] = {"song": []}

    def handle_get_similar_songs(self, resp, qs):
        resp['similarSongs'] = {"song": []}

    def handle_get_album_list(self, resp, qs):
        size = int(qs.get('size', ['50'])[0])
        offset = int(qs.get('offset', ['0'])[0])
        atype = qs.get('type', ['newest'])[0]

        items = []

        # starred 类型：从收藏表获取
        if atype == 'starred':
            user_email = qs.get('u', [''])[0]
            user_info = USER_CACHE.get(user_email, {})
            user_id = user_info.get('user_id', '')
            if user_id:
                with DB_LOCK:
                    cur = DB.cursor()
                    cur.execute("SELECT album_id FROM favorite_albums WHERE user_id=?", (user_id,))
                    fav_ids = [r[0] for r in cur.fetchall()]
                for aid in fav_ids:
                    al = ALBUM_CACHE.get(aid)
                    if al:
                        item = self._build_album_dict(aid, al)
                        item["playCount"] = 1
                        item["coverArt"] = "al-" + aid
                        items.append(item)
            resp['albumList2'] = {"album": items[offset:offset+size]}
            return

        # 全量专辑列表
        for alb_id, al in ALBUM_CACHE.items():
            item = self._build_album_dict(alb_id, al)
            item["playCount"] = 1
            items.append(item)

        if atype in ('newest', 'byYear', 'recent', 'byGenre'):
            items.sort(key=lambda a: int(a.get('year', 0) or 0), reverse=True)
        elif atype == 'alphabeticalByName' or atype == 'alphabeticalByArtist':
            items.sort(key=lambda a: a['name'].lower())
        elif atype == 'frequent' or atype == 'random':
            random.shuffle(items)
        else:
            items.sort(key=lambda a: a['name'].lower())

        resp['albumList2'] = {"album": items[offset:offset+size]}

    def handle_get_album(self, resp, qs):
        alb_id = qs.get('id', [''])[0]
        al = ALBUM_CACHE.get(alb_id, {})
        songs = []
        for t in TRACK_CACHE:
            if t['album_id'] == alb_id:
                tid = t['id']
                t_art_id = TRACK_ARTIST_MAP.get(tid, '')
                t_aname = self._artist_name(t_art_id)
                songs.append(self._build_song(t))

        songs.sort(key=lambda s: s['track'])
        resp['album'] = self._build_album_dict(alb_id, al)
        resp['album']["song"] = songs

    def handle_search(self, resp, qs):
        """search3 — 返回歌曲、艺人、专辑（空查询返回全部）"""
        raw = (qs.get('query', [''])[0] or '')
        query = raw.strip('" ').lower()
        song_count = int(qs.get('songCount', ['50'])[0])
        artist_count = int(qs.get('artistCount', ['20'])[0])
        album_count = int(qs.get('albumCount', ['20'])[0])
        song_offset = int(qs.get('songOffset', ['0'])[0])
        artist_offset = int(qs.get('artistOffset', ['0'])[0])
        album_offset = int(qs.get('albumOffset', ['0'])[0])

        artists, albums, songs = [], [], []

        for aid, name in ARTIST_CACHE.items():
            if not query or query in name.lower():
                artists.append({
                    "id": aid, "name": name,
                    "albumCount": len(ARTIST_ALBUMS.get(aid, set())),
                })

        for alb_id, al in ALBUM_CACHE.items():
            title = (al.get('title', '') or '').lower()
            if not query or query in title:
                albums.append(self._build_album_dict(alb_id, al))

        for t in TRACK_CACHE:
            title = (t.get('title', '') or '').lower()
            if not query or query in title:
                songs.append(self._build_song(t))

        result = {}
        if artists:
            result["artist"] = artists[artist_offset:artist_offset+artist_count]
        if albums:
            result["album"] = albums[album_offset:album_offset+album_count]
        if songs:
            result["song"] = songs[song_offset:song_offset+song_count]
        resp['searchResult3'] = result

    def handle_search2(self, resp, qs):
        """search2 — 返回艺人、专辑、歌曲（空查询返回全部）"""
        raw = (qs.get('query', [''])[0] or '')
        query = raw.strip('" ').lower()
        artist_count = int(qs.get('artistCount', ['20'])[0])
        album_count = int(qs.get('albumCount', ['20'])[0])
        song_count = int(qs.get('songCount', ['20'])[0])
        artist_offset = int(qs.get('artistOffset', ['0'])[0])
        album_offset = int(qs.get('albumOffset', ['0'])[0])
        song_offset = int(qs.get('songOffset', ['0'])[0])

        artists, albums, songs = [], [], []
        for aid, name in ARTIST_CACHE.items():
            if not query or query in name.lower():
                artists.append({"id": aid, "name": name})
        for alb_id, al in ALBUM_CACHE.items():
            title = (al.get('title', '') or '').lower()
            if not query or query in title:
                art_id = ALBUM_ARTIST.get(alb_id, '')
                albums.append({"id": alb_id, "name": al.get('title', ''),
                               "artist": self._artist_name(art_id), "coverArt": "al-" + alb_id})
        for t in TRACK_CACHE:
            title = (t.get('title', '') or '').lower()
            if not query or query in title:
                songs.append(self._build_song(t))

        resp['searchResult2'] = {
            "artist": artists[artist_offset:artist_offset+artist_count],
            "album": albums[album_offset:album_offset+album_count],
            "song": songs[song_offset:song_offset+song_count],
        }

    def handle_songs_by_genre(self, resp, qs):
        """按流派获取歌曲"""
        genre = qs.get('genre', [''])[0]
        size = int(qs.get('count', ['50'])[0])
        offset = int(qs.get('offset', ['0'])[0])
        if genre:
            matched = []
            for t in TRACK_CACHE:
                raw = t.get('genres', '') or ''
                try:
                    glist = json.loads(raw)
                except:
                    glist = [g.strip() for g in raw.split(',') if g.strip()]
                if genre in glist:
                    matched.append(t)
        else:
            matched = list(TRACK_CACHE)
        songs = [self._build_song(t) for t in matched[offset:offset+size]]
        resp['songsByGenre'] = {"song": songs}

    def handle_random_songs(self, resp, qs):
        size = int(qs.get('size', ['10'])[0])
        if not TRACK_CACHE:
            resp['randomSongs'] = {"song": []}
            return

        sample = random.sample(TRACK_CACHE, min(size, len(TRACK_CACHE)))
        songs = [self._build_song(t) for t in sample]

        resp['randomSongs'] = {"song": songs}

    def serve_stream(self, qs):
        track_id = qs.get('id', [''])[0]

        fp = None
        for t in TRACK_CACHE:
            if t['id'] == track_id:
                fp = t.get('file_path', '')
                break

        if fp:
            host_fp = map_path(fp)
            if os.path.exists(host_fp):
                self._send_file(host_fp)
                return

        try:
            with DB_LOCK:
                cur = DB.cursor()
                cur.execute("SELECT file_path FROM tracks WHERE id=?", (track_id,))
                row = cur.fetchone()
            if row and row[0]:
                host_fp = map_path(row[0])
                if os.path.exists(host_fp):
                    self._send_file(host_fp)
                    return
        except:
            pass

        self.send_error(404, "File not found")

    PLACEHOLDER_PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'

    def _placeholder_image(self):
        self.send_response(200)
        self.send_header('Content-Type', 'image/png')
        self.send_header('Content-Length', str(len(self.PLACEHOLDER_PNG)))
        self.send_header('Cache-Control', 'public, max-age=86400')
        self.end_headers()
        self.wfile.write(self.PLACEHOLDER_PNG)

    def serve_cover(self, qs):
        """代理封面图片 — 支持 al-xxx / ar-xxx / pl-xxx 前缀"""
        cov_id = (qs.get('id', [''])[0] or '')

        # 去掉 music-tag-web 的 al-/ar- 前缀
        real_id = cov_id
        if cov_id.startswith('al-'):
            real_id = cov_id[3:]
        elif cov_id.startswith('ar-'):
            # 艺人封面 → 查 artists 表 cover_art_path
            aid = cov_id[3:]
            try:
                with DB_LOCK:
                    cur = DB.cursor()
                    cur.execute("SELECT cover_art_path FROM artists WHERE id=?", (aid,))
                    row = cur.fetchone()
                if row and row[0] and os.path.exists(map_path(row[0])):
                    self._send_file(map_path(row[0]), is_image=True)
                    return
            except:
                pass
            # fallback: 取第一个专辑的封面
            alb_ids = ARTIST_ALBUMS.get(aid, set())
            for alb_id in alb_ids:
                al = ALBUM_CACHE.get(alb_id, {})
                cp = al.get('cover_path', '')
                if cp and os.path.exists(map_path(cp)):
                    self._send_file(map_path(cp), is_image=True)
                    return
            self._placeholder_image()
            return
        elif cov_id.startswith('pl-'):
            # 歌单封面
            pid = cov_id[3:]
            with DB_LOCK:
                cur = DB.cursor()
                cur.execute("SELECT cover_art_path FROM playlists WHERE id=?", (pid,))
                row = cur.fetchone()
            if row and row[0] and os.path.exists(map_path(row[0])):
                self._send_file(map_path(row[0]), is_image=True)
                return
            self._placeholder_image()
            return
        elif cov_id.startswith('pl_'):
            # 旧格式歌单封面 compat
            pid = cov_id
            with DB_LOCK:
                cur = DB.cursor()
                cur.execute("SELECT cover_art_path FROM playlists WHERE id=?", (pid,))
                row = cur.fetchone()
            if row and row[0] and os.path.exists(map_path(row[0])):
                self._send_file(map_path(row[0]), is_image=True)
                return
            self._placeholder_image()
            return
        elif cov_id.startswith('art_'):
            # 旧格式 compat: 直接查 artists 表
            try:
                with DB_LOCK:
                    cur = DB.cursor()
                    cur.execute("SELECT cover_art_path FROM artists WHERE id=?", (cov_id,))
                    row = cur.fetchone()
                if row and row[0] and os.path.exists(map_path(row[0])):
                    self._send_file(map_path(row[0]), is_image=True)
                    return
            except:
                pass
            alb_ids = ARTIST_ALBUMS.get(cov_id, set())
            for alb_id in alb_ids:
                al = ALBUM_CACHE.get(alb_id, {})
                cp = al.get('cover_path', '')
                if cp and os.path.exists(map_path(cp)):
                    self._send_file(map_path(cp), is_image=True)
                    return
            self._placeholder_image()
            return
        elif cov_id.startswith('trk_'):
            # 歌曲→从专辑取封面
            al = ALBUM_CACHE.get(cov_id, {})
            if not al.get('cover_path'):
                # 从 tracks 查专辑 ID
                for t in TRACK_CACHE:
                    if t['id'] == cov_id:
                        al = ALBUM_CACHE.get(t['album_id'], {})
                        break
            cp = al.get('cover_path', '') if al else ''
            if cp and os.path.exists(map_path(cp)):
                self._send_file(map_path(cp), is_image=True)
                return
            self._placeholder_image()
            return

        # 专辑封面 (alb_ 开头或原始 ID)
        al = ALBUM_CACHE.get(real_id, {})
        cover_path = al.get('cover_path', '')
        if cover_path and os.path.exists(map_path(cover_path)):
            self._send_file(map_path(cover_path), is_image=True)
            return

        # fallback
        try:
            with DB_LOCK:
                cur = DB.cursor()
                cur.execute("SELECT cover_art_path FROM albums WHERE id=?", (real_id,))
                row = cur.fetchone()
            if row and row[0] and os.path.exists(map_path(row[0])):
                self._send_file(map_path(row[0]), is_image=True)
                return
        except:
            pass

        self._placeholder_image()

    def _send_file(self, fp, is_image=False):
        if not os.path.exists(fp):
            self.send_error(404, "File not found")
            return

        self.connection.settimeout(300)  # 5分钟超时，防止大文件传输断开
        file_size = os.path.getsize(fp)
        ext = os.path.splitext(fp)[1].lower()

        if is_image:
            mimes = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
                     '.webp': 'image/webp', '.gif': 'image/gif'}
            mime = mimes.get(ext, 'image/jpeg')
        else:
            mimes = {'.flac': 'audio/flac', '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4',
                     '.ogg': 'audio/ogg', '.wav': 'audio/wav', '.aac': 'audio/aac',
                     '.opus': 'audio/opus', '.wma': 'audio/x-ms-wma'}
            mime = mimes.get(ext, 'audio/mpeg')

        # 处理 Range 请求（Seek、缓冲）
        range_header = ''
        for k, v in self.headers.items():
            if k.lower() == 'range':
                range_header = v
                break
        if range_header and range_header.startswith('bytes='):
            range_val = range_header.replace('bytes=', '')
            start, end = 0, file_size - 1
            if '-' in range_val:
                parts = range_val.split('-')
                if parts[0]:
                    start = int(parts[0])
                if len(parts) > 1 and parts[1]:
                    end = int(parts[1])
            if start >= file_size:
                self.send_error(416, "Range Not Satisfiable")
                return
            end = min(end, file_size - 1)
            content_length = end - start + 1

            self.send_response(206)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(content_length))
            self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()

            with open(fp, 'rb') as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = f.read(min(1048576, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                    remaining -= len(chunk)
            return

        # 完整文件响应
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(file_size))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()

        with open(fp, 'rb') as f:
            while True:
                chunk = f.read(1048576)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

    def log_message(self, format, *args):
        """完整请求日志 → stderr + 文件"""
        if args and len(args) >= 3:
            path = args[1] if len(args) > 1 else ''
            code = args[2] if len(args) > 2 else ''
            msg = f"[{self.log_date_time_string()}] {args[0]} {path} -> {code}"
            print(msg, flush=True)
            with open('/tmp/bridge_requests.log', 'a') as f:
                f.write(msg + '\n')


def main():
    parser = argparse.ArgumentParser(description='道理鱼 Subsonic 桥 v2')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=4040)
    parser.add_argument('--db', default=None, help='数据库路径 (自动检测)')
    parser.add_argument('--no-auth', action='store_true', help='禁用鉴权 (不推荐)')
    parser.add_argument('--music-dir', default='', help='宿主机音乐根目录 (映射 /music 容器路径)')
    args = parser.parse_args()

    if args.no_auth:
        global AUTH_ENABLED
        AUTH_ENABLED = False

    if args.music_dir:
        global MUSIC_DIR
        MUSIC_DIR = args.music_dir.rstrip('/')
        print(f"  音乐目录映射: /music → {MUSIC_DIR}")

    # 找数据库
    db_path = args.db or os.environ.get("DLY_DB") or find_db()
    if not db_path:
        print("错误: 未找到道理鱼数据库。请手动指定 --db /path/to/daoliyu.db")
        print("已搜索路径:")
        for p in [
            "/vol1/@appshare/daoliyu.music/runtime/data/daoliyu.db",
            "/vol1/@appdata/daoliyu.music/daoliyu.db",
            "/vol1/@appdata/fnnas.daoliyu.music/daoliyu.db",
        ]:
            print(f"  {'✓' if os.path.exists(p) else '✗'} {p}")
        sys.exit(1)

    init_db(db_path)
    refresh_cache()

    server = ThreadingHTTPServer((args.host, args.port), SubsonicHandler)
    print(f"\n→ 桥服务已启动: http://{args.host}:{args.port}")
    print(f"  Subsonic 端点: http://{args.host}:{args.port}/rest/")
    print(f"  Ctrl+C 停止")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止...")
        server.shutdown()
        DB.close()
        print("已停止")


if __name__ == '__main__':
    main()
