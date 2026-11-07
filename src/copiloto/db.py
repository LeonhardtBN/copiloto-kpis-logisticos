"""Carrega o dataset público da Olist no DuckDB e cria as views analíticas.

As views `pedidos` e `itens` são a única superfície que o LLM enxerga:
nomes em português, uma linha por pedido/item e KPIs já calculados.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

ARQUIVOS = {
    "orders": "olist_orders_dataset.csv",
    "customers": "olist_customers_dataset.csv",
    "items": "olist_order_items_dataset.csv",
    "reviews": "olist_order_reviews_dataset.csv",
    "sellers": "olist_sellers_dataset.csv",
    "products": "olist_products_dataset.csv",
    "categorias": "product_category_name_translation.csv",
}

REGIOES = {
    "Norte": ["AC", "AP", "AM", "PA", "RO", "RR", "TO"],
    "Nordeste": ["AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE"],
    "Centro-Oeste": ["DF", "GO", "MT", "MS"],
    "Sudeste": ["ES", "MG", "RJ", "SP"],
    "Sul": ["PR", "RS", "SC"],
}

# Descrição enviada ao LLM. Mantida junto do SQL das views para não divergir.
SCHEMA_DESCRICAO = """
Tabela `pedidos` (uma linha por pedido):
- pedido_id VARCHAR
- status VARCHAR -- 'delivered', 'shipped', 'canceled', 'unavailable', 'invoiced', 'processing', 'created', 'approved'
- data_compra TIMESTAMP
- data_aprovacao TIMESTAMP
- data_envio_transportadora TIMESTAMP
- data_entrega TIMESTAMP -- NULL se ainda não entregue
- data_prevista_entrega TIMESTAMP
- ano INTEGER, mes INTEGER, ano_mes VARCHAR -- da data_compra, ano_mes no formato 'AAAA-MM'
- dias_entrega INTEGER -- dias entre compra e entrega (NULL se não entregue)
- dias_atraso INTEGER -- dias entre a data prevista e a entrega; positivo = atrasou (NULL se não entregue)
- atrasado BOOLEAN -- TRUE se entregue depois da data prevista (NULL se não entregue)
- cliente_id VARCHAR, cliente_unico_id VARCHAR
- cidade_cliente VARCHAR, estado_cliente VARCHAR -- UF de 2 letras, ex.: 'SP'
- regiao_cliente VARCHAR -- 'Norte', 'Nordeste', 'Centro-Oeste', 'Sudeste', 'Sul'
- qtd_itens INTEGER
- valor_produtos DOUBLE -- R$, soma dos preços dos itens
- valor_frete DOUBLE -- R$, soma do frete dos itens
- nota_avaliacao DOUBLE -- média das avaliações do pedido, de 1 a 5 (NULL se sem avaliação)

