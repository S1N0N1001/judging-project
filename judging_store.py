"""Transactional storage and authorization. All writes authorize inside a transaction."""
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path

from judging_core import ALGORITHMS, calculate, validate_config, validate_scores


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def ident():
    return uuid.uuid4().hex


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def data_root():
    return Path(os.environ.get("JUDGING_DATA_DIR", Path(os.environ.get("LOCALAPPDATA", Path.home())) / "JudgingApp")).resolve()


class Store:
    def __init__(self, root=None):
        self.root = Path(root) if root else data_root()
        for name in ("data", "backups", "logs", "exports"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.path = self.root / "data" / "judging.sqlite3"
        with self.transaction() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS event (id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'preparation', algorithm TEXT NOT NULL, criteria TEXT NOT NULL, config_version TEXT NOT NULL, current_id TEXT, threshold REAL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS judges (id TEXT PRIMARY KEY, number INTEGER UNIQUE NOT NULL, token_hash TEXT UNIQUE NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS participants (id TEXT PRIMARY KEY, number INTEGER UNIQUE NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, members TEXT NOT NULL, work_type TEXT NOT NULL DEFAULT '', stage TEXT NOT NULL DEFAULT '', materials TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0, opened INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS cards (judge_id TEXT REFERENCES judges(id), participant_id TEXT REFERENCES participants(id), scores TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, version INTEGER NOT NULL, updated_at TEXT NOT NULL, submitted_at TEXT, PRIMARY KEY(judge_id,participant_id));
            CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (scope TEXT PRIMARY KEY, count INTEGER NOT NULL, blocked_until REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, object_id TEXT NOT NULL, details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, at TEXT NOT NULL, payload TEXT NOT NULL);
            """)
            version = self.setting(db, "schema_version")
            if version not in (None, "1"):
                raise ValueError("Несовместимая версия базы")
            self.set_setting(db, "schema_version", "1")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def setting(db, key):
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def set_setting(db, key, value):
        db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    @staticmethod
    def log(db, actor, action, obj="", details=None):
        db.execute("INSERT INTO audit(at,actor,action,object_id,details) VALUES (?,?,?,?,?)", (now(), actor, action, obj, json.dumps(details or {}, ensure_ascii=False)))

    def initialized(self):
        with self.transaction() as db:
            return self.setting(db, "password") is not None

    def setup(self, password, provided, expected, criteria):
        if not expected or not hmac.compare_digest(provided, expected):
            raise PermissionError("Первичная настройка доступна через локальный лаунчер")
        if len(password) < 12:
            raise ValueError("Пароль должен содержать минимум 12 символов")
        validate_config(criteria)
        with self.transaction() as db:
            if self.setting(db, "password"):
                raise ValueError("Первичная настройка уже выполнена")
            salt = secrets.token_hex(16)
            hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()
            self.set_setting(db, "password", salt + ":" + hashed)
            eid = ident()
            db.execute("INSERT INTO event(id,name,algorithm,criteria,config_version,updated_at) VALUES (?,?,?,?,?,?)", (eid, "Новое мероприятие", "criterion_trim_v1", json.dumps(criteria, ensure_ascii=False), "1", now()))
            self.log(db, "local", "setup", eid)

    def login(self, password):
        # A global persistent limiter cannot be bypassed by changing IP/header.
        with self.transaction() as db:
            row = db.execute("SELECT * FROM attempts WHERE scope='admin'").fetchone()
            if row and row["blocked_until"] > time.time():
                raise PermissionError("Слишком много попыток. Повторите через минуту")
            saved = self.setting(db, "password")
            valid = False
            if saved:
                salt, expected = saved.split(":")
                valid = hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex(), expected)
            if not valid:
                count = (row["count"] if row and row["blocked_until"] > time.time() - 60 else 0) + 1
                db.execute("INSERT OR REPLACE INTO attempts VALUES ('admin',?,?)", (count, time.time() + 60 if count >= 5 else time.time()))
            else:
                db.execute("DELETE FROM attempts WHERE scope='admin'")
                token = secrets.token_urlsafe(32)
                db.execute("INSERT INTO sessions VALUES (?,?)", (digest(token), time.time() + 12 * 3600))
                self.log(db, "secretary", "login")
        if not valid:
            raise PermissionError("Неверный пароль")
        return token

    @staticmethod
    def admin(db, token):
        row = db.execute("SELECT expires FROM sessions WHERE token_hash=?", (digest(token or ""),)).fetchone()
        if not row or row[0] <= time.time():
            raise PermissionError("Требуется вход секретаря")

    def logout(self, token):
        with self.transaction() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))

    @staticmethod
    def judge(db, token):
        row = db.execute("SELECT id,number FROM judges WHERE token_hash=? AND active=1", (digest(token or ""),)).fetchone()
        if not row:
            raise PermissionError("Ссылка судьи недействительна или отозвана")
        return dict(row)

    @staticmethod
    def event(db):
        row = db.execute("SELECT * FROM event LIMIT 1").fetchone()
        if not row:
            raise ValueError("Пройдите первоначальную настройку")
        e = dict(row)
        e["criteria"] = json.loads(e["criteria"])
        return e

    @staticmethod
    def participants(db):
        rows = []
        for row in db.execute("SELECT * FROM participants ORDER BY number"):
            p = dict(row)
            p["members"] = json.loads(p["members"])
            rows.append(p)
        return rows

    @staticmethod
    def cards(db, jid=None):
        rows = []
        query, args = ("SELECT * FROM cards", ()) if jid is None else ("SELECT * FROM cards WHERE judge_id=?", (jid,))
        for row in db.execute(query, args):
            c = dict(row)
            c["scores"] = json.loads(c["scores"])
            rows.append(c)
        return rows

    def admin_view(self, token):
        with self.transaction() as db:
            self.admin(db, token)
            e = self.event(db)
            judges = [dict(r) for r in db.execute("SELECT id,number,active FROM judges WHERE active=1 ORDER BY number")]
            active_ids = {j["id"] for j in judges}
            cards = [c for c in self.cards(db) if c["judge_id"] in active_ids]
            if e["status"] in ("preparation", "scoring"):
                cards = [{k: c[k] for k in ("judge_id", "participant_id", "status", "version", "updated_at")} for c in cards]
            return {"event": e, "participants": self.participants(db), "judges": judges, "cards": cards}

    def judge_view(self, token):
        with self.transaction() as db:
            j = self.judge(db, token)
            return {"event": self.event(db), "judge": j, "participants": [p for p in self.participants(db) if p["opened"] and not p["archived"]], "cards": self.cards(db, j["id"])}

    def configure(self, token, name, algorithm, threshold, criteria):
        validate_config(criteria)
        if not name.strip() or algorithm not in ALGORITHMS:
            raise ValueError("Укажите название и алгоритм")
        if threshold is not None and not 0 <= threshold <= 1:
            raise ValueError("Порог должен находиться между 0 и 1")
        with self.transaction() as db:
            self.admin(db, token)
            e = self.event(db)
            if e["status"] != "preparation":
                raise ValueError("Методика закреплена после открытия")
            db.execute("UPDATE event SET name=?,algorithm=?,threshold=?,criteria=?,config_version=?,updated_at=?", (name.strip(), algorithm, threshold, json.dumps(criteria, ensure_ascii=False), str(int(e["config_version"]) + 1), now()))
            self.set_setting(db, "method_confirmed", "yes")
            self.log(db, "secretary", "configure", e["id"], {"algorithm": algorithm})

    def add_participant(self, token, title, description="", members=None, work_type="", stage="", materials=""):
        if not title.strip():
            raise ValueError("Название не может быть пустым")
        with self.transaction() as db:
            self.admin(db, token)
            if self.event(db)["status"] != "preparation":
                raise ValueError("Список работ закреплён после открытия")
            pid = ident()
            number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM participants").fetchone()[0]
            db.execute("INSERT INTO participants(id,number,title,description,members,work_type,stage,materials) VALUES (?,?,?,?,?,?,?,?)", (pid, number, title.strip(), description, json.dumps(members or [], ensure_ascii=False), work_type, stage, materials))
            self.log(db, "secretary", "add_participant", pid)
            return pid

    def archive_participant(self, token, pid, reason):
        if not reason.strip():
            raise ValueError("Укажите причину")
        self.backup_internal("before_archive")
        with self.transaction() as db:
            self.admin(db, token)
            if self.event(db)["status"] != "preparation":
                raise ValueError("Архивирование доступно только при подготовке")
            db.execute("UPDATE participants SET archived=1 WHERE id=?", (pid,))
            self.log(db, "secretary", "archive", pid, {"reason": reason})

    def edit_participant(self, token, pid, title, description, members, work_type, stage, materials):
        if not title.strip():
            raise ValueError("Название не может быть пустым")
        with self.transaction() as db:
            self.admin(db, token)
            if self.event(db)["status"] != "preparation":
                raise ValueError("Редактирование доступно только при подготовке")
            db.execute("UPDATE participants SET title=?,description=?,members=?,work_type=?,stage=?,materials=? WHERE id=? AND archived=0", (title.strip(), description, json.dumps(members, ensure_ascii=False), work_type, stage, materials, pid))
            self.log(db, "secretary", "edit_participant", pid)

    def add_judge(self, token):
        raw = secrets.token_urlsafe(32)
        with self.transaction() as db:
            self.admin(db, token)
            if self.event(db)["status"] != "preparation":
                raise ValueError("Состав судей закреплён после открытия")
            number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM judges").fetchone()[0]
            jid = ident()
            db.execute("INSERT INTO judges VALUES (?,?,?,1)", (jid, number, digest(raw)))
            self.log(db, "secretary", "add_judge", jid)
        return raw

    def rotate_judge(self, token, jid, reason):
        if not reason.strip():
            raise ValueError("Укажите причину перевыпуска")
        raw = secrets.token_urlsafe(32)
        with self.transaction() as db:
            self.admin(db, token)
            if db.execute("UPDATE judges SET token_hash=? WHERE id=? AND active=1", (digest(raw), jid)).rowcount != 1:
                raise ValueError("Судья не найден")
            self.log(db, "secretary", "rotate_judge", jid, {"reason": reason})
        return raw

    def remove_judge(self, token, jid, reason):
        if not reason.strip():
            raise ValueError("Укажите причину удаления")
        with self.transaction() as db:
            self.admin(db, token)
            if self.event(db)["status"] != "preparation":
                raise ValueError("Удалить судью можно только до открытия оценивания")
            if db.execute("UPDATE judges SET active=0,token_hash=? WHERE id=? AND active=1", (digest(secrets.token_urlsafe(32)), jid)).rowcount != 1:
                raise ValueError("Судья уже удалён или не найден")
            self.log(db, "secretary", "remove_judge", jid, {"reason": reason})

    def open_participant(self, token, pid):
        with self.transaction() as db:
            self.admin(db, token)
            if self.event(db)["status"] != "scoring":
                raise ValueError("Оценивание не открыто")
            if not db.execute("SELECT 1 FROM participants WHERE id=? AND archived=0", (pid,)).fetchone():
                raise ValueError("Работа недоступна")
            db.execute("UPDATE participants SET opened=1 WHERE id=?", (pid,))
            db.execute("UPDATE event SET current_id=?,updated_at=?", (pid, now()))
            self.log(db, "secretary", "current", pid)

    def save_card(self, token, pid, scores, comment, version, submit=False):
        with self.transaction() as db:
            j = self.judge(db, token)
            e = self.event(db)
            if e["status"] != "scoring":
                raise ValueError("Оценивание закрыто")
            if not db.execute("SELECT 1 FROM participants WHERE id=? AND opened=1 AND archived=0", (pid,)).fetchone():
                raise ValueError("Работа не открыта")
            if submit:
                validate_scores(scores, e["criteria"])
            else:
                if not set(scores) <= set(e["criteria"]):
                    raise ValueError("Неизвестный критерий")
                for key, value in scores.items():
                    if type(value) is not int or not 0 <= value <= e["criteria"][key]["max"]:
                        raise ValueError("Недопустимый балл")
            old = db.execute("SELECT * FROM cards WHERE judge_id=? AND participant_id=?", (j["id"], pid)).fetchone()
            if old and old["status"] == "submitted":
                if submit and old["version"] == version + 1 and json.loads(old["scores"]) == scores and old["comment"] == comment:
                    return old["version"]  # retry after a lost acknowledgement
                raise ValueError("Карточка отправлена; обратитесь к секретарю")
            current = old["version"] if old else 0
            if current != version:
                if old and current == version + 1 and json.loads(old["scores"]) == scores and old["comment"] == comment and not submit:
                    return current
                raise ValueError("Карточка изменена в другой вкладке. Обновите страницу")
            timestamp = now()
            db.execute("INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?,?)", (j["id"], pid, json.dumps(scores), comment, "submitted" if submit else "draft", current + 1, timestamp, timestamp if submit else None))
            self.log(db, j["id"], "submit" if submit else "save", pid, {"old": dict(old) if old else None, "scores": scores, "comment": comment, "version": current + 1})
            return current + 1

    def unlock(self, token, jid, pid, reason):
        if not reason.strip():
            raise ValueError("Укажите причину")
        with self.transaction() as db:
            self.admin(db, token)
            if self.event(db)["status"] != "scoring":
                raise ValueError("Сначала возобновите оценивание")
            db.execute("UPDATE cards SET status='draft',version=version+1 WHERE judge_id=? AND participant_id=?", (jid, pid))
            self.log(db, "secretary", "unlock", pid, {"judge": jid, "reason": reason})

    def transition(self, token, target, reason=""):
        self.backup_internal("before_" + target)
        with self.transaction() as db:
            self.admin(db, token)
            e = self.event(db)
            allowed = {"preparation": {"scoring"}, "scoring": {"closed"}, "closed": {"scoring", "approved"}, "approved": {"scoring"}}
            if target not in allowed[e["status"]]:
                raise ValueError("Недопустимый переход")
            if target == "scoring" and e["status"] != "preparation" and not reason.strip():
                raise ValueError("Укажите причину возобновления")
            if target == "scoring" and e["status"] == "preparation" and self.setting(db, "method_confirmed") != "yes":
                raise ValueError("Сначала сохраните и подтвердите методику")
            snap = self._snapshot(db)
            if not any(j["active"] for j in snap["judges"]) or not any(not p["archived"] for p in snap["participants"]):
                raise ValueError("Добавьте судей и работы")
            if target in ("closed", "approved") and not all(r["complete"] for r in calculate(snap)):
                raise ValueError("Не все судьи отправили все карточки")
            if target == "approved" and any(d["discussion"] for r in calculate(snap) for d in r["detail"]) and not reason.strip():
                raise ValueError("Есть расхождения: укажите решение обсуждения в поле причины")
            db.execute("UPDATE event SET status=?,updated_at=?", (target, now()))
            self.log(db, "secretary", "transition", e["id"], {"from": e["status"], "to": target, "reason": reason})
            if target == "approved":
                snap = self._snapshot(db)
                db.execute("INSERT INTO snapshots VALUES (?,?,?)", (snap["id"], snap["created_at"], json.dumps(snap, ensure_ascii=False)))

    def _snapshot(self, db):
        decisions = [{"at": r["at"], "action": r["action"], "object_id": r["object_id"], "details": json.loads(r["details"])} for r in db.execute("SELECT at,action,object_id,details FROM audit WHERE action IN ('transition','unlock','archive','remove_judge') ORDER BY id")]
        return {"id": ident(), "created_at": now(), "schema_version": 1, "event": self.event(db), "judges": [dict(r) for r in db.execute("SELECT id,number,active FROM judges ORDER BY number")], "participants": self.participants(db), "cards": self.cards(db), "penalties": [], "decisions": decisions, "rounding": "half_up_final_0.01", "ties": "1,2,2,4"}

    def snapshot(self, token):
        with self.transaction() as db:
            self.admin(db, token)
            status = self.event(db)["status"]
            if status not in ("closed", "approved"):
                raise PermissionError("Числовые результаты доступны после закрытия")
            if status == "approved":
                return json.loads(db.execute("SELECT payload FROM snapshots ORDER BY rowid DESC LIMIT 1").fetchone()[0])
            return self._snapshot(db)

    def audit(self, token):
        with self.transaction() as db:
            self.admin(db, token)
            return [dict(r) for r in db.execute("SELECT id,at,actor,action,object_id FROM audit ORDER BY id DESC LIMIT 500")]

    def backup_internal(self, label="manual"):
        dest = self.root / "backups" / (datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_") + ident()[:8] + "_" + label + ".sqlite3")
        with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(dest)) as target:
            source.backup(target)
        return dest

    def backup(self, token):
        with self.transaction() as db:
            self.admin(db, token)
        return self.backup_internal()

    def migrate(self, token, legacy_path):
        """Explicit one-time import; password and schema setup remain under local control."""
        path = Path(legacy_path)
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("Корень JSON должен быть объектом")
        if "commands" not in data:
            data = {"commands": {k: v for k, v in data.items() if isinstance(v, dict) and "title" in v}, "judges_count": 0}
        with self.transaction() as db:
            self.admin(db, token)
            if self.setting(db, "legacy_imported"):
                raise ValueError("Импорт уже выполнен")
            if self.event(db)["status"] != "preparation" or db.execute("SELECT 1 FROM participants UNION SELECT 1 FROM judges LIMIT 1").fetchone():
                raise ValueError("Импорт разрешён только в пустое мероприятие")
            count = data.get("judges_count", 0)
            commands = data.get("commands", {})
            if not isinstance(commands, dict) or any(not isinstance(c, dict) for c in commands.values()):
                raise ValueError("Некорректная структура списка работ")
            criteria = self.event(db)["criteria"]
            errors = []
            if type(count) is not int or not 1 <= count <= 50:
                errors.append("Количество судей должно находиться в диапазоне 1..50")
            for key, cmd in commands.items():
                if not isinstance(cmd.get("title"), str) or not cmd["title"].strip():
                    errors.append(f"Работа {key}: отсутствует название")
                if not isinstance(cmd.get("members", []), list) or any(not isinstance(m, str) for m in cmd.get("members", [])):
                    errors.append(f"Работа {key}: участники должны быть списком строк")
                if not isinstance(cmd.get("rates", {}), dict):
                    errors.append(f"Работа {key}: некорректные оценки")
                    continue
                for jid, scores in cmd.get("rates", {}).items():
                    try:
                        if not isinstance(scores, dict):
                            raise ValueError("Оценки должны быть объектом")
                        if not jid.isdigit() or not 1 <= int(jid) <= count:
                            raise ValueError("Судья вне состава")
                        validate_scores(scores, criteria)
                    except ValueError as exc:
                        errors.append(f"Работа {key}, судья {jid}: {exc}")
            if errors:
                raise ValueError("Импорт отменён:\n" + "\n".join(errors))
            copy = self.root / "backups" / ("legacy_" + digest(raw.hex())[:12] + ".json")
            if not copy.exists():
                shutil.copy2(path, copy)
            mapping = {}
            for number in range(1, count + 1):
                jid = ident()
                mapping[number] = jid
                db.execute("INSERT INTO judges VALUES (?,?,?,1)", (jid, number, digest(secrets.token_urlsafe(32))))
            for number, cmd in enumerate(commands.values(), 1):
                pid = ident()
                db.execute("INSERT INTO participants(id,number,title,description,members,opened) VALUES (?,?,?,?,?,1)", (pid, number, cmd["title"], cmd.get("description", ""), json.dumps(cmd.get("members", []), ensure_ascii=False)))
                for old, scores in cmd.get("rates", {}).items():
                    db.execute("INSERT INTO cards VALUES (?,?,?,?,?,?,?,?)", (mapping[int(old)], pid, json.dumps(scores), "", "submitted", 1, now(), now()))
            db.execute("UPDATE event SET algorithm='legacy_total_trim_v1'")
            self.set_setting(db, "method_confirmed", "no")
            self.set_setting(db, "legacy_imported", str(copy))
            self.log(db, "secretary", "legacy_import", details={"works": len(commands), "judges": count})

    def reset_password_local(self, password):
        """Local CLI only: no web endpoint. Requires access to the server's user account."""
        if len(password) < 12:
            raise ValueError("Минимум 12 символов")
        self.backup_internal("password_reset")
        salt = secrets.token_hex(16)
        value = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()
        with self.transaction() as db:
            self.set_setting(db, "password", salt + ":" + value)
            db.execute("DELETE FROM sessions")
            self.log(db, "local", "password_reset")
