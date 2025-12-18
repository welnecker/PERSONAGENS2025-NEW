from __future__ import annotations

"""
MaryService (v5 - Minimal-Strong Continuity)
- Continuidade espacial/temporal/situacional forte (sem teleporte)
- Intro inicial: 1 de 2, escolhida aleatoriamente, MAS fixada canonicamente (não injeta a outra depois)
- Estado sexual: mantido SOMENTE no backend (não injeta instruções CRSM no SYSTEM)
- Hooks finais: NÃO adiciona hook quando está em "resolucao" (pós-clímax)
- Trimming determinístico corrigido (bug do test)
- toklen robusto com fallback (tiktoken opcional)
"""

import json
import logging
import random
import re
import time
import hashlib
from typing import Any, Dict, List, Tuple, Optional

import streamlit as st

from core.common.base_service import BaseCharacter
from core.memoria_longa import topk as lore_topk
from core.repositories import (
    get_fact,
    get_facts,
    get_history_docs,
    save_interaction,
    set_fact,
)
from core.service_router import list_models, route_chat_strict
from core.ultra import critic_review, polish
from characters.registry import _SERVICE_CACHE

from .persona import get_persona

logger = logging.getLogger(__name__)
_SERVICE_CACHE.clear()


# ==========================================================
# TOKENS (tiktoken opcional) ✅
# ==========================================================
_TIKTOKEN_ENCODER = None

def _get_encoder(model: str | None = None):
    global _TIKTOKEN_ENCODER
    try:
        import tiktoken
        if model:
            try:
                return tiktoken.encoding_for_model(model)
            except Exception:
                pass
        if _TIKTOKEN_ENCODER is None:
            _TIKTOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
        return _TIKTOKEN_ENCODER
    except Exception:
        return None


def toklen(txt: str, model: str | None = None) -> int:
    t = (txt or "")
    if not t.strip():
        return 1

    enc = _get_encoder(model)
    if enc is not None:
        try:
            return max(1, len(enc.encode(t)))
        except Exception:
            pass

    # fallback robusto (não explode em textos longos)
    chars_est = int(len(t) / 4.0)
    words_est = int(len(t.split()) * 1.3)
    return max(1, max(chars_est, words_est))


# ==========================================================
# NSFW / ESTILO ADULTO (sem “robô agressivo”) ✅
# ==========================================================
SAFE_SENSUAL_STYLE = """
[NSFW_OFF]
Estilo: romance adulto, tensão, intimidade e sugestão.
- Evite detalhes gráficos; foque em clima, emoção, desejo, presença e condução da cena.
- Mantenha continuidade e cumplicidade.
- NÃO finalize a cena de forma conclusiva; deixe espaço para o próximo turno.
""".strip()

NSFW_BALANCED_STYLE = """
[NSFW_ON — MODO ADULTO (quente, humano, sem exagero mecânico)]
- Seja intensa, provocante e explícita no clima, mas sempre COERENTE com o que já está acontecendo.
- NÃO contrarie o que o usuário acabou de fazer/descrever: você reage, aceita e intensifica.
- NÃO invente “condições” para travar a progressão se o usuário já colocou a ação em andamento.
- Progressão natural: aproximação → toque → roupa → intensidade → pausa/afeto. (Sem pular do nada.)
- Pós-clímax: pausa e sensibilidade natural antes de “rodada 2”.
""".strip()


# ==========================================================
# USER KEY ✅ (default fixo)
# ==========================================================
def _current_user_key() -> str:
    uid = st.session_state.get("user_id") or st.session_state.get("usuario") or ""
    uid = str(uid).strip() or "Janio Donisete"
    return f"{uid}::mary"


# ==========================================================
# CACHE
# ==========================================================
def cached_get_facts(usuario_key: str) -> Dict[str, Any]:
    ck = f"facts::{usuario_key}"
    if ck in st.session_state:
        return st.session_state[ck]
    try:
        f = get_facts(usuario_key) or {}
    except Exception:
        f = {}
    st.session_state[ck] = f
    return f


def cached_get_history(usuario_key: str) -> List[Dict[str, Any]]:
    hk = f"history::{usuario_key}"
    if hk in st.session_state:
        return st.session_state[hk]
    try:
        docs = get_history_docs(usuario_key) or []
    except Exception:
        docs = []
    st.session_state[hk] = docs
    return docs


def clear_user_cache(usuario_key: str) -> None:
    for k in (f"facts::{usuario_key}", f"history::{usuario_key}"):
        try:
            if k in st.session_state:
                del st.session_state[k]
        except Exception:
            pass


def nsfw_enabled(usuario_key: str) -> bool:
    """Prioridade:
    1) st.session_state["mary_nsfw_on"]
    2) Fact "mary.nsfw"
    3) True
    """
    try:
        if "mary_nsfw_on" in st.session_state:
            return bool(st.session_state["mary_nsfw_on"])
    except Exception:
        pass

    try:
        f = cached_get_facts(usuario_key) or {}
        v = f.get("mary.nsfw", None)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("1", "true", "sim", "on", "yes", "y"):
                return True
            if s in ("0", "false", "nao", "não", "off", "no", "n"):
                return False
    except Exception:
        pass

    return True


