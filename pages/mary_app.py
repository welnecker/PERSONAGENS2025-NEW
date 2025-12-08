from __future__ import annotations
import streamlit as st

from characters.mary.service import MaryService, _current_user_key
from characters.mary.persona import get_persona
from core.service_router import list_models
from core.repositories import save_interaction, get_history_docs

# ==== BLOQUEIO POR SENHA PARA O MARY_APP ====

SENHA_CORRETA = "141267"   # ← coloque aqui a senha que quiser

def check_password():
    """Exibe um campo de senha e barra acesso se estiver incorreto."""
    if "senha_ok" not in st.session_state:
        st.session_state["senha_ok"] = False

    # Se ainda não validou a senha, mostra a caixa
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

        # Impede que o app abaixo carregue
        return False

    return True


# ---- BLOQUEIA EXECUÇÃO DO APP SE A SENHA NÃO FOR VALIDADA ----
if not check_password():
    st.stop()



# ========= HELPERS DE ESTADO =========
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
        st.session_state["model"] = (
            modelos[0] if modelos else "deepseek/deepseek-chat-v3-0324"
        )

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
        persona_text, history_boot = get_persona()
    except Exception:
        persona_text, history_boot = "", []

    intro = ""

    # Tenta achar a primeira fala da Mary no history_boot
    if isinstance(history_boot, list):
        for msg in history_boot:
            if isinstance(msg, dict) and msg.get("content"):
                intro = str(msg["content"]).strip()
                break

    # Fallback simples se não tiver nada no boot
    if not intro:
        intro = (
            "Eu ajeito o cabelo, dou um sorriso de canto e fico te observando por um instante.\n\n"
            "\"Então… vamos continuar de onde a gente parou, amor?\""
        )

    # Tenta registrar essa fala como interação inicial no backend
    try:
        usuario_key = _current_user_key()
        save_interaction(
            usuario_key,
            "[FALA_INICIAL_MARY]",
            intro,
            "mary-persona-static",
        )
    except Exception:
        # Se der erro de backend, não derruba a tela
        pass

    return intro


def _colar_fala_inicial_na_tela() -> None:
    """Adiciona a fala inicial no chat_history e marca intro_done."""
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

    # Ex.: [..., ("user", ...), ("assistant", ...)]
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



# ========= APP PRINCIPAL =========
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
            index=all_models.index(st.session_state["model"])
            if st.session_state["model"] in all_models
            else 0,
            key="model",
        )

        st.markdown("---")

        # NSFW ON/OFF – manda direto pro session_state
        nsfw_on = st.checkbox(
            "Modo adulto liberado (NSFW)",
            value=st.session_state.get("mary_nsfw_on", True),
            help=(
                "Quando ligado, Mary usa o estilo adulto completo. "
                "Quando desligado, ela fica só no tom sugestivo/romântico."
            ),
        )
        st.session_state["mary_nsfw_on"] = nsfw_on

        st.markdown("---")
        st.subheader("🎭 Persona / Turnos")

        # 1) RECARREGAR PERSONA (limpar tela + fala inicial)
        if st.button("Recarregar persona (limpar tela)"):
            _colar_fala_inicial_na_tela()
            st.success("Persona recarregada. Fala inicial exibida.")
            st.rerun()


        # 2) APAGAR ÚLTIMO TURNO (apenas visual)
        if st.button("Apagar último turno"):
            _apagar_ultimo_turno_visual()
            st.info("Último turno removido da tela (somente visual).")
            st.rerun()


        st.markdown("---")
        st.subheader("🧹 Limpeza / Reset")

        # 3) LIMPAR TELA (somente visual)
        if st.button("Limpar tela (chat visual)"):
            st.session_state["chat_history"] = []
            st.info("Tela limpa. Histórico no backend preservado.")
            st.rerun()

        # 4) RESET HISTÓRICO (sessão) – usando comando interno da Mary
        if st.button("Reset histórico da Mary (sessão)"):
            st.session_state["chat_input"] = "/reset historico"
            resp = svc.reply(
                user=st.session_state.get("user_id", "Janio"),
                model=st.session_state.get("model"),
            )
            st.session_state["chat_history"].append(("assistant", resp))
            st.success("Histórico de diálogo e resumo rolante resetados para esta sessão.")
            st.rerun()

        # 5) RESET TOTAL (memórias fixas) – também via comando interno
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

        # Sidebar detalhado da Mary (debug, memórias, etc.)
        try:
            svc_for_sidebar = MaryService()
            svc_for_sidebar.render_sidebar(st.container())
        except Exception:
            st.caption("Sidebar avançado da Mary carregado no modo básico.")

        # ========= FALA INICIAL / RECUPERAÇÃO DO HISTÓRICO =========
    if not st.session_state["chat_history"]:
        # 1) Tenta reconstruir a conversa a partir do Mongo
        backend_hist = _carregar_chat_visual_do_backend()
        if backend_hist:
            st.session_state["chat_history"] = backend_hist
            st.session_state["mary_intro_done"] = True
        else:
            # 2) Se não houver nada salvo, aí sim gera a fala inicial da persona
            if not st.session_state.get("mary_intro_done", False):
                _colar_fala_inicial_na_tela()


    # ========= RENDER DO HISTÓRICO =========
    for role, content in st.session_state["chat_history"]:
        with st.chat_message(role):
            st.markdown(content)

    # ========= INPUT DO USUÁRIO =========
    prompt = st.chat_input("Fala algo pra Mary...")
    if prompt:
        # mostra no chat
        st.session_state["chat_history"].append(("user", prompt))
        with st.chat_message("user"):
            st.markdown(prompt)

        # envia para MaryService
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
