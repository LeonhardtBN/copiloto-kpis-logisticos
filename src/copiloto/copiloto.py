"""Pipeline do copiloto: pergunta em português → SQL validado → resultado → resumo.

1. O Claude recebe o schema e gera o SQL por meio de uma ferramenta (saída estruturada).
2. O SQL passa pelo `sql_guard` e é executado no DuckDB.
3. Se a validação ou a execução falhar, o erro volta para o Claude corrigir (até N tentativas).
4. O Claude escreve um resumo curto usando SOMENTE os números retornados.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import duckdb
import pandas as pd

from .db import SCHEMA_DESCRICAO
from .sql_guard import SQLInvalido, validar

MODELO_PADRAO = os.getenv("COPILOTO_MODELO", "claude-sonnet-5-5")
LIMITE_LINHAS = 1000

FERRAMENTA_SQL = {
    "name": "executar_consulta",
    "description": "Executa uma consulta SQL (DuckDB) de leitura sobre as tabelas pedidos e itens.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "Uma única consulta SELECT no dialeto DuckDB."},
            "explicacao": {
                "type": "string",
                "description": "Uma frase em português explicando o que a consulta calcula e os filtros usados.",
            },
        },
        "required": ["sql", "explicacao"],
    },
}

PROMPT_SQL = f"""Você é um analista de dados de operações logísticas de um e-commerce brasileiro.
Converta a pergunta do gestor em UMA consulta SQL no dialeto DuckDB e chame a ferramenta `executar_consulta`.

{SCHEMA_DESCRICAO}

Regras:
- Use somente as tabelas `pedidos` e `itens`. Somente leitura.
- Para KPIs de prazo e atraso, considere apenas pedidos com status = 'delivered'.
- Percentuais: multiplique por 100 e arredonde com ROUND(x, 1). Valores em R$: ROUND(x, 2).
- Dê aliases em português e snake_case (ex.: pct_atraso, dias_medios_entrega).
- Ordene o resultado de forma útil e use LIMIT 50 quando listar muitas linhas.
- Para séries no tempo, agrupe por ano_mes e ordene por ele.
- "Atraso" sem outra indicação = percentual de pedidos entregues após a data prevista.
- O dataset cobre compras de 2016 a 2018. Se a pergunta citar um mês sem ano, use o ano mais recente que tenha esse mês nos dados.
"""

PROMPT_RESUMO = """Você escreve para gestores de operações, em português do Brasil.
Responda à pergunta em 2 ou 3 frases curtas usando SOMENTE os números da tabela de resultado.
Destaque o principal achado com o número. Não invente causas nem dados que não estão na tabela.
Se o resultado estiver vazio, diga isso e sugira reformular a pergunta."""


@dataclass
class Resposta:
    pergunta: str
    sql: str | None = None
    explicacao: str | None = None
    dados: pd.DataFrame | None = None
    resumo: str | None = None
    erro: str | None = None
    tentativas: int = 0
    historico_erros: list[str] = field(default_factory=list)


class Copiloto:
    def __init__(self, con: duckdb.DuckDBPyConnection, cliente=None, modelo: str = MODELO_PADRAO,
                 max_tentativas: int = 3):
        if cliente is None:
            import anthropic  # importado aqui para os testes rodarem sem a chave

            cliente = anthropic.Anthropic()
        self.con = con
        self.cliente = cliente
        self.modelo = modelo
        self.max_tentativas = max_tentativas

    def perguntar(self, pergunta: str) -> Resposta:
        resposta = Resposta(pergunta=pergunta)
        mensagens: list[dict] = [{"role": "user", "content": pergunta}]

        for tentativa in range(1, self.max_tentativas + 1):
            resposta.tentativas = tentativa
            msg = self.cliente.messages.create(
                model=self.modelo,
                max_tokens=1024,
                system=PROMPT_SQL,
                tools=[FERRAMENTA_SQL],
                tool_choice={"type": "tool", "name": FERRAMENTA_SQL["name"]},
                messages=mensagens,
            )
            chamada = next(b for b in msg.content if b.type == "tool_use")
            sql, explicacao = chamada.input.get("sql", ""), chamada.input.get("explicacao", "")
            resposta.sql, resposta.explicacao = sql, explicacao

            try:
                sql_ok = validar(sql)
                dados = self.con.sql(sql_ok).limit(LIMITE_LINHAS).df()
            except (SQLInvalido, duckdb.Error) as e:
                erro = str(e).splitlines()[0][:500]
                resposta.historico_erros.append(erro)
                mensagens += [
                    {"role": "assistant", "content": msg.content},
                    {"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": chamada.id, "is_error": True,
                        "content": f"A consulta falhou: {erro}. Corrija e chame a ferramenta de novo.",
                    }]},
                ]
                continue

            resposta.sql, resposta.dados = sql_ok, dados
            resposta.resumo = self._resumir(pergunta, sql_ok, dados)
            return resposta

        resposta.erro = (
            "Não consegui montar uma consulta válida para essa pergunta. "
            "Tente reformular, citando a métrica e o recorte (ex.: região, mês, categoria)."
        )
        return resposta

    def _resumir(self, pergunta: str, sql: str, dados: pd.DataFrame) -> str:
        tabela = dados.head(50).to_csv(index=False) if not dados.empty else "(resultado vazio)"
        msg = self.cliente.messages.create(
            model=self.modelo,
            max_tokens=400,
            system=PROMPT_RESUMO,
            messages=[{
                "role": "user",
                "content": f"Pergunta: {pergunta}\n\nSQL executado:\n{sql}\n\n"
                           f"Resultado ({len(dados)} linhas, primeiras 50 em CSV):\n{tabela}",
            }],
        )
        return "".join(b.text for b in msg.content if b.type == "text").strip()


def sugerir_grafico(dados: pd.DataFrame) -> dict | None:
    """Escolhe um gráfico simples: linha para séries no tempo, barras para categorias."""
    if dados is None or dados.empty or len(dados) < 2 or len(dados) > 60 or dados.shape[1] < 2:
        return None
    numericas = [c for c in dados.columns if pd.api.types.is_numeric_dtype(dados[c])
                 and not pd.api.types.is_bool_dtype(dados[c])]
    categoricas = [c for c in dados.columns if c not in numericas]
    if not categoricas or not numericas:
        return None
    x = categoricas[0]
    temporal = pd.api.types.is_datetime64_any_dtype(dados[x]) or any(
        p in x.lower() for p in ("ano_mes", "data", "mes", "semana", "dia")
    )
    return {"tipo": "linha" if temporal else "barra", "x": x, "y": numericas[0]}