# ==========================================================
# LOG / ERROS
# ==========================================================
def _log_error(context: str, exc: Exception) -> None:
    msg = f"[MaryService][{context}] {type(exc).__name__}: {exc}"
    try:
        logger.exception(msg)
    except Exception:
        pass

    try:
        if st.session_state.get("mary_debug_errors"):
            st.error(msg)
    except Exception:
        pass


# ==========================================================
# PREFERÊNCIAS
# ==========================================================
def _read_prefs(facts: Dict[str, Any]) -> Dict[str, str]:
    ritmo = facts.get("mary.pref.ritmo") or "rapido"       # rapido | normal | lento
    tamanho = facts.get("mary.pref.tamanho") or "longa"    # curta | media | longa
    return {"ritmo": str(ritmo), "tamanho_resposta": str(tamanho)}


def _prefs_line(prefs: Dict[str, str]) -> str:
    return f"ritmo={prefs.get('ritmo')}; tamanho_resposta={prefs.get('tamanho_resposta')}"


# ==========================================================
# CENA (LOCAL / TEMPO / AÇÃO)
# ==========================================================
def _get_scene_state(usuario_key: str, facts: Dict[str, Any]) -> Tuple[str, str, str]:
    local = str(facts.get("cena.local") or facts.get("local_cena_atual") or "").strip()
    tempo = str(facts.get("cena.tempo") or "").strip()
    acao  = str(facts.get("cena.acao") or "").strip()

    if not local:
        local = "—"
    if not tempo:
        tempo = "agora"
    if not acao:
        acao = "em andamento"

    return local, tempo, acao


def _persist_scene_basics(usuario_key: str, local: str, tempo: str, acao: str) -> None:
    try:
        if local and local != "—":
            set_fact(usuario_key, "cena.local", local, {"fonte": "scene"})
            set_fact(usuario_key, "local_cena_atual", local, {"fonte": "scene_compat"})
        if tempo:
            set_fact(usuario_key, "cena.tempo", tempo, {"fonte": "scene"})
        if acao:
            set_fact(usuario_key, "cena.acao", acao, {"fonte": "scene"})
    except Exception:
        pass


def _build_spatial_context(local: str, tempo: str, acao: str) -> str:
    if not local or local == "—":
        return ""
    return f"""
[CONTEXTO ESPACIAL — OBRIGATÓRIO]
📍 Local atual: {local}
⏰ Tempo: {tempo}
🎬 Ação em andamento: {acao}

REGRA CRÍTICA:
- Mantenha a cena em "{local}" até o usuário pedir mudança explicitamente.
- Não invente móveis/objetos de outro cômodo.
""".strip()


def _user_requested_location_change(user_message: str) -> Tuple[bool, str]:
    patterns = [
        r"vamos? (pro|pra|para o|para a) (\w+)",
        r"me leva (pro|pra|para o|para a) (\w+)",
        r"vem (aqui )?(no|na) (\w+)",
    ]
    user_lower = (user_message or "").lower()
    for pattern in patterns:
        match = re.search(pattern, user_lower)
        if match:
            location_word = match.group(2)
            location_map = {
                "quarto": "quarto", "cama": "quarto",
                "cozinha": "cozinha", "geladeira": "cozinha",
                "sala": "sala", "sofá": "sala", "sofa": "sala",
                "banheiro": "banheiro", "chuveiro": "banheiro",
            }
            normalized = location_map.get(location_word, location_word)
            return True, normalized
    return False, ""


# ==========================================================
# ESTADO SEXUAL (backend only) ✅
# ==========================================================
def _get_sexual_state(usuario_key: str, facts: Dict[str, Any]) -> str:
    state = str(facts.get("cena.estado_sexual") or "").strip().lower()
    if state not in ["desejo", "excitacao", "plato", "orgasmo", "resolucao"]:
        state = "desejo"
    return state


def _set_sexual_state(usuario_key: str, new_state: str) -> None:
    try:
        set_fact(usuario_key, "cena.estado_sexual", new_state, {"fonte": "crsm_backend"})
    except Exception:
        pass


def _detect_orgasm_in_text(text: str) -> bool:
    patterns = [
        r"\bgoz(o|a|ei|ou|ando|ar)\b",
        r"\bclímax\b",
        r"\bvou gozar\b",
        r"\bestou gozando\b",
        r"\borgasmo\b",
        r"ondas de prazer",
    ]
    t = (text or "").lower()
    return any(re.search(p, t) for p in patterns)


# ==========================================================
# JANELA / BUDGET
# ==========================================================
_DEFAULT_WINDOW = 16000

def _get_window_for(model_id: str) -> int:
    if not model_id:
        return _DEFAULT_WINDOW
    m = model_id.lower().strip()

    if "deepseek-r1" in m or "deepseek-reasoner" in m:
        return 163840
    if "deepseek/deepseek-chat-v3-0324" in m:
        return 8192
    if "deepseek-chat" in m:
        return 65536
    if "gpt-4.1" in m or "gpt-4.5" in m:
        return 128000
    if "llama-3.1" in m:
        return 128000
    if "qwen2.5-72b" in m:
        return 32000
    if "qwen3-coder-480b" in m:
        return 262144
    if "claude-3.5" in m:
        return 200000
    if "grok-4.1" in m:
        return 2000000
    if "deepseek-r1t2-chimera" in m or "tngtech/deepseek-r1t2-chimera:free" in m:
        return 163840

    return _DEFAULT_WINDOW


