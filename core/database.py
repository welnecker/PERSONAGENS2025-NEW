# core/database.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Iterable, Tuple
from threading import RLock
import os
import uuid
import datetime as _dt

from .config import settings

# ===================== Estado global do backend =====================
_BACKEND = (os.getenv("DB_BACKEND", "").strip().lower() or "memory")

def get_backend() -> str:
    return _BACKEND

def set_backend(kind: str) -> None:
    global _BACKEND
    kind = (kind or "").strip().lower()
    _BACKEND = "mongo" if kind == "mongo" else "memory"

# Se houver credenciais de Mongo e DB_BACKEND não foi explicitado, defaulta para mongo
if settings.mongo_uri():
    env_choice = os.getenv("DB_BACKEND", "").strip().lower()
    if env_choice == "":
        _BACKEND = "mongo"

# ===================== Implementação: Memória =====================
_STORE: Dict[str, List[Dict[str, Any]]] = {}
_LOCK = RLock()

def _get_nested(d: Dict[str, Any], dotted: str, default=None):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return default
        if part not in cur:
            return default
        cur = cur[part]
    return cur

def _set_nested(d: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value

def _unset_nested(d: Dict[str, Any], dotted: str) -> None:
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            return
        cur = cur[p]
    cur.pop(parts[-1], None)

def _match_simple(doc: Dict[str, Any], filt: Optional[Dict[str, Any]]) -> bool:
    if not filt:
        return True
    for k, v in filt.items():
        # suporte mínimo a $in e chaves pontilhadas
        if isinstance(v, dict) and "$in" in v:
            if "." in k:
                value = _get_nested(doc, k, None)
            else:
                value = doc.get(k)
            if value not in v["$in"]:
                return False
        else:
            if "." in k:
                if _get_nested(doc, k, object()) != v:
                    return False
            else:
                if doc.get(k) != v:
                    return False
    return True

class MemoryCollection:
    def __init__(self, name: str):
        self.name = name
        _STORE.setdefault(name, [])

    def insert_one(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        with _LOCK:
            d = dict(doc)
            d.setdefault("_id", str(uuid.uuid4()))
            d.setdefault("ts", _dt.datetime.utcnow())
            _STORE[self.name].append(d)
            return {"inserted_id": d["_id"]}

    def find(
        self,
        filt: Optional[Dict[str, Any]] = None,
        sort: Optional[List[Tuple[str, int]]] = None,
        limit: Optional[int] = None,
    ) -> Iterable[Dict[str, Any]]:
        with _LOCK:
            rows = [d.copy() for d in _STORE.get(self.name, []) if _match_simple(d, filt)]
        if sort:
            # aplica múltiplas chaves, da última para a primeira
            for key, direction in reversed(sort):
                rows.sort(
                    key=lambda x: _get_nested(x, key, None),
                    reverse=(direction or 1) < 0
                )
        if limit:
            rows = rows[:limit]
        return rows

    def find_one(
        self,
        filt: Optional[Dict[str, Any]] = None,
        sort: Optional[List[Tuple[str, int]]] = None,
    ) -> Optional[Dict[str, Any]]:
        rows = list(self.find(filt=filt, sort=sort, limit=1))
        return rows[0] if rows else None

    def update_one(self, filt: Dict[str, Any], update: Dict[str, Any], upsert: bool = False) -> None:
        """
        Suporta:
          - $set com chaves pontilhadas
          - $unset com chaves pontilhadas
          - upsert
        """
        with _LOCK:
            rows = _STORE.get(self.name, [])

            # tenta atualizar documento existente
            for d in rows:
                if _match_simple(d, filt):
                    if "$set" in update or "$unset" in update:
                        if "$set" in update:
                            for k, v in update["$set"].items():
                                if "." in k:
                                    _set_nested(d, k, v)
                                else:
                                    d[k] = v
                        if "$unset" in update:
                            for k in update["$unset"].keys():
                                if "." in k:
                                    _unset_nested(d, k)
                                else:
                                    d.pop(k, None)
                    else:
                        d.update(update)
                    return

            # se não encontrou e for upsert → cria
            if upsert:
                new_doc: Dict[str, Any] = dict(filt)
                if "$set" in update:
                    for k, v in update["$set"].items():
                        if "." in k:
                            _set_nested(new_doc, k, v)
                        else:
                            new_doc[k] = v
                else:
                    new_doc.update(update)
                self.insert_one(new_doc)

    def delete_many(self, filt: Dict[str, Any]) -> int:
        with _LOCK:
            rows = _STORE.get(self.name, [])
            before = len(rows)
            rows[:] = [d for d in rows if not _match_simple(d, filt)]
            return before - len(rows)

    def delete_one(self, filt: Dict[str, Any]) -> Dict[str, Any]:
        r = self._col.delete_one(filt or {})
        return {"deleted_count": int(getattr(r, "deleted_count", 0) or 0)}
    
    def delete_one(self, filt: Dict[str, Any]) -> Dict[str, Any]:
        """
        Remove o primeiro doc que casar com o filtro.
        Retorna {"deleted_count": 0|1} para compatibilidade com pymongo.
        """
        with _LOCK:
            rows = _STORE.get(self.name, [])
            for i, d in enumerate(rows):
                if _match_simple(d, filt):
                    rows.pop(i)
                    return {"deleted_count": 1}
            return {"deleted_count": 0}
    

# ===================== Implementação: Mongo (opcional) =====================
_MONGO_OK = False
_mongo_client = None
_mongo_db = None

def _close_mongo():
    global _mongo_client
    if _mongo_client:
        try:
            _mongo_client.close()
        except Exception:
            pass
        _mongo_client = None

import atexit
atexit.register(_close_mongo)

def _ensure_mongo():
    global _MONGO_OK, _mongo_client, _mongo_db
    # Double-check locking pattern could be better, but we are under GIL for global var assign effectively.
    # To be safer with threads, we use the existing _LOCK or a new one.
    if _mongo_db is not None:
        return

    uri = settings.mongo_uri()
    if not uri:
        _MONGO_OK = False
        return

    with _LOCK:
        if _mongo_db is not None:
            return
        
        try:
            from pymongo import MongoClient
            # Melhora robustez e eficiência de conexões
            _mongo_client = MongoClient(
                uri,
                maxPoolSize=50,
                minPoolSize=5,
                serverSelectionTimeoutMS=5000, # Fail fast se DB cair
                connectTimeoutMS=5000,
                socketTimeoutMS=5000,
            )
            _mongo_db = _mongo_client.get_database(settings.APP_NAME)
            _MONGO_OK = True
        except Exception:
            _MONGO_OK = False
            _mongo_client = None
            _mongo_db = None

class MongoCollection:
    def __init__(self, name: str):
        _ensure_mongo()
        if not _MONGO_OK:
            raise RuntimeError("Mongo não inicializado.")
        self._col = _mongo_db.get_collection(name)

    def insert_one(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        d = dict(doc)
        d.setdefault("ts", _dt.datetime.utcnow())
        r = self._col.insert_one(d)
        return {"inserted_id": str(r.inserted_id)}

    def find(
        self,
        filt: Optional[Dict[str, Any]] = None,
        sort: Optional[List[Tuple[str, int]]] = None,
        limit: Optional[int] = None,
    ) -> Iterable[Dict[str, Any]]:
        cur = self._col.find(filt or {})
        if sort:
            cur = cur.sort(sort)
        if limit:
            cur = cur.limit(limit)
        return list(cur)

    def find_one(
        self,
        filt: Optional[Dict[str, Any]] = None,
        sort: Optional[List[Tuple[str, int]]] = None,
    ) -> Optional[Dict[str, Any]]:
        if sort:
            cur = self._col.find(filt or {}).sort(sort).limit(1)
            rows = list(cur)
            return rows[0] if rows else None
        return self._col.find_one(filt or {})

    def update_one(self, filt: Dict[str, Any], update: Dict[str, Any], upsert: bool = False) -> None:
        self._col.update_one(filt, update, upsert=upsert)

    def delete_many(self, filt: Dict[str, Any]) -> int:
        return self._col.delete_many(filt or {}).deleted_count

# ===================== API pública =====================
def get_col(name: str):
    """Retorna uma coleção de acordo com o backend atual."""
    if get_backend() == "mongo":
        try:
            return MongoCollection(name)
        except Exception:
            # fallback duro para memória se Mongo falhar
            return MemoryCollection(name)
    return MemoryCollection(name)

def db_status() -> Tuple[str, str]:
    """(backend, detalhe)"""
    b = get_backend()
    if b == "mongo":
        _ensure_mongo()
        detail = "OK" if _MONGO_OK else "indisponível"
        return ("mongo", detail)
    return ("memory", "memória local")

def ping_db() -> Tuple[str, bool, str]:
    """(backend, ok, detalhe) — faz um insert+read+delete na coleção 'diagnostic'."""
    b = get_backend()
    try:
        col = get_col("diagnostic")
        rid = col.insert_one({"marker": "ping", "ts": _dt.datetime.utcnow()}).get("inserted_id")
        last = col.find_one(sort=[("ts", -1)])
        col.delete_many({"marker": "ping"})
        return (b, True, f"OK (id={rid}) — last_ts={last.get('ts') if last else '—'}")
    except Exception as e:
        return (b, False, f"{type(e).__name__}: {e}")
