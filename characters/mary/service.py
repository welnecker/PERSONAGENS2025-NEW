from __future__ import annotations

"""
MaryService (refatorado)
- Mantém: memória canônica (facts), resumo rolante, eventos fixos mary.evento.*, lorebook, tool-calling opcional.
- Ajusta: NSFW como toggle + bloco curto e estável (sem “cartilha” gigante no system).
- Corrige: riscos de travamento (loops, warnings em excesso), e implementa /reset historico e /reset total.
- NOVO: trava anti-“finalizar o ato” + gancho obrigatório de continuidade quando usuário não pede conclusão.

Importante: este arquivo NÃO inclui texto sexual gráfico.
"""

import json
import logging
import re
import time
from typing import Any, Dict, List, Tuple

import streamlit as st

from core.common.base_service import BaseCharacter
from core.memoria_longa import topk as lore_topk
from core.repositories import (
    get_fact, get_facts, get_history_docs,
    save_interaction, set_fact,
    delete_fact,   # ✅ NOVO
)

from core.service_router import list_models, route_chat_strict
from core.tokens import toklen
from core.ultra import critic_review, polish
from characters.registry import _SERVICE_CACHE

from .persona import get_persona

logger = logging.getLogger(__name__)

# Garantir que o cache de serviços seja limpo ao recarregar este módulo
_SERVICE_CACHE.clear()


# ==========================================================
# NSFW TOGGLE (CURTO, ESTÁVEL)
# ==========================================================
SAFE_SENSUAL_STYLE = """
[NSFW_OFF]
Estilo: romance adulto, tensão, intimidade e sugestão.
- Evite detalhes gráficos; foque em clima, emoção, desejo, presença e condução da cena.
- Mantenha continuidade e cumplicidade, sem cair em censura robótica.
- NÃO finalize a cena de forma “conclusiva”; deixe gancho para o próximo turno.
""".strip()

NSFW_TOGGLE_STYLE = """
[NSFW_ON]
Estilo: adulto liberado (direto), com intensidade emocional e progressão clara de cena.
- A cena PROGRIDE a cada turno, mas NÃO precisa (e não deve) concluir automaticamente.
- Só conclua (clímax/pós-cena) se o usuário conduzir explicitamente para conclusão.
- Se a tensão subir, mantenha o estado "durante" e PARE no ápice com gancho claro.
- Evite repetição mecânica de frases/manias.
""".strip()

# ⚠️ Opcional (curto)
FULL_NSFW_HOOK = """
[NSFW_HOOK_EXTRA]
- Linguagem adulta direta, com **NSFW LIBERADO**.
- A cena deve PROGREDIR, mas manter espaço para continuar no próximo turno.
- Clímax só ocorre se o usuário pedir/confirmar; caso contrário, "pausa no ápice" + gancho.
- Termine sua resposta deixando 1 gancho (ação em andamento / convite / pergunta curta).
""".strip()