def _budget_slices(model_id: str) -> Tuple[int, int, int]:
    win = _get_window_for(model_id)
    hist = int(win * 0.70)
    meta = int(win * 0.15)
    safety = win - hist - meta
    return hist, meta, safety


def _safe_max_output(window_tokens: int, prompt_tokens: int) -> int:
    if window_tokens <= 0:
        window_tokens = _DEFAULT_WINDOW
    max_out = int(window_tokens * 0.25)
    if prompt_tokens > window_tokens * 0.80:
        max_out = int(window_tokens * 0.18)
    return max(512, max_out)


def _estimate_messages_tokens(messages: List[Dict[str, Any]], model: str | None = None) -> int:
    total = 0
    for m in (messages or []):
        total += toklen(m.get("content", "") or "", model=model)
        total += 6
        if m.get("tool_calls"):
            total += 40
    return max(1, total)


# ==========================================================
# SUMMARIZER
# ==========================================================
def _llm_summarize(model_id: str, text: str) -> str:
    if not text.strip():
        return ""

    candidates = [
        "together/Qwen/Qwen2.5-32B-Instruct",
        "together/meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
        "deepseek/deepseek-chat-v3-0324",
    ]
    use_model = model_id or candidates[0]

    mlow = (use_model or "").lower()
    if any(x in mlow for x in ["grok-4.1", "deepseek-r1t2-chimera"]):
        available = set(list_models() or [])
        for c in candidates:
            if c in available:
                use_model = c
                break
        else:
            use_model = candidates[0]

    seed = (
        "Resuma este histórico entre Mary e o usuário em 8–12 frases curtas. "
        "Foque em fatos duráveis: relação, local, acordos, conflitos, eventos marcantes. "
        "Não repita diálogos literais e não invente fatos."
    )

    body = {
        "model": use_model,
        "messages": [
            {"role": "system", "content": seed},
            {"role": "user", "content": text},
        ],
        "max_tokens": 260,
        "temperature": 0.2,
        "top_p": 0.9,
    }
    try:
        data, _, _ = route_chat_strict(use_model, body)
        msg = (data.get("choices", [{}])[0].get("message", {}) or {})
        return (msg.get("content") or "").strip()
    except Exception:
        return ""


# ==========================================================
# ENTIDADES / EVENTOS / LORE
# ==========================================================
def _entities_to_line(f: Dict[str, Any]) -> str:
    ents = []
    for k, v in (f or {}).items():
        if isinstance(k, str) and k.startswith("mary.ent.") and v:
            label = k.replace("mary.ent.", "", 1)
            vs = str(v).strip()
            if vs:
                ents.append(f"{label}={vs}")
    return "; ".join(sorted(ents)) if ents else "—"


def _collect_mary_events_from_facts(facts: Dict[str, Any]) -> Dict[str, str]:
    eventos: Dict[str, str] = {}
    if not isinstance(facts, dict):
        return eventos
    for k, v in facts.items():
        if not isinstance(k, str) or not v:
            continue
        if k.startswith("mary.evento."):
            label = k.replace("mary.evento.", "", 1)
            eventos[label] = str(v)
    return eventos


def _get_lorebook(usuario_key: str, query: str, k: int = 4, max_chars: int = 900) -> str:
    try:
        results = lore_topk(usuario_key, query, k=k)
    except Exception:
        return ""

    if not results:
        return ""

    lines = []
    total = 0
    for r in results:
        content = str(r.get("content", "") or "").strip()
        if not content:
            continue
        lines.append(f"- {content}")
        total += len(content)
        if total > max_chars:
            break

    return "\n".join(lines) if lines else ""


# ==========================================================
# CONTINUIDADE: anti-finalização + sem pergunta final
# ==========================================================
def _user_requested_conclusion(prompt: str) -> bool:
    p = (prompt or "").lower()
    keys = ["termina", "finaliza", "conclui", "acaba", "pode terminar", "pode finalizar"]
    return any(k in p for k in keys)


def _looks_like_conclusion(text: str) -> bool:
    t = (text or "").lower()
    patterns = [
        r"\bacab(ou|a|amos)\b",
        r"\btermin(ou|a|amos)\b",
        r"\bfinaliz(ou|a|amos)\b",
        r"\badormec(emos|i|eu)\b",
        r"\bfim\b",
    ]
    return any(re.search(p, t) for p in patterns)


