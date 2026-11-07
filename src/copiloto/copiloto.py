"""Pipeline do copiloto: pergunta em português → SQL validado → resultado → resumo.

1. O Gemini recebe o schema e devolve o SQL em JSON (saída estruturada com schema).
2. O SQL passa pelo `sql_guard` e é executado no DuckDB.
3. Se a validação ou a execução falhar, o erro volta para o Gemini corrigir (até N tentativas).
4. O Gemini escreve um resumo curto usando SOMENTE os números retornados.

Usa o plano gratuito da Gemini API (Google AI Studio). O plano gratuito tem limite
de requisições por minuto; quando ele é atingido (erro 429), o copiloto espera e tenta de novo.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field

import duckdb
import pandas as pd
from pydantic import BaseModel, Field

from .db import SCHEMA_DESCRICAO
from .sql_guard import SQLInvalido, validar

MODELO_PADRAO = os.getenv("COPILOTO_MODELO", "gemini-3.8-flash")
LIMITE_LINHAS = 1000
ESPERAS_LIMITE_TAXA = (10, 30, 60)  # segundos de espera a cada erro 429 seguido


class ConsultaSQL(BaseModel):
    sql: str = Field(description="Uma única consulta SELECT no dialeto DuckDB.")
    explicacao: str = Field(
        description="Uma frase em português explicando o que a consulta calcula e os filtros usados."
    )


PROMPT_SQL = f"""Você é um analista de dados de operações logísticas de um e-commerce brasileiro.
Converta a pergunta do gestor em UMA consulta SQL no dialeto DuckDB.
Responda apenas com o JSON pedido, com os campos `sql` e `explicacao`.

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


def _conteudo(papel: str, texto: str):
    from google.genai import types

    return types.Content(role=papel, parts=[types.Part(text=texto)])


class Copiloto:
    def __init__(self, con: duckdb.DuckDBPyConnection, cliente=None, modelo: str = MODELO_PADRAO,
                 max_tentativas: int = 3, esperas_limite_taxa=ESPERAS_LIMITE_TAXA):
        if cliente is None:
            from google import genai  # importado aqui para os testes rodarem sem a chave

            cliente = genai.Client()  # lê GEMINI_API_KEY do ambiente
        self.con = con
        self.cliente = cliente
        self.modelo = modelo
        self.max_tentativas = max_tentativas
        self.esperas_limite_taxa = esperas_limite_taxa

    # ------------------------------------------------------------------ API pública

    def perguntar(self, pergunta: str) -> Resposta:
        from google.genai import types

        resposta = Resposta(pergunta=pergunta)
        conversa = [_conteudo("user", pergunta)]
        config = types.GenerateContentConfig(
            system_instruction=PROMPT_SQL,
            response_mime_type="application/json",
            response_schema=ConsultaSQL,
        )

        for tentativa in range(1, self.max_tentativas + 1):
            resposta.tentativas = tentativa
            retorno = self._gerar(conversa, config)
            consulta = self._ler_consulta(retorno)
            resposta.sql, resposta.explicacao = consulta.sql, consulta.explicacao

            try:
                sql_ok = validar(consulta.sql)
                dados = self.con.sql(sql_ok).limit(LIMITE_LINHAS).df()
            except (SQLInvalido, duckdb.Error) as e:
                erro = str(e).splitlines()[0][:500]
                resposta.historico_erros.append(erro)
                conversa += [
                    _conteudo("model", retorno.text or consulta.model_dump_json()),
                    _conteudo("user", f"A consulta falhou: {erro}. Corrija e responda com o JSON de novo."),
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

    # ------------------------------------------------------------------ internos

    def _gerar(self, conteudos, config):
        """Chama o Gemini, esperando e tentando de novo quando o limite do plano gratuito é atingido."""
        from google.genai import errors

        for espera in (*self.esperas_limite_taxa, None):
            try:
                return self.cliente.models.generate_content(model=self.modelo, contents=conteudos, config=config)
            except errors.APIError as e:
                if e.code != 429 or espera is None:
                    raise
                sugerida = re.search(r"retry in (\d+(?:\.\d+)?)s", str(e)) or re.search(
                    r"'retryDelay': '(\d+)s'", str(e))
                time.sleep(float(sugerida.group(1)) + 1 if sugerida else espera)

    @staticmethod
    def _ler_consulta(retorno) -> ConsultaSQL:
        if isinstance(getattr(retorno, "parsed", None), ConsultaSQL):
            return retorno.parsed
        try:
            return ConsultaSQL.model_validate_json(retorno.text or "")
        except ValueError:
            return ConsultaSQL(sql="", explicacao="O modelo não devolveu um JSON válido.")

    def _resumir(self, pergunta: str, sql: str, dados: pd.DataFrame) -> str:
        from google.genai import types

        tabela = dados.head(50).to_csv(index=False) if not dados.empty else "(resultado vazio)"
        texto = (f"Pergunta: {pergunta}\n\nSQL executado:\n{sql}\n\n"
                 f"Resultado ({len(dados)} linhas, primeiras 50 em CSV):\n{tabela}")
        retorno = self._gerar([_conteudo("user", texto)],
                              types.GenerateContentConfig(system_instruction=PROMPT_RESUMO))
        return (retorno.text or "").strip()


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
