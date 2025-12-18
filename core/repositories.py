# core/repositories.py
from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime

from .database import get_col

# coleções
_state  = lambda: get_col("state_data")
_hist   = lambda: get_col("history")
_events = lambda: get_col("events")


# ---------- helpers internos ----------
def _delete_dotted(root: Dict[str, Any], dotted_key: str) -> bool:
    """
    Remove a chave `dotted_key` (ex.: "perfil.endereco.rua") de um dict aninhado,
    modificando `root` in-place. Faz prune de dicts vazios no caminho.
    Retorna True se removeu algo.
    """
    if not dotted_key:
        return False

    parts = [p for p in dotted_key.split(".") if p]
    if not parts:
        return False

    stack: List[tuple[Dict[str, Any], str]] = []
    cur: Any = root

    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return False
        stack.append((cur, p))
        cur = cur[p]

    leaf = parts[-1]
    if not isinstance(cur, dict) or leaf not in cur:
        return False

    # remove a folha
    del cur[leaf]

    # prune dicts vazios, de baixo pra cima
    while stack:
        parent, key = stack.pop()
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            del parent[key]
        else:
            break

    return True


# ---------- Fatos ----------
def get_facts(usuario: str) -> Dict[str, Any]:
    d = _state().find_one({"usuario": usuario})
    return d.get("fatos", {}) if d else {}


def get_fact(usuario: str, key: str, default: Any = None) -> Any:
    d = _state().find_one({"usuario": usuario})
    if not d:
        return default
    cur: Any = d.get("fatos", {})
    for part in (key or "").split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


from datetime import datetime

def set_fact(usuario: str, key: str, value: Any, meta: Optional[Dict[str, Any]] = None) -> None:
    """
    Seta um fact (chave pontilhada vira 'fatos.<key>').
    Meta: guarda por chave em 'meta.<key>' e atualiza meta.updated_at.
    """
    meta = meta or {}
    _state().update_one(
        {"usuario": usuario},
        {"$set": {
            "usuario": usuario,
            f"fatos.{key}": value,
            f"meta.{key}": meta,
            "meta.updated_at": datetime.utcnow(),
        }},
        upsert=True
    )


def delete_fact(usuario: str, key: str) -> bool:
    """
    Remove uma memória canônica (suporta chave pontilhada).
    Atualiza o bloco 'fatos' inteiro (mais robusto que depender de $unset).
    """
    doc = _state().find_one({"usuario": usuario})
    if not doc:
        return False

    facts = dict(doc.get("fatos", {}) or {})
    if not _delete_dotted(facts, key):
        return False

    _state().update_one({"usuario": usuario}, {"$set": {"fatos": facts}}, upsert=True)
    return True


# ---------- Histórico ----------
def save_interaction(usuario: str, mensagem_usuario: str, resposta_mary: str, model_tag: str) -> None:
    """
    Salva um turno de conversa. Mantém o campo legado 'resposta_mary' (UI depende dele).
    """
    _hist().insert_one({
        "usuario": usuario,
        "mensagem_usuario": mensagem_usuario,
        "resposta_mary": resposta_mary,
        "model": model_tag,
        "ts": datetime.utcnow(),  # ordenação estável
    })


def get_history_docs(usuario: str, limit: int = 400):
    """Retorna histórico em ordem cronológica (mais antigo → mais recente).
    Robusto para backends:
    - Mongo/PyMongo (cursor com .sort/.limit)
    - Backends que retornam LISTA no .find()
    """
    col = get_col("history")
    q = {"usuario": usuario}

    docs = []
    try:
        if hasattr(col, "find"):
            res = col.find(q)

            # Cursor (pymongo)
            if hasattr(res, "sort") and hasattr(res, "limit"):
                try:
                    res = res.sort([("ts", 1), ("_id", 1)]).limit(int(limit))
                    docs = list(res)
                except TypeError:
                    # .find() devolveu lista (não dá pra encadear .sort(list))
                    docs = list(res)
            else:
                docs = list(res)

        elif isinstance(col, list):
            docs = [d for d in col if isinstance(d, dict) and d.get("usuario") == usuario]
    except Exception:
        docs = []

    def _k(d):
        ts = d.get("ts", 0) or 0
        _id = d.get("_id", "") or ""
        return (ts, str(_id))

    docs = [d for d in docs if isinstance(d, dict)]
    docs_sorted = sorted(docs, key=_k)

    # mantém os mais recentes, mas em ordem cronológica
    if limit and len(docs_sorted) > int(limit):
        docs_sorted = docs_sorted[-int(limit):]

    return docs_sorted