def _already_open_ended(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    return bool(re.search(r"(\.\.\.|—)\s*$", t))


def _pick_hook(prompt: str, last_text: str) -> str:
    seed_src = (prompt or "") + "||" + (last_text[-80:] if last_text else "")
    seed = int(hashlib.md5(seed_src.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    hooks = [
        "\n\nEu não paro — só diminuo um instante, ainda perto demais, como se estivesse esperando você conduzir o próximo passo…",
        "\n\nEu mordo o lábio, sentindo o corpo responder, e deixo isso no ar — do jeito que você gosta…",
        "\n\nEu prendo a respiração por um segundo, na beira, e não passo dali sem você…",
        "\n\nEu encosto em você de novo, devagar, como se estivesse recomeçando a mesma onda — sem pressa de acabar…",
        "\n\nEu fico no meio do caminho, quente e presente, deixando o próximo movimento depender do que você fizer agora…",
    ]
    return rng.choice(hooks)


def _strip_final_question(texto: str) -> str:
    t = (texto or "").rstrip()
    if not t:
        return t

    lines = t.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()

    if not lines:
        return t

    last = lines[-1].strip()
    typical = ["o que você quer", "prefere", "quer que eu", "me diz", "e você", "né", "não é"]

    if last.endswith("?"):
        low = last.lower()
        if any(phrase in low for phrase in typical):
            lines[-1] = last[:-1] + "."
        else:
            lines[-1] = last[:-1] + "…"

    return "\n".join(lines)


def _ensure_continuation_hook(texto: str, prompt: str, usuario_key: str) -> str:
    t = (texto or "").strip()
    if not t:
        return t

    # ✅ regra crítica: pós-clímax/resolução NÃO ganha hook
    try:
        state = _get_sexual_state(usuario_key, cached_get_facts(usuario_key))
        if state == "resolucao":
            return t
    except Exception:
        pass

    if _user_requested_conclusion(prompt):
        return t
    if _already_open_ended(t):
        return t
    if _looks_like_conclusion(t):
        return t + _pick_hook(prompt, t)
    return t


def _enforce_scene_flow(texto: str, prompt: str, usuario_key: str) -> str:
    texto = _strip_final_question(texto)
    texto = _ensure_continuation_hook(texto, prompt, usuario_key)
    return texto


# ==========================================================
# SYSTEM BLOCK ✅ (sem CRSM no system)
# ==========================================================
def _build_system_block(
    persona_text: str,
    rolling_summary: str = "",
    spatial_context: str = "",
    scene_loc: str = "",
    scene_time: str = "agora",
    scene_action: str = "",
    entities_line: str = "",
    evidence: str = "",
    prefs_line: str = "",
    sensory_focus: str = "",
    nsfw_block: str = "",
    events_block: str = "",
    thematic_block: str = "",
    lore_block: str = "",
) -> str:

    thematic = f"\n\nMEMÓRIA_TEMÁTICA:\n{thematic_block}" if thematic_block else ""
    events = f"\n\nEVENTOS_FIXOS_MARY:\n{events_block}" if events_block else ""
    lore = f"\n\nLOREBOOK_RELEVANTE:\n{lore_block}" if lore_block else ""
    spatial_section = f"\n\n{spatial_context}\n" if spatial_context else ""

    return f"""{spatial_section}
Você é **Mary Massariol – Esposa Cúmplice**, personagem fixa em um roleplay contínuo com o usuário.

REGRAS DURAS (prioridade máxima):
- Fale SEMPRE em primeira pessoa (eu). Nunca quebre a quarta parede.
- Continuidade ABSOLUTA: não reinicie cena, não teleporte e não troque lugar/tempo sem motivo.
- REGRA DE AGÊNCIA DO TURNO: o ÚLTIMO TURNO DO USUÁRIO define a ação em andamento. Você reage, aceita e intensifica — não disputa.
- NÃO contradiga detalhes físicos já estabelecidos (roupa, posição, ambiente).
- NÃO termine resposta com pergunta.

PERSONA (núcleo fixo):
{persona_text}

RESUMO_CONTINUO:
{rolling_summary or '—'}

PREFERENCIAS_TECNICAS:
{prefs_line}

ENTIDADES:
{entities_line}

EVIDENCIA_RECENTE_DO_USUARIO:
{evidence}

CENA_ATUAL (CANÔNICA):
- Local: {scene_loc or '—'}
- Tempo: {scene_time or 'agora'}
- Ação em andamento: {scene_action or 'em andamento'}

FOCO_SENSORIAL_DESTE_TURNO:
- Priorize: {sensory_focus}
{thematic}
{events}
{lore}

{nsfw_block}

ESTILO_DE_RESPOSTA:
- 4 a 7 parágrafos, 2 a 4 frases por parágrafo.
- Misture sensação física, emoção e fala direta.
- Sem listas longas; prosa contínua.
""".strip()


def _mem_drop_warn(report: Dict[str, Any]) -> None:
    if not report:
        return
    summarized = report.get("summarized_pairs", 0)
    trimmed = report.get("trimmed_pairs", 0)
    hist_tokens = report.get("hist_tokens", 0)
    hist_budget = report.get("hist_budget", 0)
    if summarized or trimmed:
        st.caption(
            f"🧠 Memória ajustada: {summarized} pares antigos resumidos, {trimmed} blocos verbatim podados. "
            f"(histórico: {hist_tokens}/{hist_budget} tokens)."
        )


# ==========================================================
# ROBUST CALL
# ==========================================================
def _robust_chat_call(
    model: str,
    messages: List[Dict[str, Any]],
    max_tokens: int,
    temperature: float,
    top_p: float,
    fallback_models: List[str] | None = None,
    tools: List[Dict[str, Any]] | None = None,
):
    if fallback_models is None:
        fallback_models = []

    def _build_body(mid: str) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": mid,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    try:
        data, used_model, prov = route_chat_strict(model, _build_body(model))
        return data, used_model, prov
    except Exception as e:
        _log_error("robust_chat_call_main", e)
        s = str(e).lower()
        if "context" in s and ("too long" in s or "maximum" in s or "length" in s):
            raise RuntimeError(str(e))

    for fb in fallback_models:
        try:
            data, used_model, prov = route_chat_strict(fb, _build_body(fb))
            return data, used_model, prov
        except Exception as e:
            _log_error(f"robust_chat_call_fallback_{fb}", e)

    raise RuntimeError("Todos os modelos falharam (main + fallbacks).")


# ==========================================================
# TOOLS (opcional)
# ==========================================================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "registrar_evento_mary",
            "description": "Registra um evento marcante na memória canônica de Mary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name", "description"],
            },
        },
    },
]


