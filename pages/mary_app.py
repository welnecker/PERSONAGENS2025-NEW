# mary_app.py (v2 - Continuidade Espacial)
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


# ==========================================================
# SENHA
# ==========================================================
def check_password() -> bool:
    if "senha_ok" not in st.session_state:
        st.session_state["senha_ok"] = False
    if st.session_state["senha_ok"]:
        return True

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
# ✅ NOVO v2: HELPER PARA EXTRAIR LOCAL
# ==========================================================
def _extract_location_from_text(text: str) -> str | None:
    """Extrai o local mencionado em um texto."""
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
    """Chave nova + chave legada."""
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


def _garantir_estado_inicial() -> None:
    if "user_id" not in st.session_state or not st.session_state["user_id"]:
        st.session_state["user_id"] = "Janio"
    if "chat_history" not in st.session_state:
        st.session_state["chat_history"] = []
    if "model" not in st.session_state:
        try:
            modelos = list_models() or []
        except Exception:
            modelos = []
        st.session_state["model"] = modelos[0] if modelos else "deepseek/deepseek-chat-v3-0324"
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
    """
    ✅ v2: Extrai o local da mensagem de boot e o persiste em cena.local.
    """
    # pega a primeira mensagem do boot da persona
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

    # ✅ NOVO v2: Extrai e persiste o local da fala inicial
    try:
        keys = _keys_para_mary()
        usuario_key = keys[0]
        
        # Extrai o local mencionado na intro
        initial_location = _extract_location_from_text(intro)
        if initial_location:
            # Persiste o local ANTES de salvar a interação
            _persist_scene_basics(usuario_key, initial_location, "agora", "início de cena")
        
        # SEMPRE gera fala inicial nova quando mary_intro_done é False
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
    """APAGA DE VERDADE o histórico (coleção history), novo + legado."""
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
    """Apaga facts mary.evento.* (um por um)."""
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
    _garantir_estado_inicial()
    svc = _get_service()

    # ✅ IDENTIFICADOR DE VERSÃO
    st.success("✅ ESTE É O mary_app.py v2 (Continuidade Espacial) QUE ESTÁ RODANDO AGORA.")

    backend, detail = db_status()
    st.caption(f"🗄️ Backend atual: **{backend}** ({detail})")

    st.title("Mary – Esposa Cúmplice 💍💍")

    # ====== BOTÕES DE BACKEND (NA TELA, NÃO NA SIDEBAR) ======
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

        st.text_input("👤 Usuário", key="user_id", on_change=_on_user_change)
        st.caption(f"🔑 usuario_key atual: {_current_user_key()}")

        try:
            all_models = list_models() or []
        except Exception:
            all_models = []
        if not all_models:
            all_models = ["deepseek/deepseek-chat-v3-0324"]

        st.selectbox(
            "🧠 Modelo",
            all_models,
            index=all_models.index(st.session_state["model"]) if st.session_state["model"] in all_models else 0,
            key="model",
        )

        st.markdown("---")
        st.checkbox("Modo adulto liberado (NSFW)", key="mary_nsfw_on")

        st.markdown("---")
        st.subheader("Turnos")
        if st.button("Apagar último turno (backend)"):
            # tenta no novo; se falhar e tiver legado, tenta no legado
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
            model=st.session_state.get("model"),
        )
        st.session_state["chat_input"] = ""

        with st.chat_message("assistant"):
            st.markdown(resposta)

        st.session_state["chat_history"].append(("assistant", resposta))
        _invalidate_backend_cache()


if __name__ == "__main__":
    main()