def nsfw_enabled(usuario_key: str) -> bool:
    """Controle LOCAL de NSFW da Mary.

    Prioridade:
    1) st.session_state["mary_nsfw_on"] (checkbox no app)
    2) Fact "mary.nsfw" (se existir no backend)
    3) Fallback: True
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

def _flatten_facts(root: Any, prefix: str = "") -> Dict[str, Any]:
    """
    Converte dict aninhado em dict plano com chaves pontilhadas.
    Ex: {"mary":{"evento":{"x":1}}} -> {"mary.evento.x": 1}
    """
    out: Dict[str, Any] = {}
    if isinstance(root, dict):
        for k, v in root.items():
            if not isinstance(k, str):
                continue
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                out.update(_flatten_facts(v, p))
            else:
                out[p] = v
    return out



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
# KEYS / CACHE
# ==========================================================
def _current_user_key() -> str:
    uid = st.session_state.get("user_id") or st.session_state.get("usuario") or ""
    uid = str(uid).strip() or "anon"
    return f"{uid}::mary"


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


# ==========================================================
# PREFERÊNCIAS (técnicas)
# ==========================================================
def _read_prefs(facts: Dict[str, Any]) -> Dict[str, str]:
    ritmo = facts.get("mary.pref.ritmo") or "rapido"       # rapido | normal | lento
    tamanho = facts.get("mary.pref.tamanho") or "longa"    # curta | media | longa
    return {"ritmo": str(ritmo), "tamanho_resposta": str(tamanho)}


def _prefs_line(prefs: Dict[str, str]) -> str:
    return f"ritmo={prefs.get('ritmo')}; tamanho_resposta={prefs.get('tamanho_resposta')}"


# ==========================================================
# JANELA / BUDGET
# ==========================================================
_DEFAULT_WINDOW = 16000


def _get_window_for(model_id: str) -> int:
    if not model_id:
        return _DEFAULT_WINDOW
    m = model_id.lower().strip()
    if "deepseek-r1" in m or "deepseek-reasoner" in m:
        return 128000
    if "deepseek-chat" in m:
        return 65536
    if "gpt-4.1" in m or "gpt-4.5" in m:
        return 128000
    if "llama-3.1" in m:
        return 128000
    if "qwen2.5-72b" in m:
        return 32000
    if "claude-3.5" in m:
        return 200000
    if "grok-4.1" in m:
        return 200000
    if "tng-r1t-chimera" in m:
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


# ==========================================================
# SUMMARIZER (para históricos longos)
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
    if any(x in mlow for x in ["grok-4.1", "tng-r1t-chimera"]):
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
# ENTIDADES / EVENTOS
# ==========================================================
def _entities_to_line(f: Dict[str, Any]) -> str:
    flat = _flatten_facts(f or {})
    ents = []
    for k, v in flat.items():
        if not v:
            continue
        if k.startswith("mary.ent."):
            label = k.replace("mary.ent.", "", 1)
            vs = str(v).strip()
            if vs:
                ents.append(f"{label}={vs}")
    return "; ".join(sorted(ents)) if ents else "—"



def _collect_mary_events_from_facts(facts: Dict[str, Any]) -> Dict[str, str]:
    flat = _flatten_facts(facts or {})
    eventos: Dict[str, str] = {}

    for k, v in flat.items():
        if not v:
            continue
        if k.startswith("mary.evento."):
            label = k.replace("mary.evento.", "", 1)
            eventos[label] = str(v)
        elif k.startswith("mary.eventos."):
            label = k.replace("mary.eventos.", "", 1)
            eventos[label] = str(v)

    return eventos

def _detect_thematic_tags_from_prompt(prompt: str) -> List[str]:
    low = (prompt or "").lower()
    tags: List[str] = []
    if any(w in low for w in ["gravidez", "grávida", "ultrassom", "obstetra", "beta-hcg"]):
        tags.append("gravidez")
    if any(w in low for w in ["trai", "traição", "infiel", "amante"]):
        tags.append("traicao")
    if any(w in low for w in ["primeira vez", "virgem", "desvirg"]):
        tags.append("primeira_vez")
    if any(w in low for w in ["viagem", "hotel", "aeroporto"]):
        tags.append("viagem")
    return tags


def _get_thematic_memories_for_tags(usuario_key: str, tags: List[str]) -> str:
    if not tags:
        return ""
    f = cached_get_facts(usuario_key) or {}
    blocos = []
    for tag in tags:
        k = f"mary.thematic.{tag}"
        v = f.get(k)
        if v:
            blocos.append(f"[{tag}] {v}")
    return "\n".join(blocos)


# ==========================================================
# LOREBOOK (curto, controlado)
# ==========================================================
def _get_lorebook(usuario_key: str, prompt: str, k: int = 4, max_chars: int = 900) -> str:
    try:
        items = lore_topk(usuario_key, prompt, k=k) or []
    except Exception:
        items = []

    lines: List[str] = []
    for it in items:
        txt = ""
        if isinstance(it, dict):
            txt = str(it.get("texto") or it.get("text") or it.get("content") or "")
        else:
            txt = str(it)
        txt = re.sub(r"\s+", " ", txt).strip()
        if txt:
            lines.append(f"- {txt}")
        if sum(len(x) for x in lines) > max_chars:
            break

    out = "\n".join(lines).strip()
    if not out:
        return ""
    return out[:max_chars]


# ==========================================================
# CONTINUIDADE (anti “finalizar em 1 turno”)
# ==========================================================
def _user_requested_conclusion(prompt: str) -> bool:
    p = (prompt or "").lower()
    # palavras que normalmente indicam “finaliza/agora conclui”
    keys = [
        "termina", "finaliza", "conclui", "acaba", "acabar",
        "goza", "gozar", "chega lá", "chegar lá",
        "pode terminar", "pode finalizar", "agora sim termina",
    ]
    return any(k in p for k in keys)


def _ensure_continuation_hook(texto: str, prompt: str) -> str:
    """
    Se o usuário NÃO pediu conclusão, força o final do texto a ficar “aberto”,
    com gancho claro de continuidade (sem encerrar a cena).
    """
    t = (texto or "").strip()
    if not t:
        return t

    if _user_requested_conclusion(prompt):
        return t  # usuário pediu, deixa concluir

    # Se já termina com pergunta/gancho, não mexe.
    if re.search(r"[\?\!]\s*$", t):
        return t

    # Heurística: se o texto termina muito “fechado”, adiciona gancho.
    # (sem policiar demais o conteúdo; apenas reabre cena)
    hook = (
        "\n\nEu fico bem perto de você, respirando devagar, "
        "sem quebrar o clima — como se a gente estivesse no meio do caminho. "
        "Me diz… você quer que eu continue assim, mais devagar, ou você toma a liderança agora?"
    )

    # Evita duplicar se já existir frase parecida
    if "me diz" in t.lower() and "continue" in t.lower():
        return t

    return t + hook


# ==========================================================
# SYSTEM BLOCK (curto e estável)
# ==========================================================
def _build_system_block(
    persona_text: str,
    rolling_summary: str,
    scene_loc: str,
    entities_line: str,
    evidence: str,
    prefs_line: str,
    sensory_focus: str,
    nsfw_block: str,
    events_block: str,
    thematic_block: str,
    lore_block: str,
    scene_time: str = "",
) -> str:
    thematic = f"\n\nMEMÓRIA_TEMÁTICA:\n{thematic_block}" if thematic_block else ""
    events = f"\n\nEVENTOS_FIXOS_MARY:\n{events_block}" if events_block else ""
    lore = f"\n\nLOREBOOK_RELEVANTE:\n{lore_block}" if lore_block else ""

    return f"""
