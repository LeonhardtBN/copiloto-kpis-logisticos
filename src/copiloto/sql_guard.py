"""Valida o SQL gerado pelo LLM antes de executar.

Regras: um único comando, somente leitura (SELECT/WITH), apenas as views
`pedidos` e `itens`, sem funções que leem arquivos ou acessam o sistema.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from .db import TABELAS_PERMITIDAS

FUNCOES_PROIBIDAS_PREFIXO = ("read_", "parquet_", "sniff_", "glob", "duckdb_", "pragma_")
FUNCOES_PROIBIDAS = {"query", "query_table", "getenv", "current_setting", "sqlite_scan", "postgres_scan"}
NOS_PROIBIDOS = (
    exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter,
    exp.Command, exp.Copy, exp.Attach, exp.Detach, exp.Pragma, exp.Set, exp.Use, exp.Merge,
)


class SQLInvalido(ValueError):
    """O SQL não passou na validação. A mensagem é devolvida ao LLM para correção."""


def validar(sql: str) -> str:
    """Retorna o SQL normalizado se for seguro; levanta SQLInvalido caso contrário."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise SQLInvalido("A consulta está vazia.")
    try:
        comandos = [c for c in sqlglot.parse(sql, read="duckdb") if c is not None]
    except sqlglot.errors.ParseError as e:
        raise SQLInvalido(f"Erro de sintaxe: {e}") from e

    if len(comandos) != 1:
        raise SQLInvalido("Envie exatamente um comando SQL.")
    arvore = comandos[0]

    if not isinstance(arvore, exp.Query):
        raise SQLInvalido("Somente consultas de leitura (SELECT/WITH) são permitidas.")
    for no in arvore.walk():
        if isinstance(no, NOS_PROIBIDOS):
            raise SQLInvalido(f"Comando não permitido: {type(no).__name__.upper()}.")

    ctes = {cte.alias_or_name.lower() for cte in arvore.find_all(exp.CTE)}
    for tabela in arvore.find_all(exp.Table):
        if not isinstance(tabela.this, exp.Identifier):
            raise SQLInvalido("Funções de tabela não são permitidas; use apenas `pedidos` e `itens`.")
        nome = tabela.name.lower()
        if tabela.db or tabela.catalog:
            raise SQLInvalido(f"Use as tabelas sem prefixo de schema: `{tabela.sql()}`.")
        if nome not in TABELAS_PERMITIDAS and nome not in ctes:
            raise SQLInvalido(f"Tabela `{nome}` não existe. Use apenas `pedidos` e `itens`.")

    for func in arvore.find_all(exp.Func):
        nome = (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).lower()
        if nome in FUNCOES_PROIBIDAS or nome.startswith(FUNCOES_PROIBIDAS_PREFIXO):
            raise SQLInvalido(f"Função não permitida: {nome}().")

    return sql
