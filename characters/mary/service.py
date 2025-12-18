from __future__ import annotations

"""
MaryService (v5.1 - Router Compat + Erro Visível)
- Mantém tua lógica de continuidade / boot canônico / trimming
- ✅ Corrige possível quebra por mudança de assinatura do route_chat_strict
- ✅ Guarda erro do modelo em st.session_state["mary_last_model_error"]
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
# ROUTER COMPAT ✅ (suporta 2 assinaturas)
# ==========================================================
def _route_chat_strict_compat(model_id: str, body: Dict[str, Any]):
    """
    Alguns projetos mudam o route_chat_strict para:
      - route_chat_strict(model_id, body) -> (data, used_model, provider)
      OU
      - route_chat_strict(body) -> (data, used_model, provider)
    Aqui tentamos os dois.
    """
    try:
        return route_chat_strict(model_id, body)
    except TypeError:
        return route_chat_strict(body)


# ==========================================================
# TOKENS (tiktoken opcional)
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

    chars_est = int(len(t) / 4.0)
    words_est = int(len(t.split()) * 1.3)
    return max(1, max(chars_est, words_est))


# ==========================================================
# ESTILO (sem mexer no “tom” do teu app)
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
- Seja intensa, provocante e coerente com a ação em andamento.
- Não teleporte nem reinicie cena.
- Progressão natural; pós-clímax com pausa.
""".strip()


# ==========================================================
# USER KEY
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
        if k in st.session_state:
            del st.session_state[k]


def nsfw_enabled(usuario_key: str) -> bool:
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
# ERROS (guarda no session_state)
# ==========================================================
def _log_error(context: str, exc: Exception) -> None:
    msg = f"[MaryService][{context}] {type(exc).__name__}: {exc}"
    try:
        logger.exception(msg)
    except Exception:
        pass
    st.session_state["mary_last_model_error"] = msg[:400]


# ==========================================================
# PREFERÊNCIAS
# ==========================================================
def _read_prefs(facts: Dict[str, Any]) -> Dict[str, str]:
    ritmo = facts.get("mary.pref.ritmo") or "rapido"
    tamanho = facts.get("mary.pref.tamanho") or "longa"
    return {"ritmo": str(ritmo), "tamanho_resposta": str(tamanho)}


def _prefs_line(prefs: Dict[str, str]) -> str:
    return f"ritmo={prefs.get('ritmo')}; tamanho_resposta={prefs.get('tamanho_resposta')}"


# ==========================================================
# CENA
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
# ESTADO SEXUAL (backend only)
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
# SUMMARIZER (mantido)
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
        data, _, _ = _route_chat_strict_compat(use_model, body)
        msg = (data.get("choices", [{}])[0].get("message", {}) or {})
        return (msg.get("content") or "").strip()
    except Exception:
        return ""


# ==========================================================
# CONTINUIDADE FINAL (mantido)
# ==========================================================
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
    if last.endswith("?"):
        lines[-1] = last[:-1] + "…"
    return "\n".join(lines)


def _enforce_scene_flow(texto: str, prompt: str, usuario_key: str) -> str:
    return _strip_final_question(texto)


# ==========================================================
# SYSTEM BLOCK (mantido)
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
) -> str:
    spatial_section = f"\n\n{spatial_context}\n" if spatial_context else ""
    return f"""{spatial_section}
Você é **Mary Massariol – Esposa Cúmplice**, personagem fixa em um roleplay contínuo com o usuário.

REGRAS DURAS (prioridade máxima):
- Fale SEMPRE em primeira pessoa (eu). Nunca quebre a quarta parede.
- Continuidade ABSOLUTA: não reinicie cena, não teleporte e não troque lugar/tempo sem motivo.
- REGRA DE AGÊNCIA DO TURNO: o ÚLTIMO TURNO DO USUÁRIO define a ação em andamento. Você reage, aceita e intensifica.
- NÃO contradiga detalhes já estabelecidos (roupa, posição, ambiente).
- NÃO termine resposta com pergunta.

PERSONA (núcleo fixo):
{persona_text}

RESUMO_CONTINUO:
{rolling_summary or '—'}

PREFERENCIAS_TECNICAS:
{prefs_line}

ENTIDADES:
{entities_line}

CENA_ATUAL (CANÔNICA):
- Local: {scene_loc or '—'}
- Tempo: {scene_time or 'agora'}
- Ação em andamento: {scene_action or 'em andamento'}

FOCO_SENSORIAL_DESTE_TURNO:
- Priorize: {sensory_focus}

{nsfw_block}

ESTILO_DE_RESPOSTA:
- 4 a 7 parágrafos, 2 a 4 frases por parágrafo.
- Prosa contínua, sem listas longas.
""".strip()


# ==========================================================
# ROBUST CALL (agora usa compat)
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
        body = _build_body(model)
        data, used_model, prov = _route_chat_strict_compat(model, body)
        return data, used_model, prov
    except Exception as e:
        _log_error("robust_chat_call_main", e)

    for fb in fallback_models:
        try:
            body = _build_body(fb)
            data, used_model, prov = _route_chat_strict_compat(fb, body)
            return data, used_model, prov
        except Exception as e:
            _log_error(f"robust_chat_call_fallback_{fb}", e)

    raise RuntimeError("Todos os modelos falharam (main + fallbacks).")


# ==========================================================
# TOOLS (mantido)
# ==========================================================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "registrar_evento_mary",
            "description": "Registra um evento marcante na memória canônica de Mary.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
                "required": ["name", "description"],
            },
        },
    },
]


