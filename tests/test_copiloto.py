import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))
sys.path.insert(0, str(RAIZ / "scripts"))

from copiloto import Copiloto, conectar, sugerir_grafico  # noqa: E402
from copiloto.sql_guard import SQLInvalido, validar  # noqa: E402
from gerar_amostra import gerar  # noqa: E402


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    pasta = tmp_path_factory.mktemp("dados")
    gerar(pasta, qtd_pedidos=400, seed=7)
    return conectar(pasta)


# ---------- views ----------

def test_views_tem_uma_linha_por_pedido(con):
    total, distintos = con.execute("SELECT COUNT(*), COUNT(DISTINCT pedido_id) FROM pedidos").fetchone()
    assert total == distintos == 400


def test_kpis_de_atraso_coerentes(con):
    inconsistentes = con.execute("""
        SELECT COUNT(*) FROM pedidos
        WHERE status = 'delivered' AND atrasado <> (dias_atraso > 0)
    """).fetchone()[0]
    assert inconsistentes == 0
    nao_entregues = con.execute("SELECT COUNT(*) FROM pedidos WHERE data_entrega IS NULL AND atrasado IS NOT NULL").fetchone()[0]
    assert nao_entregues == 0


def test_toda_uf_tem_regiao(con):
    assert con.execute("SELECT COUNT(*) FROM pedidos WHERE regiao_cliente IS NULL").fetchone()[0] == 0


def test_banco_nao_le_arquivos_depois_de_carregado(con):
    with pytest.raises(Exception):
        con.execute("SELECT * FROM read_csv_auto('/etc/passwd')").fetchall()


# ---------- validação de SQL ----------

@pytest.mark.parametrize("sql", [
    "SELECT regiao_cliente, AVG(dias_entrega) FROM pedidos GROUP BY 1",
    "WITH t AS (SELECT * FROM pedidos) SELECT COUNT(*) FROM t",
    "SELECT i.categoria, COUNT(*) FROM itens i JOIN pedidos p USING (pedido_id) GROUP BY 1;",
])
def test_aceita_leituras(sql):
    assert validar(sql)


@pytest.mark.parametrize("sql", [
    "DROP TABLE pedidos",
    "DELETE FROM pedidos",
    "SELECT 1; DROP TABLE pedidos",
    "SELECT * FROM raw_orders",
    "SELECT * FROM read_csv_auto('/etc/passwd')",
    "SELECT * FROM main.pedidos",
    "COPY pedidos TO 'x.csv'",
    "ATTACH 'x.db'",
    "SELECT getenv('ANTHROPIC_API_KEY')",
    "",
])
def test_bloqueia_comandos_perigosos(sql):
    with pytest.raises(SQLInvalido):
        validar(sql)


# ---------- pipeline com um cliente falso ----------

class ClienteFalso:
    """Imita `anthropic.Anthropic().messages`: devolve SQLs pré-definidos e um resumo."""

    def __init__(self, sqls):
        self.sqls = list(sqls)
        self.chamadas = []
        self.messages = self

    def create(self, **kwargs):
        self.chamadas.append(kwargs)
        if "tools" in kwargs:
            bloco = SimpleNamespace(type="tool_use", id=f"t{len(self.chamadas)}",
                                    input={"sql": self.sqls.pop(0), "explicacao": "teste"})
            return SimpleNamespace(content=[bloco])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="Resumo de teste.")])


def test_pipeline_feliz(con):
    cliente = ClienteFalso(["SELECT regiao_cliente, COUNT(*) AS pedidos FROM pedidos GROUP BY 1 ORDER BY 2 DESC"])
    r = Copiloto(con, cliente=cliente).perguntar("Quantos pedidos por região?")
    assert r.erro is None and r.tentativas == 1
    assert len(r.dados) == 5 and r.resumo == "Resumo de teste."
    assert "regiao_cliente" in cliente.chamadas[-1]["messages"][0]["content"]  # resumo recebe os dados


def test_pipeline_corrige_apos_erro(con):
    cliente = ClienteFalso([
        "SELECT * FROM raw_orders",                # bloqueado pelo guard
        "SELECT coluna_inexistente FROM pedidos",  # erro do DuckDB
        "SELECT COUNT(*) AS total FROM pedidos",
    ])
    r = Copiloto(con, cliente=cliente).perguntar("Total de pedidos?")
    assert r.erro is None and r.tentativas == 3 and len(r.historico_erros) == 2
    feedback = cliente.chamadas[1]["messages"][-1]["content"][0]
    assert feedback["type"] == "tool_result" and feedback["is_error"]


def test_pipeline_desiste_depois_do_limite(con):
    cliente = ClienteFalso(["DROP TABLE pedidos"] * 3)
    r = Copiloto(con, cliente=cliente).perguntar("Apague tudo")
    assert r.erro and r.dados is None and r.tentativas == 3


# ---------- gráfico ----------

def test_sugere_linha_para_serie_temporal():
    df = pd.DataFrame({"ano_mes": ["2018-01", "2018-02"], "pct_atraso": [5.0, 9.1]})
    assert sugerir_grafico(df) == {"tipo": "linha", "x": "ano_mes", "y": "pct_atraso"}


def test_sugere_barra_para_categorias():
    df = pd.DataFrame({"regiao_cliente": ["Norte", "Sul"], "dias": [20.0, 12.0]})
    assert sugerir_grafico(df)["tipo"] == "barra"


def test_sem_grafico_para_valor_unico():
    assert sugerir_grafico(pd.DataFrame({"total": [10]})) is None