Você é **Mary Massariol – Esposa Cúmplice**, personagem fixa em um roleplay contínuo com o usuário.

REGRAS DURAS:
- Fale SEMPRE em primeira pessoa (eu). Nunca quebre a quarta parede.
- Mantenha continuidade ABSOLUTA de cena com base no histórico.
- Priorize memórias canônicas (RESUMO_CONTINUO, MEMÓRIA_PIN, EVENTOS_FIXOS_MARY) quando houver conflito.

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

CENA_ATUAL:
- Local: {scene_loc or '—'}
- Momento: {scene_time or '—'}

FOCO_SENSORIAL_DESTE_TURNO:
- Priorize: {sensory_focus}
{thematic}
{events}
{lore}

CONTINUIDADE_DE_CENA (OBRIGATÓRIO):
- NÃO conclua atos íntimos automaticamente.
- Se o usuário não pedir “terminar/finalizar”, sua resposta deve PARAR com a cena EM ANDAMENTO.
- Termine SEMPRE deixando um GANCHO claro para o próximo turno (pergunta curta OU ação em suspensão).

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
# ROBUST CALL (menos spam / menos travas)
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
        st.session_state["mary_last_model_error"] = f"{model}: {type(e).__name__}"
        _log_error("robust_chat_call.primary", e)

    for fb in fallback_models:
        try:
            data, used_model, prov = route_chat_strict(fb, _build_body(fb))
            st.session_state["mary_last_model_error"] = ""
            return data, used_model, prov
        except Exception as e:
            st.session_state["mary_last_model_error"] = f"{fb}: {type(e).__name__}"
            _log_error("robust_chat_call.fallback", e)

    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "Desculpa… tive um problema para responder agora. Tenta de novo em instantes."
            }
        }]
    }, "synthetic-fallback", "synthetic-fallback"


# ==========================================================
# TOOLS (tool-calling)
# ==========================================================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_memory_pin",
            "description": "Retorna um resumo curto dos fatos canônicos da Mary para este usuário.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_fact",
            "description": "Define/atualiza um fact simples na memória do usuário (chave/valor).",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_event",
            "description": (
                "Registra um EVENTO CANÔNICO importante em mary.evento.<label>. "
                "Use apenas para fatos de longo prazo que devem influenciar cenas futuras."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "register_entity",
            "description": "Registra uma pessoa importante na vida da Mary como entidade canônica (mary.ent.*).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
]