# ==========================================================
# INTRO CANÔNICA (mantido)
# ==========================================================
def _pick_canonical_intro(usuario_key: str, history_boot: List[Dict[str, str]]) -> Tuple[List[Dict[str, str]], str]:
    if not history_boot:
        return history_boot, ""

    assistant_msgs = [m for m in history_boot if (m.get("role") == "assistant" and (m.get("content") or "").strip())]
    if len(assistant_msgs) <= 1:
        return history_boot, (assistant_msgs[0].get("content") if assistant_msgs else "")

    facts = cached_get_facts(usuario_key) or {}
    saved_hash = str(facts.get("mary.boot_hash") or "").strip()

    if saved_hash:
        for m in assistant_msgs:
            h = hashlib.md5((m.get("content") or "").encode("utf-8")).hexdigest()
            if h == saved_hash:
                chosen = m
                filtered = []
                chosen_kept = False
                for x in history_boot:
                    if x.get("role") == "assistant" and (x.get("content") or "").strip():
                        hx = hashlib.md5((x.get("content") or "").encode("utf-8")).hexdigest()
                        if hx == saved_hash and not chosen_kept:
                            filtered.append(x)
                            chosen_kept = True
                        else:
                            continue
                    else:
                        filtered.append(x)
                return filtered, (chosen.get("content") or "")

    seed = int(hashlib.md5(usuario_key.encode("utf-8")).hexdigest()[:8], 16) ^ int(time.time() // 3600)
    rng = random.Random(seed)
    chosen = rng.choice(assistant_msgs)
    chosen_hash = hashlib.md5((chosen.get("content") or "").encode("utf-8")).hexdigest()

    try:
        set_fact(usuario_key, "mary.boot_hash", chosen_hash, {"fonte": "boot"})
        clear_user_cache(usuario_key)
    except Exception:
        pass

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

        if not model:
            model = "deepseek/deepseek-chat-v3-0324"
        if "grok" in (model or "").lower():
            model = "deepseek/deepseek-chat-v3-0324"

        usuario_key = _current_user_key()
        plow = prompt.lower().strip()

        # comandos leves
        if plow == "/reset historico":
            set_fact(usuario_key, "mary.rs.v2", "", {"fonte": "cmd"})
            set_fact(usuario_key, "mary.rs.v2.ts", time.time(), {"fonte": "cmd"})
            clear_user_cache(usuario_key)
            return "✅ Reset aplicado: resumo rolante limpo. Continuidade de cena preservada."

        # persona + boot
        persona_text, history_boot = get_persona()
        history_boot, _ = _pick_canonical_intro(usuario_key, history_boot)

        f_all = cached_get_facts(usuario_key) or {}
        prefs = _read_prefs(f_all)

        scene_loc, scene_time, scene_action = _get_scene_state(usuario_key, f_all)
        spatial_context = _build_spatial_context(scene_loc, scene_time, scene_action)

        nsfw_on = nsfw_enabled(usuario_key)
        st.session_state["_mary_effective_nsfw"] = bool(nsfw_on)
        nsfw_block = NSFW_BALANCED_STYLE if nsfw_on else SAFE_SENSUAL_STYLE

        rolling = str(f_all.get("mary.rs.v2", "") or "")
        entities_line = "—"

        foco_pool = ["olhos", "boca", "mãos", "respiração", "perfume", "pele/temperatura", "voz", "sorriso"]
        idx = int(st.session_state.get("mary_attr_idx", -1))
        idx = (idx + 1) % len(foco_pool)
        st.session_state["mary_attr_idx"] = idx
        foco = foco_pool[idx]

        system_core = _build_system_block(
            persona_text=persona_text,
            rolling_summary=rolling,
            spatial_context=spatial_context,
            scene_loc=scene_loc,
            scene_time=scene_time,
            scene_action=scene_action,
            entities_line=entities_line,
            prefs_line=_prefs_line(prefs),
            sensory_focus=foco,
            nsfw_block=nsfw_block,
        )

        hist_msgs = self._montar_historico(
            usuario_key,
            history_boot,
            verbatim_ultimos=int(st.session_state.get("verbatim_ultimos", 30)),
        )

        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_core}]
        messages.extend(hist_msgs)
        messages.append({"role": "user", "content": prompt})

        win = _get_window_for(model)
        prompt_tokens = _estimate_messages_tokens(messages, model=model)
        max_out = _safe_max_output(win, prompt_tokens)
        if prefs.get("tamanho_resposta") == "longa":
            max_out = int(max_out * 1.25)

        temperature = 0.75 if prefs.get("ritmo") == "rapido" else 0.65

        fallbacks = [
            "together/Qwen/Qwen2.5-72B-Instruct",
            "together/meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo",
            "anthropic/claude-3.5-haiku",
        ]

        try:
            data, used_model, provider = _robust_chat_call(
                model=model,
                messages=messages,
                max_tokens=max_out,
                temperature=temperature,
                top_p=0.95,
                fallback_models=fallbacks,
                tools=None,
            )
        except Exception as e:
            _log_error("reply_call", e)
            return ""  # o mary_app.py agora mostra erro real se você preferir lançar exception

        msg = (data.get("choices", [{}])[0].get("message", {}) or {})
        texto = (msg.get("content") or "").strip()

        # estado sexual backend-only (mantido)
        facts_now = cached_get_facts(usuario_key) or {}
        sexual_state = _get_sexual_state(usuario_key, facts_now)
        if _detect_orgasm_in_text(texto):
            _set_sexual_state(usuario_key, "resolucao")

        texto = _enforce_scene_flow(texto, prompt, usuario_key)

        try:
            save_interaction(usuario_key, prompt, texto, used_model or model)
        except Exception as e:
            _log_error("save_interaction", e)

        clear_user_cache(usuario_key)
        return texto

    def _montar_historico(
        self,
        usuario_key: str,
        history_boot: List[Dict[str, str]],
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
