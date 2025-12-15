from __future__ import annotations

import streamlit as st

from characters.mary.service import MaryService, _current_user_key
from characters.mary.persona import get_persona
from core.service_router import list_models
from core.repositories import save_interaction, get_history_docs, get_facts, set_fact

# ✅ TEM QUE SER A PRIMEIRA CHAMADA streamlit (antes de st.title / st.sidebar / etc.)
st.set_page_config(
    page_title="Mary – Esposa Cúmplice",
    page_icon="💍",
    layout="centered",
)

# ==========================================================
# BLOQUEIO POR SENHA PARA O MARY_APP
# ==========================================================
SENHA_CORRETA = "141267"  # ← coloque aqui a senha que quiser


def check_password() -> bool:
    """Exibe um campo de senha e barra acesso se estiver incorreto."""
    if "senha_ok" not in st.session_state:
        st.session_state["senha_ok"] = False

    if not st.session_state["senha_ok"]:
        st.title("🔐 Mary – Acesso Restrito")
        senha = st.text_input("Digite a senha de acesso:", type="password")

        if st.button("Entrar"):
            if senha == SENHA_CORRETA:
                st.session_state["senha_ok"] = True
                st.success("Acesso liberado!")
                st.rerun()
            else:
                st.error("Senha incorreta. Tente novamente.")

        return False

    return True


# ---- BLOQUEIA EXECUÇÃO DO APP SE A SENHA NÃO FOR VALIDADA ----
if not check_password():
    st.stop()


# ==========================================================
# HELPERS DE ESTADO
# ==========================================================
def _garantir_estado_inicial() -> None:
    # Usuário padrão
    if "user_id" not in st.session_state or not st.session_state["user_id"]:
        st.session_state["user_id"] = "Janio"

    # Histórico visual da tela
    if "chat_history" not in st.session_state:
        st.session_state["chat_history"] = []

    # Modelo padrão
    if "model" not in st.session_state:
        try:
            modelos = list_models() or []
        except Exception:
            modelos = []
        st.session_state["model"] = modelos[0] if modelos else "deepseek/deepseek-chat-v3-0324"

    # Flag NSFW da Mary (default: ligado)
    if "mary_nsfw_on" not in st.session_state:
        st.session_state["mary_nsfw_on"] = True

    # Controle pra saber se já fizemos a fala inicial automática
    if "mary_intro_done" not in st.session_state:
        st.session_state["mary_intro_done"] = False


def _gerar_fala_inicial() -> str:
    """
    Busca a fala inicial definida em persona.get_persona()
    e grava no backend como interação inicial.
    SEM depender de reply() e SEM prompt secreto.
    """
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
        intro = (
            "Eu ajeito o cabelo, dou um sorriso de canto e fico te observando por um instante.\n\n"
            "\"Então… vamos continuar de onde a gente parou, amor?\""
        )

    try:
        usuario_key = _current_user_key()
        save_interaction(usuario_key, "[FALA_INICIAL_MARY]", intro, "mary-persona-static")
    except Exception:
        pass

    return intro


def _colar_fala_inicial_na_tela() -> None:
    intro = _gerar_fala_inicial()
    st.session_state["chat_history"] = [("assistant", intro)]
    st.session_state["mary_intro_done"] = True


def _apagar_ultimo_turno_visual() -> None:
    """
    Remove o último turno visual:
    - se terminar com assistant e antes tiver user, remove os dois.
    - senão, remove só o último.
    Não mexe no backend, é apenas visual.
    """
    hist = st.session_state.get("chat_history", [])
    if not hist:
        return

    if len(hist) >= 2 and hist[-1][0] == "assistant" and hist[-2][0] == "user":
        hist = hist[:-2]
    else:
        hist = hist[:-1]

    st.session_state["chat_history"] = hist


def _carregar_chat_visual_do_backend() -> list[tuple[str, str]]:
    """
    Reconstrói o chat visual a partir dos documentos salvos no Mongo.
    Usa os campos 'mensagem_usuario' e 'resposta_mary'.
    """
    try:
        usuario_key = _current_user_key()
        docs = get_history_docs(usuario_key) or []
    except Exception:
        return []

    hist: list[tuple[str, str]] = []
    for d in docs:
        u = (d.get("mensagem_usuario") or "").strip()
        a = (d.get("resposta_mary") or "").strip()
        if u:
            hist.append(("user", u))
        if a:
            hist.append(("assistant", a))
    return hist


def _list_eventos_mary(facts: dict) -> list[tuple[str, str]]:
    """Lista eventos fixos mary.evento.* e mary.eventos.* (para exibir na sidebar)."""
    eventos: list[tuple[str, str]] = []
    if not isinstance(facts, dict):
        return eventos

    for k, v in facts.items():
        if not isinstance(k, str) or not v:
            continue
        if k.startswith("mary.evento."):
            label = k.replace("mary.evento.", "", 1)
            eventos.append((label, str(v)))
        elif k.startswith("mary.eventos."):
            label = k.replace("mary.eventos.", "", 1)
            eventos.append((label, str(v)))

    eventos.sort(key=lambda x: x[0])
    return eventos