def get_history_docs_multi(usuarios: list[str], limit: int = 800):
    """Une histórico de múltiplas chaves (novo + legado), ordena e devolve os mais recentes."""
    if not usuarios:
        return []

    col = get_col("history")
    q = {"usuario": {"$in": usuarios}}

    docs = []
    try:
        if hasattr(col, "find"):
            res = col.find(q)

            if hasattr(res, "sort") and hasattr(res, "limit"):
                try:
                    # pega “a mais” pra depois cortar sem perder os últimos
                    res = res.sort([("ts", 1), ("_id", 1)]).limit(int(limit) * 3)
                    docs = list(res)
                except TypeError:
                    docs = list(res)
            else:
                docs = list(res)

        elif isinstance(col, list):
            u_set = set(usuarios)
            docs = [d for d in col if isinstance(d, dict) and d.get("usuario") in u_set]
    except Exception:
        docs = []

    def _k(d):
        ts = d.get("ts", 0) or 0
        _id = d.get("_id", "") or ""
        return (ts, str(_id))

    docs = [d for d in docs if isinstance(d, dict)]
    docs_sorted = sorted(docs, key=_k)

    if limit and len(docs_sorted) > int(limit):
        docs_sorted = docs_sorted[-int(limit):]

    return docs_sorted


def delete_last_interaction(usuario: str) -> bool:
    """Apaga o ÚLTIMO registro do histórico do usuário."""
    col = get_col("history")

    # 1) PyMongo
    try:
        if hasattr(col, "find_one") and hasattr(col, "delete_one"):
            last = col.find_one({"usuario": usuario}, sort=[("ts", -1), ("_id", -1)])
            if not last:
                return False
            _id = last.get("_id")
            if _id is None:
                return False
            res = col.delete_one({"_id": _id})
            return bool(getattr(res, "deleted_count", 0))
    except Exception:
        pass

    # 2) fallback: usa get_history_docs e tenta deletar por _id
    try:
        docs = get_history_docs(usuario, limit=5000) or []
        if not docs:
            return False
        last = docs[-1]
        _id = last.get("_id", None)

        if hasattr(col, "delete_one") and _id is not None:
            res = col.delete_one({"_id": _id})
            return bool(getattr(res, "deleted_count", 0))

        # 3) último fallback: lista mutável
        if isinstance(col, list):
            if _id is not None:
                for i in range(len(col) - 1, -1, -1):
                    d = col[i]
                    if isinstance(d, dict) and d.get("_id") == _id:
                        col.pop(i)
                        return True
            for i in range(len(col) - 1, -1, -1):
                d = col[i]
                if isinstance(d, dict) and d.get("usuario") == usuario:
                    col.pop(i)
                    return True
    except Exception:
        pass

    return False


def delete_user_history(usuario: str) -> int:
    """Apaga TODO histórico do usuário. Retorna quantidade removida (quando possível)."""
    col = get_col("history")

    # PyMongo
    try:
        if hasattr(col, "delete_many"):
            res = col.delete_many({"usuario": usuario})
            return int(getattr(res, "deleted_count", 0) or 0)
    except Exception:
        pass

    # Lista mutável
    try:
        if isinstance(col, list):
            before = len(col)
            col[:] = [d for d in col if not (isinstance(d, dict) and d.get("usuario") == usuario)]
            return before - len(col)
    except Exception:
        pass

    return 0




# ---------- Eventos ----------
def register_event(
    usuario: str,
    tipo: str,
    descricao: str,
    local: Optional[str],
    extra: Optional[Dict[str, Any]] = None
) -> None:
    _events().insert_one({
        "usuario": usuario,
        "tipo": tipo,
        "descricao": descricao,
        "local": local,
        "extra": extra or {},
        "ts": datetime.utcnow(),
    })


def list_events(usuario: str, limit: int = 5) -> List[Dict[str, Any]]:
    cur = (
        _events()
        .find({"usuario": usuario})
        .sort([("ts", -1), ("_id", -1)])
        .limit(limit)
    )
    return list(cur)


# ---------- Utilidades ----------
def last_event(usuario: str, tipo: str) -> Optional[Dict[str, Any]]:
    return _events().find_one(
        {"usuario": usuario, "tipo": tipo},
        sort=[("ts", -1), ("_id", -1)]
    )


def _safe_create_index(col_obj, keys):
    """
    Tenta criar índice tanto em coleções pymongo puras (create_index)
    quanto em wrappers (obj._col.create_index).
    """
    try:
        if hasattr(col_obj, "create_index"):
            col_obj.create_index(keys)
            return True
    except Exception:
        pass

    try:
        inner = getattr(col_obj, "_col", None)
        if inner is not None and hasattr(inner, "create_index"):
            inner.create_index(keys)
            return True
    except Exception:
        pass

    return False


def ensure_indexes() -> None:
    """
    Garante índices essenciais.
    Só roda quando backend for mongo.
    """
    try:
        from .database import get_backend
        if get_backend() != "mongo":
            return

        # History: busca por usuario, ordenado por data
        _safe_create_index(_hist(), [("usuario", 1), ("ts", 1), ("_id", 1)])

        # State: busca por usuario
        _safe_create_index(_state(), [("usuario", 1)])

        # Events: busca por usuario, mais recentes
        _safe_create_index(_events(), [("usuario", 1), ("ts", -1), ("_id", -1)])

    except Exception:
        pass
