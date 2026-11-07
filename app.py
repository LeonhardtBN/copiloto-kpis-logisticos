"""Interface do Copiloto de KPIs Logísticos.

Rode com: streamlit run app.py
"""

import os
import sys
from pathlib import Path

import altair as alt
import streamlit as st
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent / "src"))
from copiloto import Copiloto, conectar, sugerir_grafico  # noqa: E402

load_dotenv()
PASTA_DADOS = os.getenv("COPILOTO_DADOS", "data/olist")

EXEMPLOS = [
    "Quais regiões têm o maior percentual de pedidos atrasados?",
    "Como evoluiu o percentual de atraso mês a mês?",
    "Qual o tempo médio de entrega por estado no Nordeste?",
    "Pedidos atrasados recebem notas piores? Compare a nota média.",
    "Quais categorias têm o frete mais caro em relação ao preço?",
]

st.set_page_config(page_title="Copiloto de KPIs Logísticos", layout="wide")


@st.cache_resource
def carregar_copiloto(pasta: str) -> Copiloto:
    return Copiloto(conectar(pasta))


st.title("Copiloto de KPIs Logísticos")
st.caption("Pergunte em português sobre prazos, atrasos, frete e satisfação. "
           "A resposta vem com o SQL usado, a tabela e um gráfico.")

if not os.getenv("ANTHROPIC_API_KEY"):
    st.error("Defina ANTHROPIC_API_KEY no arquivo .env (veja .env.example).")
    st.stop()
try:
    copiloto = carregar_copiloto(PASTA_DADOS)
except FileNotFoundError as e:
    st.error(str(e))
    st.stop()

if "amostra" in PASTA_DADOS:
    st.warning("Usando a amostra SINTÉTICA. Os números são inventados; baixe o dataset real da Olist.")

with st.sidebar:
    st.subheader("Exemplos")
    for exemplo in EXEMPLOS:
        if st.button(exemplo, width="stretch"):
            st.session_state.pergunta = exemplo
    st.divider()
    st.caption(f"Dados: `{PASTA_DADOS}` · Modelo: `{copiloto.modelo}`")

pergunta = st.text_input("Sua pergunta", key="pergunta",
                         placeholder="Ex.: quais estados mais atrasaram em março de 2018?")

if pergunta:
    with st.spinner("Gerando a consulta e analisando os dados..."):
        resposta = copiloto.perguntar(pergunta)

    if resposta.erro:
        st.error(resposta.erro)
        with st.expander("Detalhes"):
            st.code(resposta.sql or "", language="sql")
            for erro in resposta.historico_erros:
                st.text(erro)
        st.stop()

    st.markdown(f"### Resposta\n{resposta.resumo}")

    grafico = sugerir_grafico(resposta.dados)
    if grafico:
        base = alt.Chart(resposta.dados)
        marca = base.mark_line(point=True) if grafico["tipo"] == "linha" else base.mark_bar()
        x = alt.X(grafico["x"], sort=None if grafico["tipo"] == "linha" else "-y", title=grafico["x"])
        st.altair_chart(
            marca.encode(x=x, y=alt.Y(grafico["y"], title=grafico["y"]), tooltip=list(resposta.dados.columns)),
            width="stretch",
        )

    st.dataframe(resposta.dados, width="stretch", hide_index=True)

    with st.expander("Como calculei"):
        st.write(resposta.explicacao)
        st.code(resposta.sql, language="sql")
        if resposta.tentativas > 1:
            st.caption(f"A consulta foi corrigida automaticamente após {resposta.tentativas - 1} erro(s).")
