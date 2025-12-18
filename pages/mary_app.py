# mary_app.py (v3 - Tema escuro + Default Chimera + Parágrafos)
from __future__ import annotations

import time
import streamlit as st
import importlib
import inspect
import re

import characters.mary.persona as mary_persona
from characters.mary.service import MaryService, _current_user_key, _persist_scene_basics
from characters.mary.persona import get_persona
from core.service_router import list_models
from core.database import db_status
from core.repositories import (
    save_interaction,
    get_history_docs,
    get_history_docs_multi,
    get_facts,
    set_fact,
    delete_fact,
    delete_last_interaction,
    delete_user_history,
)

# ==========================================================
# CONFIG
# ==========================================================
st.set_page_config(
    page_title="Mary – Esposa Cúmplice",
    page_icon="💍💍",
    layout="centered",
)

SENHA_CORRETA = "311071"
DEFAULT_VISUAL_LIMIT = 80

# ✅ DEFAULT DE MODELO (Mary)
DEFAULT_MODEL = "tngtech/deepseek-r1t2-chimera:free"
FALLBACK_MODEL = "deepseek/deepseek-chat-v3-0324"

# ==========================================================
# TEMA ESCURO (UI)
# ==========================================================
def _apply_dark_ui() -> None:
    st.markdown(
        """
        <style>
        /* ===== Base ===== */
        .stApp {
            background-color: #000 !important;
        }

        /* Texto padrão (principal + sidebar) */
        .stApp, .stApp * {
            color: #f2f2f2 !important;
        }

        /* Sidebar */
        section[data-testid="stSidebar"] {
            background-color: #070707 !important;
            border-right: 1px solid #1a1a1a !important;
        }
        section[data-testid="stSidebar"] * {
            color: #f2f2f2 !important;
        }

        /* Labels (inputs) */
        label, label * {
            color: #f2f2f2 !important;
        }

        /* Inputs */
        input, textarea {
            background-color: #0b0b0b !important;
            color: #f2f2f2 !important;
            border: 1px solid #2a2a2a !important;
        }

        /* Selectbox / multiselect (container) */
        div[data-baseweb="select"] > div {
            background-color: #0b0b0b !important;
            border: 1px solid #2a2a2a !important;
        }
        div[data-baseweb="select"] * {
            color: #f2f2f2 !important;
        }

        /* Botões */
        button {
            background-color: #111 !important;
            color: #f2f2f2 !important;
            border: 1px solid #2a2a2a !important;
        }
        button:hover {
            border-color: #3a3a3a !important;
        }

        /* Expander / cards */
        div[data-testid="stExpander"] {
            background-color: #0a0a0a !important;
            border: 1px solid #1a1a1a !important;
            border-radius: 12px !important;
        }

        /* Separadores */
        hr {
            border: none !important;
            border-top: 1px solid #1a1a1a !important;
        }

        /* Código */
        pre, code {
            background-color: #0b0b0b !important;
            border: 1px solid #1a1a1a !important;
            color: #f2f2f2 !important;
        }

        /* Chat bubbles */
        div[data-testid="stChatMessage"] > div {
            background-color: #0b0b0b !important;
            border: 1px solid #1a1a1a !important;
            border-radius: 14px !important;
            padding: 14px 14px 10px 14px !important;
        }

        /* Parágrafos do chat */
        div[data-testid="stChatMessage"] p {
            margin: 0 0 0.95rem 0 !important;
            line-height: 1.55 !important;
            font-size: 1.02rem !important;
            color: #f2f2f2 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _format_paragraphs(text: str) -> str:
    """
    Garante leitura: se vier tudo "colado", tenta quebrar em parágrafos.
    - Se já tem parágrafos (dupla quebra), não mexe.
    - Caso contrário, agrupa ~2-3 frases por parágrafo.
    """
    t = (text or "").strip()
    if not t:
        return t

    # Já tem parágrafos? respeita
    if "\n\n" in t:
        return t

    # Quebra por linhas (às vezes vem com \n simples)
    if "\n" in t and t.count("\n") >= 2:
        # normaliza para duplo \n entre blocos
        t2 = re.sub(r"\n{2,}", "\n\n", t)
        # se ainda ficou sem parágrafo, segue para heurística
        if "\n\n" in t2:
            return t2
        t = t2.replace("\n", " ")

    # Heurística por frases
    sentences = re.split(r"(?<=[.!?…])\s+", t)
    sentences = [s.strip() for s in sentences if s.strip()]

    if len(sentences) <= 3:
        return t  # curto, não força

    chunks = []
    i = 0
    while i < len(sentences):
        # 2 frases por parágrafo; às vezes 3 para variar
        size = 2 if (i % 6) != 4 else 3
        chunk = " ".join(sentences[i:i+size]).strip()
        if chunk:
            chunks.append(chunk)
        i += size

    return "\n\n".join(chunks)


# ==========================================================
# SENHA
# ==========================================================
def check_password() -> bool:
    if "senha_ok" not in st.session_state:
        st.session_state["senha_ok"] = False
    if st.session_state["senha_ok"]:
        return True

    _apply_dark_ui()

    st.title("🔐 Mary – Acesso Restrito")
    with st.form("form_senha", clear_on_submit=False):
        senha = st.text_input("Digite a senha de acesso:", type="password")
        ok = st.form_submit_button("Entrar")

    if ok:
        if senha == SENHA_CORRETA:
            st.session_state["senha_ok"] = True
            st.success("Acesso liberado!")
            st.rerun()
        else:
            st.error("Senha incorreta. Tente novamente.")
    return False


if not check_password():
    st.stop()


# ==========================================================
# ✅ HELPER PARA EXTRAIR LOCAL
# ==========================================================
def _extract_location_from_text(text: str) -> str | None:
    locations = {
        "cozinha": ["cozinha", "geladeira", "fogão", "pia da cozinha", "panela"],
        "quarto": ["quarto", "cama", "lençol", "colchão", "criado-mudo", "guarda-roupa"],
        "banheiro": ["banheiro", "espelho", "pia do banheiro", "chuveiro", "banheira"],
        "sala": ["sala", "sofá", "sofa", "televisão", "estante"],
        "corredor": ["corredor", "hall"],
    }
    text_lower = text.lower()
    for location, keywords in locations.items():
        if any(kw in text_lower for kw in keywords):
            return location
    return None


# ==========================================================
# HELPERS
# ==========================================================
def _get_service() -> MaryService:
    svc = st.session_state.get("_mary_service")
    if svc is None:
        svc = MaryService()
        st.session_state["_mary_service"] = svc
    return svc


def _invalidate_backend_cache() -> None:
    st.session_state["backend_hist_cache"] = None
    st.session_state["backend_hist_cache_ts"] = 0.0


def _keys_para_mary() -> list[str]:
    usuario_key = _current_user_key()  # ex: Janio::mary
    usuario_legado = str(st.session_state.get("user_id") or "").strip()  # ex: Janio
    keys = [usuario_key]
    if usuario_legado and usuario_legado != usuario_key:
        keys.append(usuario_legado)
    return keys


def _on_user_change() -> None:
    st.session_state["chat_history"] = []
    st.session_state["mary_intro_done"] = False
    _invalidate_backend_cache()


def _choose_default_model(available: list[str]) -> str:
    if available and DEFAULT_MODEL in available:
        return DEFAULT_MODEL
    if available:
        # Se não tiver Chimera listado, tenta evitar “grok” como default
        non_grok = [m for m in available if "grok" not in (m or "").lower()]
        return (non_grok[0] if non_grok else available[0])
    return FALLBACK_MODEL


def _garantir_estado_inicial() -> None:
    if "user_id" not in st.session_state or not st.session_state["user_id"]:
        st.session_state["user_id"] = "Janio Donisete"
    if "chat_history" not in st.session_state:
        st.session_state["chat_history"] = []

    # ✅ garante lista de modelos
    try:
        modelos = list_models() or []
    except Exception:
        modelos = []

    # ✅ modelo default correto e persistente
    if "model" not in st.session_state or not st.session_state["model"]:
        st.session_state["model"] = _choose_default_model(modelos)
    else:
        # se por algum motivo ficou em grok, puxa pro default
        if "grok" in str(st.session_state["model"]).lower():
            st.session_state["model"] = _choose_default_model(modelos)

    if "mary_nsfw_on" not in st.session_state:
        st.session_state["mary_nsfw_on"] = True
    if "mary_intro_done" not in st.session_state:
        st.session_state["mary_intro_done"] = False
    if "visual_limit" not in st.session_state:
        st.session_state["visual_limit"] = DEFAULT_VISUAL_LIMIT
    if "backend_hist_cache" not in st.session_state:
        st.session_state["backend_hist_cache"] = None
    if "backend_hist_cache_ts" not in st.session_state:
        st.session_state["backend_hist_cache_ts"] = 0.0


def _tem_historico_no_backend(keys: list[str]) -> bool:
    try:
        return bool(get_history_docs_multi(keys, limit=1) or [])
    except Exception:
        try:
            return bool(get_history_docs(keys[0], limit=1) or [])
        except Exception:
            return False


def _gerar_fala_inicial_e_salvar_backend() -> str:
    try:
        _, history_boot = get_persona()
    except Exception:
        history_boot = []

    intro = ""
    if isinstance(history_boot, list):
        for msg in history_boot:
            if isinstance(msg, dict) and msg.get("content"):
                intro = str(msg["content"]).strip()
                break

    if not intro:
        intro = "Oi… eu tô aqui. Vamos começar do zero, do jeito certo."

    try:
        keys = _keys_para_mary()
        usuario_key = keys[0]

        initial_location = _extract_location_from_text(intro)
        if initial_location:
            _persist_scene_basics(usuario_key, initial_location, "agora", "início de cena")

        if not st.session_state.get("mary_intro_done", False):
            save_interaction(usuario_key, "[FALA_INICIAL_MARY]", intro, "mary-persona-static")

    except Exception as e:
        st.error(f"⚠️ Erro ao salvar fala inicial: {e}")

    return intro


def _colar_fala_inicial_na_tela() -> None:
    intro = _gerar_fala_inicial_e_salvar_backend()
    st.session_state["chat_history"] = [("assistant", intro)]
    st.session_state["mary_intro_done"] = True


def _carregar_chat_visual_do_backend(force: bool = False) -> list[tuple[str, str]]:
    now = time.time()

    if not force:
        cached = st.session_state.get("backend_hist_cache")
        ts = float(st.session_state.get("backend_hist_cache_ts", 0.0))
        if cached is not None and (now - ts) < 2.0:
            return cached

    keys = _keys_para_mary()
    try:
        docs = get_history_docs_multi(keys, limit=800) or []
    except Exception:
        docs = []

    hist: list[tuple[str, str]] = []
    for d in docs:
        u = (d.get("mensagem_usuario") or "").strip()
        a = (d.get("resposta_mary") or "").strip()
        if u:
            hist.append(("user", u))
        if a:
            hist.append(("assistant", a))

    st.session_state["backend_hist_cache"] = hist
    st.session_state["backend_hist_cache_ts"] = now
    return hist


def _apagar_hist_bd_novo_e_legado() -> int:
    keys = _keys_para_mary()
    total = 0
    for k in keys:
        try:
            total += int(delete_user_history(k) or 0)
        except Exception:
            pass
    return total


def _diagnostico_hist(keys: list[str]) -> dict:
    out = {}
    for k in keys:
        try:
            docs = get_history_docs(k, limit=5) or []
            out[k] = {
                "count_approx_5": len(docs),
                "first_user": (docs[0].get("mensagem_usuario") if docs else None),
                "first_mary": (docs[0].get("resposta_mary") if docs else None),
                "last_user": (docs[-1].get("mensagem_usuario") if docs else None),
                "last_mary": (docs[-1].get("resposta_mary") if docs else None),
            }
        except Exception as e:
            out[k] = {"error": f"{type(e).__name__}: {e}"}
    return out


def _apagar_eventos_mary_fact(usuario_key: str) -> int:
    try:
        facts = get_facts(usuario_key) or {}
    except Exception:
        facts = {}

    keys = [k for k in facts.keys() if isinstance(k, str) and (k.startswith("mary.evento.") or k.startswith("mary.eventos."))]
    removed = 0
    for k in keys:
        try:
            if delete_fact(usuario_key, k):
                removed += 1
        except Exception:
            pass
    return removed


# ==========================================================
# APP
# ==========================================================
def main() -> None:
    _apply_dark_ui()
    _garantir_estado_inicial()
    svc = _get_service()

    st.caption("🧩 mary_app.py v3 (Tema escuro + default Chimera + parágrafos)")

    backend, detail = db_status()
    st.caption(f"🗄️ Backend atual: **{backend}** ({detail})")

    st.title("Mary – Esposa Cúmplice 💍💍")

    # ====== BOTÕES DE BACKEND ======
    keys = _keys_para_mary()
    with st.expander("🧨 BACKEND — apagar histórico de verdade + diagnóstico", expanded=False):
        st.write("Chaves usadas (novo + legado):", keys)

        if st.button("🔎 Diagnóstico agora"):
            st.json(_diagnostico_hist(keys))

        colA, colB = st.columns(2)
        with colA:
            confirmar = st.checkbox("Confirmo apagar TODO histórico do BD (history) para novo+legado", value=False)
            if st.button("🔥 APAGAR HISTÓRICO DO BD (AGORA)", type="primary"):
                if not confirmar:
                    st.error("Marque a confirmação.")
                else:
                    n = _apagar_hist_bd_novo_e_legado()
                    _invalidate_backend_cache()
                    st.session_state["chat_history"] = []
                    st.session_state["mary_intro_done"] = False
                    st.success(f"✅ Apaguei do BD (history): {n} registros (novo+legado).")
                    st.rerun()

        with colB:
            confirmar2 = st.checkbox("Confirmo apagar facts mary.evento.* também", value=False)
            if st.button("💣 APAGAR EVENTOS mary.evento.* (facts)"):
                if not confirmar2:
                    st.error("Marque a confirmação.")
                else:
                    removed = _apagar_eventos_mary_fact(_current_user_key())
                    st.success(f"✅ Apaguei {removed} facts de eventos mary.evento.*")
                    st.rerun()

    # ===== SIDEBAR NORMAL =====
    with st.sidebar:
        st.header("Mary – Controles")

        st.text_input("👤 Usuário", value="Janio Donisete", disabled=True)
        st.caption(f"🔑 usuario_key atual: {_current_user_key()}")

        try:
            all_models = list_models() or []
        except Exception:
            all_models = []
        if not all_models:
            all_models = [FALLBACK_MODEL]

        # ✅ garante que o estado atual existe na lista
        if st.session_state.get("model") not in all_models:
            st.session_state["model"] = _choose_default_model(all_models)

        # ✅ selectbox com default Chimera (se disponível)
        default_idx = all_models.index(DEFAULT_MODEL) if DEFAULT_MODEL in all_models else 0
        current = st.session_state.get("model")
        if current in all_models:
            default_idx = all_models.index(current)

        st.selectbox(
            "🧠 Modelo",
            all_models,
            index=default_idx,
            key="model",
        )

        st.markdown("---")
        st.checkbox("Modo adulto liberado (NSFW)", key="mary_nsfw_on")

        st.markdown("---")
        st.subheader("Turnos")
        if st.button("Apagar último turno (backend)"):
            ks = _keys_para_mary()
            ok = False
            try:
                ok = delete_last_interaction(ks[0])
            except Exception as e:
                st.error(f"Erro apagar último (novo): {e}")
            if not ok and len(ks) > 1:
                try:
                    ok = delete_last_interaction(ks[1])
                except Exception as e:
                    st.error(f"Erro apagar último (legado): {e}")
            _invalidate_backend_cache()
            st.success("OK" if ok else "Nada para apagar.")
            st.rerun()

        st.markdown("---")
        st.subheader("Limpar tela")
        if st.button("Limpar tela (visual)"):
            st.session_state["chat_history"] = []
            st.rerun()

        st.markdown("---")
        st.subheader("🎭 Persona")

        st.caption("Arquivo ativo:")
        st.code(inspect.getfile(mary_persona.get_persona))

        if st.button("♻️ Recarregar persona AGORA"):
            importlib.reload(mary_persona)
            st.session_state.pop("_mary_service", None)
            st.session_state["mary_intro_done"] = False
            st.session_state["chat_history"] = []
            st.session_state["backend_hist_cache"] = None
            st.session_state["backend_hist_cache_ts"] = 0.0
            st.success("Persona recarregada. Fala inicial será regenerada.")
            st.rerun()

    # ===== BOOT =====
    if not st.session_state["chat_history"]:
        backend_hist = _carregar_chat_visual_do_backend(force=False)
        if backend_hist:
            st.session_state["chat_history"] = backend_hist
            st.session_state["mary_intro_done"] = True
        else:
            if not st.session_state.get("mary_intro_done", False):
                _colar_fala_inicial_na_tela()

    # ===== RENDER =====
    hist = st.session_state.get("chat_history", [])
    visual_limit = int(st.session_state.get("visual_limit", DEFAULT_VISUAL_LIMIT))
    visible = hist[-visual_limit:] if len(hist) > visual_limit else hist

    for role, content in visible:
        with st.chat_message(role):
            if role == "assistant":
                st.markdown(_format_paragraphs(content))
            else:
                st.markdown(content)

    # ===== INPUT =====
    prompt = st.chat_input("Fala algo pra Mary...")
    if prompt:
        st.session_state["chat_history"].append(("user", prompt))
        with st.chat_message("user"):
            st.markdown(prompt)

        st.session_state["chat_input"] = prompt
        resposta = svc.reply(
            user=st.session_state.get("user_id", "Janio"),
            model=st.session_state.get("model") or DEFAULT_MODEL,
        )
        st.session_state["chat_input"] = ""

        with st.chat_message("assistant"):
            st.markdown(_format_paragraphs(resposta))

        st.session_state["chat_history"].append(("assistant", resposta))
        _invalidate_backend_cache()


if __name__ == "__main__":
    main()