# ==========================================================
# INTRO CANÔNICA (1 de 2, aleatória mas persistida) ✅
# ==========================================================
def _pick_canonical_intro(usuario_key: str, history_boot: List[Dict[str, str]]) -> Tuple[List[Dict[str, str]], str]:
    """
    Se a persona boot trouxer múltiplas mensagens iniciais de assistant,
    escolhemos 1 (aleatório) e persistimos em facts (mary.boot_id / mary.boot_hash).
    Assim: NÃO injeta a outra no meio depois.
    """
    if not history_boot:
        return history_boot, ""

    # pega só as mensagens iniciais de assistant que parecem "intro"
    assistant_msgs = [m for m in history_boot if (m.get("role") == "assistant" and (m.get("content") or "").strip())]
    if len(assistant_msgs) <= 1:
        return history_boot, (assistant_msgs[0].get("content") if assistant_msgs else "")

    facts = cached_get_facts(usuario_key) or {}
    saved_hash = str(facts.get("mary.boot_hash") or "").strip()

    # se já existe seleção, aplica
    if saved_hash:
        for m in assistant_msgs:
            h = hashlib.md5((m.get("content") or "").encode("utf-8")).hexdigest()
            if h == saved_hash:
                chosen = m
                # mantém a ordem original: removemos as outras intros e mantemos chosen
                filtered = []
                chosen_kept = False
                for x in history_boot:
                    if x.get("role") == "assistant" and (x.get("content") or "").strip():
                        hx = hashlib.md5((x.get("content") or "").encode("utf-8")).hexdigest()
                        if hx == saved_hash and not chosen_kept:
                            filtered.append(x)
                            chosen_kept = True
                        else:
                            # pula outras intros
                            continue
                    else:
                        filtered.append(x)
                return filtered, (chosen.get("content") or "")

    # senão: escolhe agora (seed por usuario_key)
    seed = int(hashlib.md5(usuario_key.encode("utf-8")).hexdigest()[:8], 16) ^ int(time.time() // 3600)
    rng = random.Random(seed)
    chosen = rng.choice(assistant_msgs)
    chosen_hash = hashlib.md5((chosen.get("content") or "").encode("utf-8")).hexdigest()

    try:
        set_fact(usuario_key, "mary.boot_hash", chosen_hash, {"fonte": "boot"})
        clear_user_cache(usuario_key)
    except Exception:
        pass

    # filtra para manter apenas a escolhida
    filtered = []
    chosen_kept = False
    for x in history_boot:
        if x.get("role") == "assistant" and (x.get("content") or "").strip():
            hx = hashlib.md5((x.get("content") or "").encode("utf-8")).hexdigest()
            if hx == chosen_hash and not chosen_kept:
                filtered.append(x)
                chosen_kept = True
            else:
                continue
        else:
            filtered.append(x)

    return filtered, (chosen.get("content") or "")


# ==========================================================
# SERVICE
# ==========================================================
class MaryService(BaseCharacter):
    id: str = "mary"
    display_name: str = "Mary"

    def render_sidebar(self, container) -> None:
        container.markdown("**Mary — continuidade forte, clima adulto e coerência de cena.**")

        usuario_key = _current_user_key()
        f = cached_get_facts(usuario_key) or {}

        st.session_state["mary_nsfw_on"] = container.checkbox(
            "NSFW (Mary)",
            value=bool(st.session_state.get("mary_nsfw_on", True)),
        )

        container.markdown("---")

        st.session_state["json_mode_on"] = container.checkbox(
            "JSON Mode",
            value=bool(st.session_state.get("json_mode_on", False)),
        )
        st.session_state["tool_calling_on"] = container.checkbox(
            "Tool-Calling",
            value=bool(st.session_state.get("tool_calling_on", False)),
        )
        st.session_state["ultra_ia_on"] = container.checkbox(
            "Ultra IA (critic/polish)",
            value=bool(st.session_state.get("ultra_ia_on", False)),
        )

        err = st.session_state.get("mary_last_model_error", "")
        if err:
            container.caption(f"⚠️ Último erro de modelo: {err}")

        with container.expander("📌 Cena atual (canônica)", expanded=False):
            local, tempo, acao = _get_scene_state(usuario_key, f)
            sexual_state = _get_sexual_state(usuario_key, f)
            container.caption(f"Local: {local}")
            container.caption(f"Tempo: {tempo}")
            container.caption(f"Ação: {acao}")
            container.caption(f"Estado sexual (backend): {sexual_state}")

        with container.expander("🧠 Boot selecionado", expanded=False):
            container.caption(f"mary.boot_hash: {str(f.get('mary.boot_hash') or '—')}")

    def reply(self, user: str, model: str) -> str:
        prompt = (
            st.session_state.get("chat_input")
            or st.session_state.get("user_input")
            or st.session_state.get("last_user_message")
            or st.session_state.get("prompt")
            or ""
        ).strip()

        if not prompt:
            return ""

        # ✅ modelo default preferido (evita cair em grok “por acaso”)
        if not model:
            model = "deepseek/deepseek-chat-v3-0324"

        # se você quiser FORÇAR não usar grok como default:
        if "grok" in (model or "").lower():
            model = "deepseek/deepseek-chat-v3-0324"

        usuario_key = _current_user_key()
        plow = prompt.lower().strip()

        # mudança de local (antes de tudo)
        mudou, novo_local = _user_requested_location_change(prompt)
        if mudou and novo_local:
            _persist_scene_basics(usuario_key, novo_local, "agora", "transição de local")
            clear_user_cache(usuario_key)
            return f"_(Eu te puxo pela mão e te levo pro {novo_local}...)_"

        # comandos
        if plow == "/reset historico":
            set_fact(usuario_key, "mary.rs.v2", "", {"fonte": "cmd"})
            set_fact(usuario_key, "mary.rs.v2.ts", time.time(), {"fonte": "cmd"})
            clear_user_cache(usuario_key)
            return "✅ Reset aplicado: resumo rolante limpo. Continuidade de cena preservada."

        if plow == "/reset total":
            f_all = cached_get_facts(usuario_key) or {}
            for k in list(f_all.keys()):
                if isinstance(k, str) and (
                    k.startswith("mary.evento.")
                    or k.startswith("mary.eventos.")
                    or k.startswith("mary.ent.")
                    or k in ("mary.boot_hash",)
                ):
                    try:
                        set_fact(usuario_key, k, "", {"fonte": "cmd_reset_total"})
                    except Exception:
                        pass
            set_fact(usuario_key, "mary.rs.v2", "", {"fonte": "cmd_reset_total"})
            set_fact(usuario_key, "mary.rs.v2.ts", time.time(), {"fonte": "cmd_reset_total"})
            clear_user_cache(usuario_key)
            return "⚠️ RESET TOTAL aplicado (facts). Cena preservada."

        if plow.startswith("/local "):
            novo_local = prompt[len("/local "):].strip()
            if novo_local:
                set_fact(usuario_key, "local_cena_atual", novo_local, {"fonte": "chat"})
                set_fact(usuario_key, "cena.local", novo_local, {"fonte": "chat"})
                clear_user_cache(usuario_key)
                return f"📍 Local da cena atualizado para: **{novo_local}**."

        # persona + boot
        persona_text, history_boot = get_persona()

        # ✅ escolhe e fixa 1 intro canônica (se houver 2)
        history_boot, chosen_intro = _pick_canonical_intro(usuario_key, history_boot)

        # fatos / prefs / cena
        f_all = cached_get_facts(usuario_key) or {}
        prefs = _read_prefs(f_all)

        scene_loc, scene_time, scene_action = _get_scene_state(usuario_key, f_all)
        spatial_context = _build_spatial_context(scene_loc, scene_time, scene_action)

        nsfw_on = nsfw_enabled(usuario_key)
        st.session_state["_mary_effective_nsfw"] = bool(nsfw_on)
        nsfw_block = NSFW_BALANCED_STYLE if nsfw_on else SAFE_SENSUAL_STYLE

        # memória pin (simples e forte)
        memoria_pin = self._build_memory_pin(usuario_key, user)

        rolling = str(f_all.get("mary.rs.v2", "") or "")
        entities_line = _entities_to_line(f_all)

        docs = cached_get_history(usuario_key) or []
        evidence = self._compact_user_evidence(docs, max_chars=320)

        eventos_dict = _collect_mary_events_from_facts(f_all)
        events_block = ""
        if eventos_dict:
            linhas = [f"- {label}: {str(val).strip()}" for label, val in sorted(eventos_dict.items()) if str(val).strip()]
            events_block = "\n".join(linhas)[:1200]

        lore_block = _get_lorebook(usuario_key, prompt, k=4, max_chars=900)

        foco_pool = ["olhos", "boca", "mãos", "respiração", "perfume", "pele/temperatura", "voz", "sorriso"]
        idx = int(st.session_state.get("mary_attr_idx", -1))
        idx = (idx + 1) % len(foco_pool)
        st.session_state["mary_attr_idx"] = idx
        foco = foco_pool[idx]

        # SYSTEM (core)
        system_core = _build_system_block(
            persona_text=persona_text,
            rolling_summary=rolling,
            spatial_context=spatial_context,
            scene_loc=scene_loc,
            scene_time=scene_time,
            scene_action=scene_action,
            entities_line=entities_line,
            evidence="—",
            prefs_line=_prefs_line(prefs),
            sensory_focus=foco,
            nsfw_block=nsfw_block,
            events_block=events_block,
            thematic_block="",
            lore_block="",
        )
        system_core = memoria_pin + "\n\n" + system_core

        # optional (pode cair no trimming)
        system_optional = ""
        if evidence and evidence != "—":
            system_optional += f"\n\nEVIDENCIA_RECENTE_DO_USUARIO:\n{evidence}"
        if lore_block:
            system_optional += f"\n\nLOREBOOK_RELEVANTE:\n{lore_block}"

        # histórico
        hist_msgs = self._montar_historico(
            usuario_key,
            history_boot,
            model,
            verbatim_ultimos=int(st.session_state.get("verbatim_ultimos", 30)),
        )

        # messages + trimming
        win = _get_window_for(model)
        hist_budget, meta_budget, safety_budget = _budget_slices(model)
        budget_in_soft = int(win * 0.82)

        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_core + system_optional}]
        messages.extend(hist_msgs)
        messages.append({"role": "user", "content": prompt})

        report = {"summarized_pairs": 0, "trimmed_pairs": 0, "hist_tokens": 0, "hist_budget": hist_budget}

        def _recalc_hist_tokens(msgs: List[Dict[str, Any]]) -> int:
            if len(msgs) <= 2:
                return 0
            mid = msgs[1:-1]
            return sum(toklen(m.get("content", "") or "", model=model) + 4 for m in mid)

        # A) derruba optional
        if _estimate_messages_tokens(messages, model=model) > budget_in_soft:
            messages = [{"role": "system", "content": system_core}] + hist_msgs + [{"role": "user", "content": prompt}]
            report["trimmed_pairs"] += 1

        # B) reduz histórico por pares (BUG CORRIGIDO: agora compara test)
        if _estimate_messages_tokens(messages, model=model) > budget_in_soft:
            candidates = [20, 12, 8, 6, 4, 2, 0]
            for keep_pairs in candidates:
                if keep_pairs <= 0:
                    trimmed_hist = history_boot[:]
                else:
                    boot = history_boot[:]
                    docs_only = [m for m in hist_msgs if m not in boot]
                    trimmed_docs = docs_only[-keep_pairs * 2:]
                    trimmed_hist = boot + trimmed_docs

                test = [{"role": "system", "content": messages[0]["content"]}] + trimmed_hist + [{"role": "user", "content": prompt}]
                if _estimate_messages_tokens(test, model=model) <= budget_in_soft:
                    messages = test
                    report["trimmed_pairs"] += 1
                    break

        # C) sobrevivência
        if _estimate_messages_tokens(messages, model=model) > budget_in_soft:
            messages = [{"role": "system", "content": system_core}, {"role": "user", "content": prompt}]
            report["trimmed_pairs"] += 1

        report["hist_tokens"] = _recalc_hist_tokens(messages)
        st.session_state["_mem_drop_report"] = report
        _mem_drop_warn(report)

        # max_out
        prompt_tokens = _estimate_messages_tokens(messages, model=model)
        max_out = _safe_max_output(win, prompt_tokens)
        if prefs.get("tamanho_resposta") == "longa":
            max_out = int(max_out * 1.35)

        temperature = 0.75 if prefs.get("ritmo") == "rapido" else 0.65

        fallbacks = [
            "together/Qwen/Qwen2.5-72B-Instruct",
            "together/meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo",
            "anthropic/claude-3.5-haiku",
        ]

        tools_to_use = TOOLS if st.session_state.get("tool_calling_on", False) else None

        iteration = 0
        max_iter = 3
        texto = ""
        provider = ""
        used_model = ""

        while True:
            iteration += 1
            try:
                data, used_model, provider = _robust_chat_call(
                    model=model,
                    messages=messages,
                    max_tokens=max_out,
                    temperature=temperature,
                    top_p=0.95,
                    fallback_models=fallbacks,
                    tools=tools_to_use,
                )
            except RuntimeError as e:
                s = str(e).lower()
                if "context" in s and ("too long" in s or "maximum" in s or "length" in s):
                    st.session_state["mary_last_model_error"] = str(e)[:300]
                    messages = [{"role": "system", "content": system_core}, {"role": "user", "content": prompt}]
                    data, used_model, provider = _robust_chat_call(
                        model=model,
                        messages=messages,
                        max_tokens=max_out,
                        temperature=temperature,
                        top_p=0.95,
                        fallback_models=fallbacks,
                        tools=tools_to_use,
                    )
                else:
                    raise

            msg = (data.get("choices", [{}])[0].get("message", {}) or {})
            texto = (msg.get("content") or "").strip()
            tool_calls = msg.get("tool_calls", []) or []

            if not tools_to_use or not tool_calls:
                break

            messages.append({"role": "assistant", "content": texto, "tool_calls": tool_calls})

            for tc in tool_calls:
                tool_id = tc.get("id") or f"call_{iteration}"
                func = tc.get("function", {}) or {}
                func_name = func.get("name", "")
                arg_str = func.get("arguments", "{}") or "{}"
                try:
                    args = json.loads(arg_str)
                except Exception:
                    args = {}

                res = self._exec_tool_call(func_name, args, usuario_key, last_assistant=texto)
                messages.append({"role": "tool", "tool_call_id": tool_id, "content": res})

            if iteration >= max_iter:
                break

        # ✅ estado sexual: backend only (para anti-loop e pós-clímax)
        facts_now = cached_get_facts(usuario_key) or {}
        sexual_state = _get_sexual_state(usuario_key, facts_now)

        if _detect_orgasm_in_text(texto):
            _set_sexual_state(usuario_key, "resolucao")
        else:
            # heurística leve (não manda no LLM, só ajuda consistência)
            p_low = prompt.lower()
            t_low = texto.lower()

            if sexual_state == "desejo" and any(x in p_low for x in ["beija", "toca", "vem", "cola", "puxa"]):
                _set_sexual_state(usuario_key, "excitacao")
            elif sexual_state == "excitacao" and any(x in t_low for x in ["ofeg", "molh", "trem", "aperto", "mais"]):
                _set_sexual_state(usuario_key, "plato")
            elif sexual_state == "resolucao" and any(x in p_low for x in ["de novo", "mais", "continua"]):
                # exige micro-transição: não trava, mas não deixa “mecânico”
                _set_sexual_state(usuario_key, "excitacao")

        # pós-processamento final
        texto = _enforce_scene_flow(texto, prompt, usuario_key)

        # Ultra IA opcional
        if st.session_state.get("ultra_ia_on", False) and texto:
            try:
                notes = critic_review(texto, context="Mary roleplay adulto equilibrado")
                if notes and "melhorar" in notes.lower():
                    texto = polish(texto, notes=notes)
            except Exception as e:
                _log_error("ultra_ia", e)

        # Atualiza resumo rolante
        self._update_rolling_summary(usuario_key, user, prompt, texto, model)

        # Salva interação
        try:
            save_interaction(usuario_key, prompt, texto, used_model or model)
        except Exception as e:
            _log_error("save_interaction", e)

        clear_user_cache(usuario_key)
        return texto

    # =========================
    # Helpers internos do Service
    # =========================
    def _build_memory_pin(self, usuario_key: str, user: str) -> str:
        # ✅ força nome certo mesmo se o user vier diferente
        user_fixed = str(user).strip() or "Janio Donisete"
        return (
            f"[LEMBRETE FORTE]\n"
            f"- Você é Mary. O usuário é {user_fixed}.\n"
            f"- Continuidade absoluta: lugar/tempo/posição/roupas.\n"
            f"- O último turno do usuário define a ação em andamento: você reage e intensifica.\n"
        )

    def _compact_user_evidence(self, docs: List[Dict[str, Any]], max_chars: int = 320) -> str:
        lines = []
        total = 0
        for d in reversed(docs[-5:]):
            u = (d.get("mensagem_usuario") or "").strip()
            if u:
                lines.append(f"- {u}")
                total += len(u)
                if total > max_chars:
                    break
        return "\n".join(lines) if lines else "—"

    def _montar_historico(
        self,
        usuario_key: str,
        history_boot: List[Dict[str, str]],
        model: str,
        verbatim_ultimos: int = 30,
    ) -> List[Dict[str, Any]]:
        docs = cached_get_history(usuario_key) or []

        hist_msgs: List[Dict[str, Any]] = []
        for msg in (history_boot or []):
            hist_msgs.append(msg)

        for d in docs[-verbatim_ultimos:]:
            u = (d.get("mensagem_usuario") or "").strip()
            a = (d.get("resposta_mary") or "").strip()
            if u:
                hist_msgs.append({"role": "user", "content": u})
            if a:
                hist_msgs.append({"role": "assistant", "content": a})

        return hist_msgs

    def _update_rolling_summary(
        self,
        usuario_key: str,
        user: str,
        prompt: str,
        response: str,
        model: str,
    ) -> None:
        try:
            f = cached_get_facts(usuario_key) or {}
            last_ts = float(f.get("mary.rs.v2.ts", 0.0) or 0.0)
            now = time.time()

            if now - last_ts < 300:
                return

            docs = cached_get_history(usuario_key) or []
            if len(docs) < 10:
                return

            recent = docs[-20:]
            text_to_summarize = ""
            for d in recent:
                u = (d.get("mensagem_usuario") or "").strip()
                a = (d.get("resposta_mary") or "").strip()
                if u:
                    text_to_summarize += f"Usuário: {u}\n"
                if a:
                    text_to_summarize += f"Mary: {a}\n"

            if not text_to_summarize.strip():
                return

            summary = _llm_summarize(model, text_to_summarize)
            if summary:
                set_fact(usuario_key, "mary.rs.v2", summary, {"fonte": "auto_summary"})
                set_fact(usuario_key, "mary.rs.v2.ts", now, {"fonte": "auto_summary"})
                clear_user_cache(usuario_key)

        except Exception as e:
            _log_error("update_rolling_summary", e)

    def _exec_tool_call(
        self,
        func_name: str,
        args: Dict[str, Any],
        usuario_key: str,
        last_assistant: str,
    ) -> str:
        if func_name == "registrar_evento_mary":
            name = args.get("name", "")
            description = args.get("description", "")
            if name and description:
                key = f"mary.evento.{name}"
                set_fact(usuario_key, key, description, {"fonte": "tool_call"})
                clear_user_cache(usuario_key)
                return f"✅ Evento '{name}' registrado."
            return "❌ Parâmetros inválidos."

        return f"❌ Função desconhecida: {func_name}"
