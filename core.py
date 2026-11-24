# -*- coding: utf-8 -*-
"""版本助手 · 文件版本引擎

设计要点:
1. 内容寻址: 每个版本对应一个 SHA-256, 相同内容只落一份盘, 10 个版本不等于 10 份空间。
2. 快照按文件记录: 每个文件有自己的时间线, 而不是整个文件夹打包。
3. 自动 + 手动: 后台静默记录保存动作, 也可以随时手工"存一版"并写备注。
4. 回退安全: 恢复前先把当前内容自动存一版, 不会一把梭。
"""
import os
import re
import json
import html
import time
import zlib
import sqlite3
import hashlib
import zipfile
import difflib
import datetime
import threading

IGNORE_DIRS = {".git", ".svn", "node_modules", "__pycache__", "$recycle.bin",
               "system volume information", ".idea", ".vscode", ".workbuddy"}
IGNORE_EXT = {".tmp", ".temp", ".crdownload", ".part", ".partial", ".swp", ".swo",
              ".lock", ".laccdb", ".db-journal", ".db-wal", ".db-shm"}
IGNORE_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
COMPRESS_MIN = 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    ext         TEXT,
    created_at  TEXT NOT NULL,
    last_seen   TEXT
);

CREATE TABLE IF NOT EXISTS versions (
    id          INTEGER PRIMARY KEY,
    item_id     INTEGER NOT NULL REFERENCES items(id),
    created_at  TEXT NOT NULL,
    origin      TEXT NOT NULL,
    note        TEXT,
    size        INTEGER NOT NULL,
    hash        TEXT,
    exists_flag INTEGER NOT NULL DEFAULT 1,
    pinned      INTEGER NOT NULL DEFAULT 0,
    author      TEXT,
    mtime_ns    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_ver_item ON versions(item_id, id DESC);

CREATE TABLE IF NOT EXISTS blobs (
    hash        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    stored_size INTEGER NOT NULL,
    codec       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_state (
    item_id   INTEGER PRIMARY KEY REFERENCES items(id),
    mtime_ns  INTEGER NOT NULL,
    size      INTEGER NOT NULL,
    hash      TEXT,
    missing   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    origin      TEXT NOT NULL,
    note        TEXT,
    created     INTEGER NOT NULL DEFAULT 0,
    changed     INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "auto_enabled": "1",
    "interval": "20",
    "roots": "[]",
    "max_mb": "300",
    "auto_keep": "30",
}


def default_author():
    import socket
    u = os.environ.get("USERNAME") or os.environ.get("USER") or "未知用户"
    try:
        hh = socket.gethostname()
    except OSError:
        hh = ""
    return f"{u}@{hh}" if hh else u


def is_network_path(path):
    """判断是否落在网络共享上（UNC 路径或映射的网络驱动器）。"""
    try:
        p = os.path.abspath(path)
    except (OSError, ValueError):
        return False
    if p.startswith("\\\\") or p.startswith("//"):
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes
        root = os.path.splitdrive(p)[0] + "\\"
        # DRIVE_REMOTE = 4
        return ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) == 4
    except Exception:  # noqa: BLE001
        return False


def _seconds_since(iso):
    try:
        t = datetime.datetime.strptime(iso, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return 1e9
    return (datetime.datetime.now() - t).total_seconds()


IDENTITY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "identity.json")


def load_author():
    """作者名属于「这台机器」，不能存在共享库里，否则会互相覆盖。"""
    try:
        with open(IDENTITY_FILE, encoding="utf-8") as f:
            a = (json.load(f).get("author") or "").strip()
        return a or default_author()
    except (OSError, ValueError):
        return default_author()


def save_author(name):
    name = (name or "").strip()
    with open(IDENTITY_FILE, "w", encoding="utf-8") as f:
        json.dump({"author": name or default_author()}, f, ensure_ascii=False, indent=2)
    return name or default_author()


def now_iso():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def sha256_file(path, limit=None):
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            h.update(chunk)
            if limit and size > limit:
                break
    return h.hexdigest(), size


def _should_skip_dir(name):
    return name.lower() in IGNORE_DIRS or name.startswith(".")


def _should_skip_file(name):
    low = name.lower()
    if low in IGNORE_NAMES:
        return True
    if name.startswith("~$"):
        return True
    return os.path.splitext(low)[1] in IGNORE_EXT


class Repo:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.obj_dir = os.path.join(self.root, "objects")
        self.db_path = os.path.join(self.root, "index.db")
        self.lock = threading.RLock()
        self.network = is_network_path(self.root)
        self.author = load_author()
        os.makedirs(self.obj_dir, exist_ok=True)
        # timeout 让 SQLite 在锁竞争时自己等，而不是立刻抛 database is locked
        # isolation_level=None: 改成显式事务，才能用 BEGIN IMMEDIATE 串行化多机写入
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=20,
                                    isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA busy_timeout = 20000")
            if self.network:
                # WAL 依赖共享内存，SMB 上不可靠，网络库必须回退到回滚日志
                self.conn.execute("PRAGMA journal_mode = DELETE")
                self.conn.execute("PRAGMA synchronous = FULL")
            else:
                self.conn.execute("PRAGMA journal_mode = WAL")
                self.conn.execute("PRAGMA synchronous = NORMAL")
            self.conn.executescript(SCHEMA)
            self._migrate()
            for k, v in DEFAULT_SETTINGS.items():
                self.conn.execute("INSERT OR IGNORE INTO settings(k, v) VALUES(?, ?)", (k, v))
            self.conn.commit()
        meta = os.path.join(self.root, "repository.json")
        if not os.path.exists(meta):
            with open(meta, "w", encoding="utf-8") as f:
                json.dump({"app": "版本助手", "version": 2, "created_at": now_iso(),
                           "network": self.network},
                          f, ensure_ascii=False, indent=2)

    def _migrate(self):
        """老版本库升级：versions 表补 author 列。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(versions)")}
        if "author" not in cols:
            self.conn.execute("ALTER TABLE versions ADD COLUMN author TEXT")
        self.conn.commit()

    def close(self):
        with self.lock:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------- settings ----------
    def get_settings(self):
        with self.lock:
            rows = self.conn.execute("SELECT k, v FROM settings").fetchall()
        d = {r["k"]: r["v"] for r in rows}
        return {
            "auto_enabled": d.get("auto_enabled", "1") == "1",
            "interval": int(d.get("interval", "20")),
            "roots": json.loads(d.get("roots", "[]")),
            "max_mb": int(d.get("max_mb", "300")),
            "auto_keep": int(d.get("auto_keep", "30")),
            "author": self.author,
        }

    def set_settings(self, patch):
        cur = self.get_settings()
        cur.update({k: v for k, v in patch.items() if k in cur})
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('auto_enabled', ?)",
                              ("1" if cur["auto_enabled"] else "0",))
            self.conn.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('interval', ?)",
                              (str(int(cur["interval"])),))
            self.conn.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('roots', ?)",
                              (json.dumps(cur["roots"], ensure_ascii=False),))
            self.conn.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('max_mb', ?)",
                              (str(int(cur["max_mb"])),))
            self.conn.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('auto_keep', ?)",
                              (str(int(cur["auto_keep"])),))
            self.conn.commit()
        return cur

    def add_root(self, path):
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            raise ValueError("目录不存在: " + path)
        cur = self.get_settings()
        if path in cur["roots"]:
            return cur
        cur["roots"].append(path)
        return self.set_settings({"roots": cur["roots"]})

    def remove_root(self, path):
        cur = self.get_settings()
        cur["roots"] = [r for r in cur["roots"] if r != path]
        return self.set_settings({"roots": cur["roots"]})

    # ---------- objects ----------
    def _obj_path(self, h, codec):
        d = os.path.join(self.obj_dir, h[:2])
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, h[2:] + (".z" if codec == "zlib" else ""))

    def has_object(self, h):
        with self.lock:
            return self.conn.execute("SELECT 1 FROM blobs WHERE hash = ?", (h,)).fetchone() is not None

    def put_object(self, src_path, h, size):
        with self.lock:
            row = self.conn.execute("SELECT hash FROM blobs WHERE hash = ?", (h,)).fetchone()
        if row:
            return
        codec, payload = "raw", None
        if size >= COMPRESS_MIN:
            try:
                with open(src_path, "rb") as f:
                    raw = f.read()
                packed = zlib.compress(raw, 6)
                if len(packed) < len(raw) * 0.92:
                    codec, payload = "zlib", packed
            except OSError:
                payload = None
        dst = self._obj_path(h, codec)
        if not os.path.exists(dst):
            # 先写唯一临时名再原子改名：多台机器同时写同一个块不会互相写坏
            tmp = f"{dst}.tmp{os.getpid()}_{threading.get_ident()}"
            try:
                if payload is not None:
                    with open(tmp, "wb") as f:
                        f.write(payload)
                else:
                    import shutil
                    shutil.copyfile(src_path, tmp)
                if os.path.exists(dst):
                    os.remove(tmp)      # 别人先写好了，用他的
                else:
                    os.replace(tmp, dst)
            except OSError:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                raise
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO blobs(hash, size, stored_size, codec, created_at) VALUES(?,?,?,?,?)",
                (h, size, os.path.getsize(dst), codec, now_iso()))
            self.conn.commit()

    def read_object(self, h):
        with self.lock:
            row = self.conn.execute("SELECT codec FROM blobs WHERE hash = ?", (h,)).fetchone()
        if not row:
            raise FileNotFoundError("内容块缺失: " + h[:12])
        path = self._obj_path(h, row["codec"])
        with open(path, "rb") as f:
            data = f.read()
        return zlib.decompress(data) if row["codec"] == "zlib" else data

    # ---------- items ----------
    def _item_id(self, path, ext):
        with self.lock:
            row = self.conn.execute("SELECT id FROM items WHERE path = ?", (path,)).fetchone()
            if row:
                self.conn.execute("UPDATE items SET last_seen = ? WHERE id = ?", (now_iso(), row["id"]))
                return row["id"]
            # 多机同时首次见到同一个文件时会撞 UNIQUE，用 IGNORE + 回查
            self.conn.execute(
                "INSERT OR IGNORE INTO items(path, ext, created_at, last_seen) VALUES(?,?,?,?)",
                (path, ext, now_iso(), now_iso()))
            row = self.conn.execute("SELECT id FROM items WHERE path = ?", (path,)).fetchone()
            return row["id"]

    def _state(self, item_id):
        with self.lock:
            return self.conn.execute("SELECT * FROM file_state WHERE item_id = ?", (item_id,)).fetchone()

    def _set_state(self, item_id, mtime_ns, size, h, missing=0):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO file_state(item_id, mtime_ns, size, hash, missing) VALUES(?,?,?,?,?)",
                (item_id, mtime_ns, size, h, missing))

    def _add_version(self, item_id, path, h, size, origin, note, exists=1, mtime_ns=0, author=None):
        if exists and h:
            self.put_object(path, h, size)
        with self.lock:
            try:
                # 多机共用时用它串行化：检查"是否已有同样内容的最新版本"和写入必须是一步
                self.conn.execute("BEGIN IMMEDIATE")
                last = self.conn.execute(
                    "SELECT hash, note, created_at FROM versions WHERE item_id = ?"
                    " ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
                same = (last and last["hash"] == h and exists
                        and _seconds_since(last["created_at"]) < 120
                        and (origin == "auto" or (last["note"] or "") == (note or "")))
                if same:
                    self.conn.execute("ROLLBACK")
                    return None
                cur = self.conn.execute(
                    "INSERT INTO versions(item_id, created_at, origin, note, size, hash,"
                    " exists_flag, author, mtime_ns) VALUES(?,?,?,?,?,?,?,?,?)",
                    (item_id, now_iso(), origin, note, size, h, 1 if exists else 0,
                     author or self.author, mtime_ns))
                vid = cur.lastrowid
                self.conn.execute("COMMIT")
            except Exception:
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        if origin == "auto":
            self._prune(item_id)
        return vid

    def _prune(self, item_id):
        keep = self.get_settings()["auto_keep"]
        with self.lock:
            rows = self.conn.execute(
                "SELECT id FROM versions WHERE item_id = ? AND origin = 'auto' AND pinned = 0"
                " ORDER BY id DESC", (item_id,)).fetchall()
            drop = [r["id"] for r in rows[keep:]]
            for vid in drop:
                self.conn.execute("DELETE FROM versions WHERE id = ?", (vid,))
            if drop:
                self.conn.commit()

    # ---------- scan ----------
    def scan(self, origin="auto", note=None, settle_seconds=2):
        started = time.time()
        st = self.get_settings()
        max_bytes = st["max_mb"] * 1024 * 1024
        roots = st["roots"]
        author = st["author"]
        created = changed = skipped = 0
        if not roots:
            return {"ok": False, "error": "还没有添加要监控的文件夹"}

        seen = set()
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
                for name in filenames:
                    if _should_skip_file(name):
                        skipped += 1
                        continue
                    full = os.path.join(dirpath, name)
                    try:
                        s = os.stat(full)
                    except OSError:
                        continue
                    seen.add(full)
                    if origin == "auto" and time.time() - s.st_mtime < settle_seconds:
                        continue  # 文件还在写入, 下一轮再看
                    if s.st_size > max_bytes:
                        skipped += 1
                        continue
                    item_id = self._item_id(full, os.path.splitext(name)[1].lower())
                    prev = self._state(item_id)
                    if (prev and not prev["missing"] and prev["hash"]
                            and prev["mtime_ns"] == s.st_mtime_ns and prev["size"] == s.st_size):
                        continue
                    try:
                        h, size = sha256_file(full)
                    except OSError:
                        continue
                    if prev and prev["hash"] == h and not prev["missing"]:
                        self._set_state(item_id, s.st_mtime_ns, size, h)
                        with self.lock:
                            self.conn.commit()
                        continue
                    vid = self._add_version(item_id, full, h, size, origin, note, 1,
                                            s.st_mtime_ns, author)
                    self._set_state(item_id, s.st_mtime_ns, size, h)
                    with self.lock:
                        self.conn.commit()
                    if vid is not None:
                        if prev and not prev["missing"]:
                            changed += 1
                        else:
                            created += 1
                # 空目录兜底

        # 消失检测
        with self.lock:
            states = self.conn.execute(
                "SELECT fs.*, i.path FROM file_state fs JOIN items i ON i.id = fs.item_id"
                " WHERE fs.missing = 0").fetchall()
        for row in states:
            p = row["path"]
            if p in seen:
                continue
            if not any(p.startswith(r) for r in roots):
                continue
            if not os.path.exists(p):
                self._add_version(row["item_id"], p, row["hash"], row["size"],
                                  origin, note, 0, row["mtime_ns"], author)
                with self.lock:
                    self.conn.execute("UPDATE file_state SET missing = 1 WHERE item_id = ?",
                                      (row["item_id"],))
                    self.conn.commit()

        dur = int((time.time() - started) * 1000)
        with self.lock:
            self.conn.execute(
                "INSERT INTO scans(started_at, origin, note, created, changed, skipped, duration_ms)"
                " VALUES(?,?,?,?,?,?,?)",
                (now_iso(), origin, note, created, changed, skipped, dur))
            self.conn.commit()
        # 清理无引用内容块
        self.gc()
        return {"ok": True, "created": created, "changed": changed,
                "skipped": skipped, "duration_ms": dur}

    def gc(self):
        # 留 10 分钟宽限：别的机器可能刚写好内容块、还没登记到 versions
        cutoff = (datetime.datetime.now()
                  - datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            self.conn.execute(
                "DELETE FROM blobs WHERE hash NOT IN"
                " (SELECT hash FROM versions WHERE hash IS NOT NULL) AND created_at < ?",
                (cutoff,))
            self.conn.commit()

    # ---------- query ----------
    def files(self, q=None):
        with self.lock:
            sql = ("SELECT i.id, i.path, i.ext,"
                   " (SELECT COUNT(*) FROM versions v WHERE v.item_id = i.id) AS n,"
                   " (SELECT v.created_at FROM versions v WHERE v.item_id = i.id ORDER BY v.id DESC LIMIT 1) AS last_at,"
                   " (SELECT v.origin FROM versions v WHERE v.item_id = i.id ORDER BY v.id DESC LIMIT 1) AS last_origin,"
                   " (SELECT v.size FROM versions v WHERE v.item_id = i.id ORDER BY v.id DESC LIMIT 1) AS last_size,"
                   " COALESCE(fs.missing, 0) AS missing"
                   " FROM items i LEFT JOIN file_state fs ON fs.item_id = i.id"
                   " WHERE EXISTS (SELECT 1 FROM versions v WHERE v.item_id = i.id)")
            args = []
            if q:
                sql += " AND i.path LIKE ?"
                args.append(f"%{q}%")
            sql += " ORDER BY last_at DESC"
            rows = self.conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["last_size_h"] = human_size(d["last_size"])
            out.append(d)
        return out

    def versions(self, item_id):
        with self.lock:
            rows = self.conn.execute(
                "SELECT v.*, i.path, i.ext FROM versions v JOIN items i ON i.id = v.item_id"
                " WHERE v.item_id = ? ORDER BY v.id DESC", (item_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["size_h"] = human_size(d["size"])
            d["exists"] = bool(d["exists_flag"])
            d["pinned"] = bool(d["pinned"])
            out.append(d)
        return out

    def stats(self):
        with self.lock:
            n_items = self.conn.execute(
                "SELECT COUNT(DISTINCT item_id) FROM versions").fetchone()[0]
            n_ver = self.conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
            logical = self.conn.execute(
                "SELECT COALESCE(SUM(size),0) FROM versions WHERE exists_flag = 1").fetchone()[0]
            stored = self.conn.execute(
                "SELECT COALESCE(SUM(stored_size),0) FROM blobs").fetchone()[0]
            dup = self.conn.execute(
                "SELECT COALESCE(SUM(size),0) FROM versions WHERE exists_flag = 1"
                " AND hash IS NOT NULL").fetchone()[0]
            uniq = self.conn.execute(
                "SELECT COALESCE(SUM(size),0) FROM blobs").fetchone()[0]
        return {"files": n_items, "versions": n_ver,
                "logical_h": human_size(logical), "stored_h": human_size(stored),
                "saved_h": human_size(max(0, dup - uniq)),
                "saved_pct": (round((dup - uniq) * 100.0 / dup) if dup else 0)}

    def activity(self, limit=12):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 全局时间线 ----------
    def _project_of(self, path, roots):
        """文件属于哪个监控目录（最长前缀匹配）。"""
        low = path.lower()
        best = None
        for r in roots:
            base = r.rstrip("\\/")
            if low == base.lower() or low.startswith(base.lower() + os.sep) or low.startswith(base.lower() + "/"):
                if best is None or len(base) > len(best):
                    best = base
        if not best:
            return "", "未分组"
        return best, (os.path.basename(best) or best)

    def timeline(self, limit=400, project=None, q=None, origin=None, days=None):
        """跨所有文件的版本流水，按时间倒序。"""
        st = self.get_settings()
        roots = st["roots"]
        sql = ("SELECT v.id, v.item_id, v.created_at, v.origin, v.note, v.size, v.hash,"
               " v.exists_flag, v.pinned, v.author, i.path, i.ext,"
               " (SELECT v2.id FROM versions v2 WHERE v2.item_id = v.item_id AND v2.id < v.id"
               "   ORDER BY v2.id DESC LIMIT 1) AS prev_id,"
               " (SELECT v2.size FROM versions v2 WHERE v2.item_id = v.item_id AND v2.id < v.id"
               "   ORDER BY v2.id DESC LIMIT 1) AS prev_size,"
               " (SELECT v2.exists_flag FROM versions v2 WHERE v2.item_id = v.item_id AND v2.id < v.id"
               "   ORDER BY v2.id DESC LIMIT 1) AS prev_exists"
               " FROM versions v JOIN items i ON i.id = v.item_id WHERE 1 = 1")
        args = []
        if origin in ("auto", "manual"):
            sql += " AND v.origin = ?"
            args.append(origin)
        if q:
            sql += " AND i.path LIKE ?"
            args.append(f"%{q}%")
        if days:
            cutoff = (datetime.datetime.now()
                      - datetime.timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
            sql += " AND v.created_at >= ?"
            args.append(cutoff)
        if project:
            base = project.rstrip("\\/")
            sql += " AND (i.path LIKE ? OR i.path LIKE ?)"
            args += [base + "\\%", base + "/%"]
        sql += " ORDER BY v.created_at DESC, v.id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 2000)))
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()

        out = []
        for r in rows:
            d = dict(r)
            proj_path, proj_name = self._project_of(d["path"], roots)
            prev_id = d["prev_id"]
            prev_size = d["prev_size"]
            if not d["exists_flag"]:
                d["delta"], d["delta_type"] = 0, "deleted"
            elif prev_id is None:
                d["delta"], d["delta_type"] = d["size"], "first"
            else:
                d["delta"] = d["size"] - (prev_size or 0)
                d["delta_type"] = "change"
            d["delta_h"] = ("+" if d["delta"] > 0 else "") + human_size(abs(d["delta"]))
            d["size_h"] = human_size(d["size"])
            d["name"] = os.path.basename(d["path"])
            d["project"] = proj_path
            d["project_name"] = proj_name
            out.append(d)
        return {"entries": out, "count": len(out), "full": len(out) >= int(limit)}

    def projects(self):
        st = self.get_settings()
        out = []
        with self.lock:
            for r in st["roots"]:
                base = r.rstrip("\\/")
                like = base + "\\%"
                n_items = self.conn.execute(
                    "SELECT COUNT(*) FROM items WHERE path LIKE ?", (like,)).fetchone()[0]
                n_ver = self.conn.execute(
                    "SELECT COUNT(*) FROM versions v JOIN items i ON i.id = v.item_id"
                    " WHERE i.path LIKE ?", (like,)).fetchone()[0]
                last = self.conn.execute(
                    "SELECT MAX(v.created_at) FROM versions v JOIN items i ON i.id = v.item_id"
                    " WHERE i.path LIKE ?", (like,)).fetchone()[0]
                out.append({"path": r, "name": os.path.basename(base) or base,
                            "files": n_items, "versions": n_ver, "last_at": last,
                            "ok": os.path.isdir(r)})
        return out

    def peers(self, days=7, limit=20):
        """共用这个库的机器/人（按作者统计）。"""
        cutoff = (datetime.datetime.now()
                  - datetime.timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            rows = self.conn.execute(
                "SELECT COALESCE(NULLIF(author, ''), '（旧版本记录）') AS a,"
                " COUNT(*) AS n, MAX(created_at) AS last_at"
                " FROM versions WHERE created_at >= ? GROUP BY a ORDER BY last_at DESC LIMIT ?",
                (cutoff, limit)).fetchall()
        return [{"author": r["a"], "versions": r["n"], "last_at": r["last_at"]} for r in rows]

    # ---------- restore ----------
    def restore(self, version_id, safety=True):
        with self.lock:
            v = self.conn.execute(
                "SELECT v.*, i.path FROM versions v JOIN items i ON i.id = v.item_id"
                " WHERE v.id = ?", (version_id,)).fetchone()
        if not v:
            raise ValueError("版本不存在")
        path = v["path"]
        if safety and os.path.exists(path):
            try:
                h, size = sha256_file(path)
                if h != v["hash"]:
                    stt = os.stat(path)
                    iid = self._item_id(path, os.path.splitext(path)[1].lower())
                    self._add_version(iid, path, h, size, "manual",
                                      "恢复「" + v["created_at"] + "」版本前的自动备份", 1, stt.st_mtime_ns)
                    self._set_state(iid, stt.st_mtime_ns, size, h)
                    with self.lock:
                        self.conn.commit()
            except OSError:
                pass
        if not v["exists_flag"]:
            if os.path.exists(path):
                os.remove(path)
            with self.lock:
                self.conn.execute("UPDATE file_state SET missing = 1 WHERE item_id = ?", (v["item_id"],))
                self.conn.commit()
            return {"ok": True, "deleted": True}
        data = self.read_object(v["hash"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".restoring"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        stt = os.stat(path)
        self._set_state(v["item_id"], stt.st_mtime_ns, len(data), v["hash"], 0)
        with self.lock:
            self.conn.commit()
        return {"ok": True, "path": path}

    def set_pin(self, version_id, pinned):
        with self.lock:
            self.conn.execute("UPDATE versions SET pinned = ? WHERE id = ?",
                              (1 if pinned else 0, version_id))
            self.conn.commit()
        return {"ok": True}

    def delete_version(self, version_id):
        with self.lock:
            self.conn.execute("DELETE FROM versions WHERE id = ?", (version_id,))
            self.conn.commit()
        self.gc()
        return {"ok": True}


# ---------------- 差异对比 ----------------
def _docx_text(data):
    import io as _io
    with zipfile.ZipFile(_io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(xml)


def _xlsx_text(data):
    import io as _io
    out = []
    with zipfile.ZipFile(_io.BytesIO(data)) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            sx = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
            for si in re.findall(r"<si>(.*?)</si>", sx, re.S):
                shared.append(html.unescape(re.sub(r"<[^>]+>", "", si)))
        sheets = []
        if "xl/workbook.xml" in names:
            wb = z.read("xl/workbook.xml").decode("utf-8", "ignore")
            sheets = re.findall(r'<sheet[^>]*name="([^"]+)"', wb)
        for idx in range(1, len(sheets) + 1):
            p = f"xl/worksheets/sheet{idx}.xml"
            if p not in names:
                continue
            sx = z.read(p).decode("utf-8", "ignore")
            sname = sheets[idx - 1]
            for row in re.findall(r"<row[^>]*>(.*?)</row>", sx, re.S):
                vals = []
                for m in re.finditer(r"<c\b([^>]*)>(.*?)</c>", row, re.S):
                    attrs, body = m.group(1), m.group(2)
                    t = re.search(r'\bt="([^"]+)"', attrs)
                    t = t.group(1) if t else ""
                    v = re.search(r"<v>(.*?)</v>", body, re.S)
                    if t == "s" and v:
                        try:
                            vals.append(shared[int(v.group(1))])
                        except (ValueError, IndexError):
                            vals.append("")
                    elif t == "inlineStr":
                        vals.append(html.unescape(re.sub(r"<[^>]+>", "", body)))
                    else:
                        vals.append(html.unescape(v.group(1)) if v else "")
                if any(x.strip() for x in vals):
                    out.append(f"[{sname}] " + " | ".join(vals))
    return "\n".join(out)


TEXT_EXT = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".log", ".ini",
            ".config", ".yml", ".yaml", ".py", ".js", ".ts", ".css", ".sql", ".bat", ".ps1"}


def _hunk_label(raw):
    m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", raw)
    if not m:
        return raw
    a, an, b, bn = int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1)
    left = f"第 {a} 行" if an <= 1 else f"第 {a}–{a + an - 1} 行"
    right = f"第 {b} 行" if bn <= 1 else f"第 {b}–{b + bn - 1} 行"
    return f"{left}（旧） → {right}（新）"


def diff_versions(repo, va_id, vb_id):
    with repo.lock:
        va = repo.conn.execute("SELECT v.*, i.path, i.ext FROM versions v JOIN items i ON i.id=v.item_id"
                               " WHERE v.id=?", (va_id,)).fetchone()
        vb = repo.conn.execute("SELECT v.*, i.path, i.ext FROM versions v JOIN items i ON i.id=v.item_id"
                               " WHERE v.id=?", (vb_id,)).fetchone()
    if not va or not vb:
        raise ValueError("版本不存在")
    if va["item_id"] != vb["item_id"]:
        return {"kind": "none", "message": "两个版本属于不同文件，不能对比。"}
    if not va["exists_flag"] or not vb["exists_flag"]:
        return {"kind": "none", "message": "该版本对应文件已不存在，只能查看记录。"}
    ext = (va["ext"] or "").lower()
    a = repo.read_object(va["hash"])
    b = repo.read_object(vb["hash"])
    kind = "text"
    try:
        if ext == ".docx":
            ta, tb, kind = _docx_text(a), _docx_text(b), "docx"
        elif ext in (".xlsx", ".xlsm"):
            ta, tb, kind = _xlsx_text(a), _xlsx_text(b), "xlsx"
        elif ext in TEXT_EXT:
            ta = a.decode("utf-8", "replace")
            tb = b.decode("utf-8", "replace")
        else:
            return {"kind": "binary",
                    "message": "该格式暂不支持内容级对比，可看大小与哈希。",
                    "a_size_h": human_size(va["size"]), "b_size_h": human_size(vb["size"]),
                    "a_hash": (va["hash"] or "")[:12], "b_hash": (vb["hash"] or "")[:12]}
    except Exception as e:  # noqa: BLE001
        return {"kind": "binary", "message": "解析失败：" + str(e)}
    la = [ln.rstrip() for ln in ta.splitlines()]
    lb = [ln.rstrip() for ln in tb.splitlines()]
    lines, added, removed = [], 0, 0
    for raw in difflib.unified_diff(la, lb, lineterm="", n=2):
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("@@"):
            lines.append({"t": "h", "s": _hunk_label(raw)})
            continue
        sign, body = (raw[0], raw[1:]) if raw[:1] in "+- " else (" ", raw)
        if sign == "+":
            added += 1
        elif sign == "-":
            removed += 1
        lines.append({"t": sign, "s": body})
        if len(lines) > 4000:
            lines.append({"t": "h", "s": "… 差异过多，已截断"})
            break
    return {"kind": kind, "lines": lines, "added": added, "removed": removed,
            "a_at": va["created_at"], "b_at": vb["created_at"],
            "a_size_h": human_size(va["size"]), "b_size_h": human_size(vb["size"])}
