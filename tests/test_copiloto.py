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

from google.genai import errors  # noqa: E402

from copiloto.copiloto import ConsultaSQL  # noqa: E402


class ClienteFalso:
    """Imita `genai.Client().models`: devolve SQLs pré-definidos e um resumo.

    `falhas_429` = quantas chamadas seguidas devem falhar por limite de taxa antes de responder.
    """

    def __init__(self, sqls, falhas_429=0):
        self.sqls = list(sqls)
        self.falhas_429 = falhas_429
        self.chamadas = []
        self.models = self

    def generate_content(self, *, model, contents, config):
        self.chamadas.append({"model": model, "contents": list(contents), "config": config})
        if self.falhas_429:
            self.falhas_429 -= 1
            raise errors.ClientError(429, {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}})
        if config.response_schema is ConsultaSQL:
            consulta = ConsultaSQL(sql=self.sqls.pop(0), explicacao="teste")
            return SimpleNamespace(parsed=consulta, text=consulta.model_dump_json())
        return SimpleNamespace(parsed=None, text="Resumo de teste.")


def _texto(conteudo):
    return conteudo.parts[0].text


def test_pipeline_feliz(con):
    cliente = ClienteFalso(["SELECT regiao_cliente, COUNT(*) AS pedidos FROM pedidos GROUP BY 1 ORDER BY 2 DESC"])
    r = Copiloto(con, cliente=cliente).perguntar("Quantos pedidos por região?")
    assert r.erro is None and r.tentativas == 1
    assert len(r.dados) == 5 and r.resumo == "Resumo de teste."
    assert cliente.chamadas[0]["model"].startswith("gemini")
    assert "regiao_cliente" in _texto(cliente.chamadas[-1]["contents"][0])  # resumo recebe os dados


def test_pipeline_corrige_apos_erro(con):
    cliente = ClienteFalso([
        "SELECT * FROM raw_orders",                # bloqueado pelo guard
        "SELECT coluna_inexistente FROM pedidos",  # erro do DuckDB
        "SELECT COUNT(*) AS total FROM pedidos",
    ])
    r = Copiloto(con, cliente=cliente).perguntar("Total de pedidos?")
    assert r.erro is None and r.tentativas == 3 and len(r.historico_erros) == 2
    conversa = cliente.chamadas[1]["contents"]
    assert [c.role for c in conversa] == ["user", "model", "user"]
    assert "falhou" in _texto(conversa[-1])


def test_pipeline_desiste_depois_do_limite(con):
    cliente = ClienteFalso(["DROP TABLE pedidos"] * 3)
    r = Copiloto(con, cliente=cliente).perguntar("Apague tudo")
    assert r.erro and r.dados is None and r.tentativas == 3


def test_espera_e_tenta_de_novo_no_limite_do_plano_gratuito(con):
    cliente = ClienteFalso(["SELECT COUNT(*) AS total FROM pedidos"], falhas_429=2)
    r = Copiloto(con, cliente=cliente, esperas_limite_taxa=(0, 0)).perguntar("Total?")
    assert r.erro is None and len(cliente.chamadas) == 4  # 2 falhas + SQL + resumo


def test_limite_persistente_vira_erro(con):
    cliente = ClienteFalso(["SELECT 1"], falhas_429=5)
    with pytest.raises(errors.ClientError):
        Copiloto(con, cliente=cliente, esperas_limite_taxa=(0, 0)).perguntar("Total?")


# ---------- gráfico ----------

def test_sugere_linha_para_serie_temporal():
    df = pd.DataFrame({"ano_mes": ["2018-01", "2018-02"], "pct_atraso": [5.0, 9.1]})
    assert sugerir_grafico(df) == {"tipo": "linha", "x": "ano_mes", "y": "pct_atraso"}


def test_sugere_barra_para_categorias():
    df = pd.DataFrame({"regiao_cliente": ["Norte", "Sul"], "dias": [20.0, 12.0]})
    assert sugerir_grafico(df)["tipo"] == "barra"


def test_sem_grafico_para_valor_unico():
    assert sugerir_grafico(pd.DataFrame({"total": [10]})) is None