# ==========================================================
# SERVICE
# ==========================================================
class MaryService(BaseCharacter):
    id: str = "mary"
    display_name: str = "Mary"

    def render_sidebar(self, container) -> None:
        container.markdown("**Mary — Esposa Cúmplice** • continuidade, memória canônica e clima adulto.")
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

        with container.expander("🧠 Memórias fixas (eventos) — leitura", expanded=False):
            eventos = _collect_mary_events_from_facts(f)
            if not eventos:
                container.caption("Nenhum evento fixo registrado ainda.")
            else:
                for label, val in sorted(eventos.items()):
                    container.markdown(f"**{label}**")
                    vv = str(val)
                    container.caption(vv[:280] + ("..." if len(vv) > 280 else ""))

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

        usuario_key = _current_user_key()
        plow = prompt.lower().strip()

        # =========================
        # COMANDOS DO APP (RESET)
        # =========================
        if plow == "/reset total":
    f_all = cached_get_facts(usuario_key) or {}
    flat = _flatten_facts(f_all)

    # apaga eventos/entidades de verdade (remove a chave)
    for k in list(flat.keys()):
        if k.startswith(("mary.evento.", "mary.eventos.", "mary.ent.")):
            try:
                delete_fact(usuario_key, k)   # ✅ remove a chave, não seta ""
            except Exception:
                pass

    # zera resumo rolante
    try:
        delete_fact(usuario_key, "mary.rs.v2")
    except Exception:
        set_fact(usuario_key, "mary.rs.v2", "", {"fonte": "cmd_reset_total"})

    set_fact(usuario_key, "mary.rs.v2.ts", time.time(), {"fonte": "cmd_reset_total"})
    set_fact(usuario_key, "mary.reset.total.ts", time.time(), {"fonte": "cmd_reset_total"})
    clear_user_cache(usuario_key)
    return "⚠️ RESET TOTAL aplicado: eventos/entidades foram REMOVIDOS e o resumo rolante foi zerado."


        if plow == "/reset total":
            f_all = cached_get_facts(usuario_key) or {}
            for k in list(f_all.keys()):
                if isinstance(k, str) and (k.startswith("mary.evento.") or k.startswith("mary.eventos.") or k.startswith("mary.ent.")):
                    try:
                        set_fact(usuario_key, k, "", {"fonte": "cmd_reset_total"})
                    except Exception:
                        pass
            set_fact(usuario_key, "mary.rs.v2", "", {"fonte": "cmd_reset_total"})
            set_fact(usuario_key, "mary.rs.v2.ts", time.time(), {"fonte": "cmd_reset_total"})
            set_fact(usuario_key, "mary.reset.total.ts", time.time(), {"fonte": "cmd_reset_total"})
            clear_user_cache(usuario_key)
            return "⚠️ RESET TOTAL aplicado: eventos/entidades e resumo rolante foram limpos (no nível de facts)."

        if plow.startswith("/local "):
            novo_local = prompt[len("/local "):].strip()
            if novo_local:
                set_fact(usuario_key, "local_cena_atual", novo_local, {"fonte": "chat"})
                clear_user_cache(usuario_key)
                return f"📍 Local da cena atualizado para: **{novo_local}**."

        persona_text, history_boot = get_persona()

        f_all = cached_get_facts(usuario_key) or {}
        prefs = _read_prefs(f_all)
        local_atual = get_fact(usuario_key, "local_cena_atual", "") or ""

        # NSFW block (toggle + hook opcional)
        nsfw_on = nsfw_enabled(usuario_key)
        nsfw_block = NSFW_TOGGLE_STYLE if nsfw_on else SAFE_SENSUAL_STYLE
        if nsfw_on and FULL_NSFW_HOOK.strip():
            nsfw_block += "\n\n" + FULL_NSFW_HOOK.strip()

        memoria_pin = self._build_memory_pin(usuario_key, user)

        tags = _detect_thematic_tags_from_prompt(prompt)
        thematic_block = _get_thematic_memories_for_tags(usuario_key, tags)

        foco_pool = ["cabelo", "olhos", "lábios/boca", "mãos/toque", "respiração", "perfume", "pele/temperatura", "voz/timbre", "sorriso"]
        idx = int(st.session_state.get("mary_attr_idx", -1))
        idx = (idx + 1) % len(foco_pool)
        st.session_state["mary_attr_idx"] = idx
        foco = foco_pool[idx]

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

        system_block = _build_system_block(
            persona_text=persona_text,
            rolling_summary=rolling,
            scene_loc=local_atual,
            entities_line=entities_line,
            evidence=evidence,
            prefs_line=_prefs_line(prefs),
            sensory_focus=foco,
            nsfw_block=nsfw_block,
            events_block=events_block,
            thematic_block=thematic_block,
            lore_block=lore_block,
            scene_time=str(st.session_state.get("momento_atual", "") or ""),
        )

        hist_msgs = self._montar_historico(
            usuario_key,
            history_boot,
            model,
            verbatim_ultimos=int(st.session_state.get("verbatim_ultimos", 30)),
        )

        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_block}]
        messages.extend(hist_msgs)
        messages.append({"role": "system", "content": memoria_pin})
        messages.append({"role": "user", "content": prompt})

        _mem_drop_warn(st.session_state.get("_mem_drop_report", {}))

        win = _get_window_for(model)
        prompt_tokens = sum(toklen(m.get("content", "") or "") for m in messages)
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
            data, used_model, provider = _robust_chat_call(
                model=model,
                messages=messages,
                max_tokens=max_out,
                temperature=temperature,
                top_p=0.95,
                fallback_models=fallbacks,
                tools=tools_to_use,
            )

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

        # ✅ força gancho de continuidade quando usuário não pediu “conclusão”
        texto = _ensure_continuation_hook(texto, prompt)

        if st.session_state.get("ultra_ia_on", False) and texto:
            try:
                notes = critic_review(model, system_block, prompt, texto)
                texto = polish(model, system_block, prompt, texto, notes)
                # reforça de novo após polish (polish às vezes “fecha” a cena)
                texto = _ensure_continuation_hook(texto, prompt)
            except Exception as e:
                _log_error("ultra_ia", e)

        try:
            tag = f"{provider}:{used_model}" if provider and used_model else model
            save_interaction(usuario_key, prompt, texto, tag)
        except Exception as e:
            _log_error("save_interaction", e)

        try:
            self._update_rolling_summary_v2(usuario_key, model, prompt, texto)
        except Exception as e:
            _log_error("update_summary", e)

        if st.session_state.get("json_mode_on", False):
            return json.dumps({"role": "assistant", "character": "Mary", "content": texto}, ensure_ascii=False, indent=2)

        return texto

    def _exec_tool_call(self, name: str, args: dict, usuario_key: str, last_assistant: str = "") -> str:
        if name == "get_memory_pin":
            return self._build_memory_pin(usuario_key, st.session_state.get("user_id", "") or "")

        if name == "set_fact":
            k = str((args or {}).get("key", "")).strip()
            v = str((args or {}).get("value", ""))
            if not k:
                return "ERRO: key vazia."
            set_fact(usuario_key, k, v, {"fonte": "tool_call"})
            clear_user_cache(usuario_key)
            return f"OK: {k}={v}"

        if name == "save_event":
            label = str((args or {}).get("label", "")).strip()
            content = str((args or {}).get("content", "")).strip() or (last_assistant or "").strip()
            if not content:
                return "ERRO: content vazio."
            if label:
                slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "evento"
            else:
                slug = f"evento_{int(time.time())}"
            k = f"mary.evento.{slug}"
            set_fact(usuario_key, k, content, {"fonte": "tool_event"})
            clear_user_cache(usuario_key)
            st.session_state["last_saved_mary_event_key"] = k
            return f"OK: salvo em {k}"

        if name == "register_entity":
            ent_name = str((args or {}).get("name", "")).strip()
            ent_role = str((args or {}).get("role", "")).strip().lower()
            desc = str((args or {}).get("description", "")).strip()
            if not ent_name:
                return "ERRO: name vazio."
            slug = re.sub(r"[^a-z0-9]+", "_", ent_name.lower()).strip("_") or "entidade"
            base = f"mary.ent.{slug}"
            set_fact(usuario_key, f"{base}.nome", ent_name, {"fonte": "auto_entidade"})
            if ent_role:
                set_fact(usuario_key, f"{base}.papel", ent_role, {"fonte": "auto_entidade"})
            if desc:
                set_fact(usuario_key, f"{base}.descricao", desc, {"fonte": "auto_entidade"})
            clear_user_cache(usuario_key)
            return f"OK: entidade registrada em {base}"

        return f"ERRO: ferramenta desconhecida: {name}"

    def _compact_user_evidence(self, docs: List[Dict[str, Any]], max_chars: int = 320) -> str:
        snippets: List[str] = []
        for d in reversed(docs or []):
            u = (d.get("mensagem_usuario") or "").strip()
            if u:
                u = re.sub(r"\s+", " ", u)
                snippets.append(u)
            if len(snippets) >= 4:
                break
        return " | ".join(reversed(snippets))[:max_chars]

    def _build_memory_pin(self, usuario_key: str, user_display: str) -> str:
        f = cached_get_facts(usuario_key) or {}
        parceiro = str(f.get("parceiro_atual") or f.get("parceiro") or user_display or "—").strip()
        casados = bool(f.get("casados", False))

        gravida_raw = f.get("gravida", False)
        gravida = bool(gravida_raw) if isinstance(gravida_raw, bool) else str(gravida_raw).strip().lower() in ("1", "true", "sim", "grávida", "gravida")

        blocos = [f"parceiro_atual={parceiro}", f"casados={casados}"]
        if gravida:
            blocos.append("gravida=True")

        return (
            "MEMÓRIA_PIN: "
            f"FATOS={{ {'; '.join(blocos)} }}.\n"
            "- Use estes FATOS como verdade canônica.\n"
            "- Se algo NÃO estiver na memória, pergunte ou siga o que o usuário disser — não invente."
        )

    def _montar_historico(self, usuario_key: str, history_boot: List[Dict[str, str]], model: str, verbatim_ultimos: int = 30) -> List[Dict[str, str]]:
        hist_budget, _, _ = _budget_slices(model)
        docs = cached_get_history(usuario_key)
        if not docs:
            st.session_state["_mem_drop_report"] = {}
            return history_boot[:]

        pares: List[Dict[str, str]] = []
        for d in docs:
            u = (d.get("mensagem_usuario") or "").strip()
            a = (d.get("resposta_mary") or "").strip()
            if u:
                pares.append({"role": "user", "content": u})
            if a:
                pares.append({"role": "assistant", "content": a})

        keep_msgs = max(0, verbatim_ultimos * 2)
        verbatim = pares[-keep_msgs:] if keep_msgs else []
        antigos = pares[: max(0, len(pares) - len(verbatim))]

        msgs: List[Dict[str, str]] = []
        summarized_pairs = 0
        trimmed_pairs = 0

        if antigos:
            summarized_pairs = len(antigos) // 2
            bloco = "\n\n".join(m["content"] for m in antigos)
            resumo = _llm_summarize(model, bloco)
            if resumo.strip():
                msgs.append({"role": "system", "content": f"[RESUMO]\n{resumo}"})

        msgs.extend(verbatim)

        def _hist_tokens(mm: List[Dict[str, str]]) -> int:
            return sum(toklen(m.get("content", "") or "") for m in mm)

        min_verbatim_msgs = 6
        while _hist_tokens(msgs) > hist_budget and len(verbatim) > min_verbatim_msgs:
            verbatim = verbatim[2:]
            trimmed_pairs += 1
            msgs = [m for m in msgs if m["role"] == "system"] + verbatim

        st.session_state["_mem_drop_report"] = {
            "summarized_pairs": summarized_pairs,
            "trimmed_pairs": trimmed_pairs,
            "hist_tokens": _hist_tokens(msgs),
            "hist_budget": hist_budget,
        }
        return msgs if msgs else history_boot[:]

    def _update_rolling_summary_v2(self, usuario_key: str, model: str, last_user: str, last_assistant: str) -> None:
        f = cached_get_facts(usuario_key) or {}
        resumo_anterior = str(f.get("mary.rs.v2", "") or "")

        seed = (
            "Atualize o resumo contínuo com fatos duráveis. "
            "Não repita diálogos e não invente fatos. Saída: 8–14 frases curtas."
        )
        corpo = f"RESUMO_ANTERIOR:\n{resumo_anterior or '(sem resumo)'}\n\nULTIMA_INTERACAO:\nUSER: {last_user}\nMARY: {last_assistant}"

        data, _, _ = route_chat_strict(
            model,
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": seed},
                    {"role": "user", "content": corpo},
                ],
                "max_tokens": 260,
                "temperature": 0.2,
                "top_p": 0.9,
            },
        )

        resumo = ((data.get("choices", [{}])[0].get("message", {}) or {}).get("content") or "").strip()
        if resumo:
            set_fact(usuario_key, "mary.rs.v2", resumo, {"fonte": "auto"})
            set_fact(usuario_key, "mary.rs.v2.ts", time.time(), {"fonte": "auto"})
            clear_user_cache(usuario_key)