# ==========================================================
# APP PRINCIPAL
# ==========================================================
def main() -> None:
    st.title("Mary – Esposa Cúmplice 💍")

    _garantir_estado_inicial()
    svc = MaryService()

    # ========= SIDEBAR =========
    with st.sidebar:
        st.header("Mary – Controles")

        # Usuário
        st.text_input("👤 Usuário", key="user_id")

        # Seleção de modelo
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

        # NSFW ON/OFF (só UMA fonte de verdade: session_state["mary_nsfw_on"])
        st.checkbox(
            "Modo adulto liberado (NSFW)",
            key="mary_nsfw_on",
            help=(
                "Quando ligado, Mary usa o estilo adulto completo. "
                "Quando desligado, ela fica só no tom sugestivo/romântico."
            ),
        )

        st.markdown("---")
        st.subheader("🎭 Persona / Turnos")

        if st.button("Recarregar persona (limpar tela)"):
            _colar_fala_inicial_na_tela()
            st.success("Persona recarregada. Fala inicial exibida.")
            st.rerun()

        if st.button("Apagar último turno"):
            _apagar_ultimo_turno_visual()
            st.info("Último turno removido da tela (somente visual).")
            st.rerun()

        st.markdown("---")
        st.subheader("🧹 Limpeza / Reset")

        if st.button("Limpar tela (chat visual)"):
            st.session_state["chat_history"] = []
            st.info("Tela limpa. Histórico no backend preservado.")
            st.rerun()

        if st.button("Reset histórico da Mary (sessão)"):
            st.session_state["chat_input"] = "/reset historico"
            resp = svc.reply(
                user=st.session_state.get("user_id", "Janio"),
                model=st.session_state.get("model"),
            )
            st.session_state["chat_history"].append(("assistant", resp))
            st.success("Histórico de diálogo e resumo rolante resetados para esta sessão.")
            st.rerun()

        if st.button("RESET TOTAL da Mary (memórias fixas)"):
            st.session_state["chat_input"] = "/reset total"
            resp = svc.reply(
                user=st.session_state.get("user_id", "Janio"),
                model=st.session_state.get("model"),
            )
            st.session_state["chat_history"].append(("assistant", resp))
            st.warning("RESET TOTAL executado. Memórias fixas de eventos foram apagadas.")
            st.rerun()

        st.markdown("---")
        st.subheader("🧠 Memória / Diagnóstico")

        # Mostra último erro de modelo, se existir
        err = st.session_state.get("mary_last_model_error", "")
        if err:
            st.caption(f"⚠️ Último erro de modelo: {err}")

        # Exibe eventos fixos (somente leitura)
        try:
            usuario_key = _current_user_key()
            facts = get_facts(usuario_key) or {}
        except Exception:
            facts = {}

        with st.expander("📌 Eventos fixos (mary.evento.*) — leitura", expanded=False):
            eventos = _list_eventos_mary(facts)
            if not eventos:
                st.caption("Nenhum evento fixo registrado ainda.")
            else:
                for label, val in eventos:
                    st.markdown(f"**{label}**")
                    vv = str(val)
                    st.caption(vv[:400] + ("..." if len(vv) > 400 else ""))

        # Atalho opcional para setar local da cena (salva como fact)
        with st.expander("📍 Cena atual — local", expanded=False):
            local_atual = ""
            try:
                local_atual = str((facts or {}).get("local_cena_atual", "") or "")
            except Exception:
                pass
            novo_local = st.text_input("Local da cena (canônico)", value=local_atual)
            if st.button("Salvar local"):
                try:
                    set_fact(usuario_key, "local_cena_atual", novo_local.strip(), {"fonte": "sidebar"})
                    st.success("Local salvo.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Falha ao salvar local: {e}")

    # ========= FALA INICIAL / RECUPERAÇÃO DO HISTÓRICO =========
    if not st.session_state["chat_history"]:
        backend_hist = _carregar_chat_visual_do_backend()
        if backend_hist:
            st.session_state["chat_history"] = backend_hist
            st.session_state["mary_intro_done"] = True
        else:
            if not st.session_state.get("mary_intro_done", False):
                _colar_fala_inicial_na_tela()

    # ========= RENDER DO HISTÓRICO =========
    for role, content in st.session_state["chat_history"]:
        with st.chat_message(role):
            st.markdown(content)

    # ========= INPUT DO USUÁRIO =========
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

        with st.chat_message("assistant"):
            st.markdown(resposta)

        st.session_state["chat_history"].append(("assistant", resposta))


# Chamado sempre, já que o arquivo é uma página multipage
main()