Tabela `itens` (uma linha por item de pedido; junte com `pedidos` por pedido_id):
- pedido_id VARCHAR, item_numero INTEGER
- produto_id VARCHAR
- categoria VARCHAR -- nome da categoria em português, ex.: 'beleza_saude'
- categoria_en VARCHAR -- nome da categoria em inglês
- vendedor_id VARCHAR
- cidade_vendedor VARCHAR, estado_vendedor VARCHAR, regiao_vendedor VARCHAR
- preco DOUBLE -- R$
- frete DOUBLE -- R$
""".strip()

TABELAS_PERMITIDAS = {"pedidos", "itens"}


def _case_regiao(coluna: str) -> str:
    partes = [
        f"WHEN {coluna} IN ({', '.join(repr(uf) for uf in ufs)}) THEN '{regiao}'"
        for regiao, ufs in REGIOES.items()
    ]
    return "CASE " + " ".join(partes) + " END"


def conectar(pasta_dados: str | Path, banco: str | Path = ":memory:") -> duckdb.DuckDBPyConnection:
    """Abre o DuckDB, carrega os CSVs da pasta e cria as views `pedidos` e `itens`."""
    pasta = Path(pasta_dados)
    faltando = [nome for nome in ARQUIVOS.values() if not (pasta / nome).exists()]
    if faltando:
        raise FileNotFoundError(
            f"Arquivos da Olist não encontrados em {pasta}: {', '.join(faltando)}. "
            "Veja a seção 'Dados' do README ou rode `python scripts/gerar_amostra.py`."
        )

    con = duckdb.connect(str(banco))
    for tabela, arquivo in ARQUIVOS.items():
        caminho = (pasta / arquivo).as_posix().replace("'", "''")
        con.execute(
            f"CREATE OR REPLACE TABLE raw_{tabela} AS "
            f"SELECT * FROM read_csv_auto('{caminho}', header=true)"
        )

    con.execute(
        f"""
        CREATE OR REPLACE VIEW pedidos AS
        WITH itens_agg AS (
            SELECT order_id,
                   COUNT(*)            AS qtd_itens,
                   SUM(price)          AS valor_produtos,
                   SUM(freight_value)  AS valor_frete
            FROM raw_items GROUP BY order_id
        ),
        notas AS (
            SELECT order_id, AVG(review_score) AS nota_avaliacao
            FROM raw_reviews GROUP BY order_id
        )
        SELECT
            o.order_id                                        AS pedido_id,
            o.order_status                                    AS status,
            CAST(o.order_purchase_timestamp AS TIMESTAMP)     AS data_compra,
            CAST(o.order_approved_at AS TIMESTAMP)            AS data_aprovacao,
            CAST(o.order_delivered_carrier_date AS TIMESTAMP) AS data_envio_transportadora,
            CAST(o.order_delivered_customer_date AS TIMESTAMP) AS data_entrega,
            CAST(o.order_estimated_delivery_date AS TIMESTAMP) AS data_prevista_entrega,
            year(CAST(o.order_purchase_timestamp AS TIMESTAMP))  AS ano,
            month(CAST(o.order_purchase_timestamp AS TIMESTAMP)) AS mes,
            strftime(CAST(o.order_purchase_timestamp AS TIMESTAMP), '%Y-%m') AS ano_mes,
            date_diff('day', CAST(o.order_purchase_timestamp AS TIMESTAMP),
                             CAST(o.order_delivered_customer_date AS TIMESTAMP)) AS dias_entrega,
            date_diff('day', CAST(o.order_estimated_delivery_date AS DATE),
                             CAST(o.order_delivered_customer_date AS DATE))      AS dias_atraso,
            CAST(o.order_delivered_customer_date AS DATE)
                > CAST(o.order_estimated_delivery_date AS DATE)                  AS atrasado,
            c.customer_id                                     AS cliente_id,
            c.customer_unique_id                              AS cliente_unico_id,
            c.customer_city                                   AS cidade_cliente,
            c.customer_state                                  AS estado_cliente,
            {_case_regiao("c.customer_state")}                AS regiao_cliente,
            COALESCE(i.qtd_itens, 0)                          AS qtd_itens,
            COALESCE(i.valor_produtos, 0)                     AS valor_produtos,
            COALESCE(i.valor_frete, 0)                        AS valor_frete,
            n.nota_avaliacao
        FROM raw_orders o
        LEFT JOIN raw_customers c ON c.customer_id = o.customer_id
        LEFT JOIN itens_agg i     ON i.order_id = o.order_id
        LEFT JOIN notas n         ON n.order_id = o.order_id
        """
    )

    con.execute(
        f"""
        CREATE OR REPLACE VIEW itens AS
        SELECT
            it.order_id                       AS pedido_id,
            it.order_item_id                  AS item_numero,
            it.product_id                     AS produto_id,
            p.product_category_name           AS categoria,
            t.product_category_name_english   AS categoria_en,
            it.seller_id                      AS vendedor_id,
            s.seller_city                     AS cidade_vendedor,
            s.seller_state                    AS estado_vendedor,
            {_case_regiao("s.seller_state")}  AS regiao_vendedor,
            it.price                          AS preco,
            it.freight_value                  AS frete
        FROM raw_items it
        LEFT JOIN raw_products p   ON p.product_id = it.product_id
        LEFT JOIN raw_categorias t ON t.product_category_name = p.product_category_name
        LEFT JOIN raw_sellers s    ON s.seller_id = it.seller_id
        """
    )
    # Defesa em profundidade: depois de carregar os CSVs, o banco não lê nem
    # grava mais nenhum arquivo, e a configuração não pode ser alterada por SQL.
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con
