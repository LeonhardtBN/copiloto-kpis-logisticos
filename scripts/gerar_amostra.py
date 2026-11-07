"""Gera uma amostra sintética no MESMO formato dos CSVs da Olist.

Serve para testar o projeto sem baixar o dataset real do Kaggle.
Os números são inventados (com padrões plausíveis: Norte/Nordeste mais lentos,
picos de atraso na Black Friday e em fev-mar), então NÃO use a amostra
para tirar conclusões — use o dataset real.

Uso: python scripts/gerar_amostra.py [pasta_destino] [qtd_pedidos]
"""

from __future__ import annotations

import csv
import random
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

UFS = {
    "SP": ("sao paulo", 40), "RJ": ("rio de janeiro", 13), "MG": ("belo horizonte", 12),
    "RS": ("porto alegre", 6), "PR": ("curitiba", 5), "SC": ("florianopolis", 4),
    "BA": ("salvador", 4), "DF": ("brasilia", 2), "GO": ("goiania", 2), "ES": ("vitoria", 2),
    "PE": ("recife", 2), "CE": ("fortaleza", 1.5), "PA": ("belem", 1), "MT": ("cuiaba", 1),
    "MA": ("sao luis", 0.8), "MS": ("campo grande", 0.7), "PB": ("joao pessoa", 0.6),
    "PI": ("teresina", 0.5), "RN": ("natal", 0.5), "AL": ("maceio", 0.4), "SE": ("aracaju", 0.4),
    "TO": ("palmas", 0.3), "RO": ("porto velho", 0.3), "AM": ("manaus", 0.3),
    "AC": ("rio branco", 0.1), "AP": ("macapa", 0.1), "RR": ("boa vista", 0.1),
}
DISTANCIA = {  # dias extras de entrega por UF, a partir de SP
    "SP": 0, "RJ": 3, "MG": 3, "PR": 3, "SC": 4, "RS": 6, "ES": 5, "DF": 5, "GO": 5,
    "MS": 6, "MT": 8, "BA": 8, "SE": 10, "AL": 10, "PE": 9, "PB": 10, "RN": 10, "CE": 10,
    "PI": 10, "MA": 11, "TO": 9, "PA": 12, "AM": 14, "RO": 12, "AC": 14, "AP": 15, "RR": 16,
}
CATEGORIAS = [
    ("cama_mesa_banho", "bed_bath_table", 90), ("beleza_saude", "health_beauty", 120),
    ("esporte_lazer", "sports_leisure", 110), ("moveis_decoracao", "furniture_decor", 150),
    ("informatica_acessorios", "computers_accessories", 200), ("utilidades_domesticas", "housewares", 80),
    ("relogios_presentes", "watches_gifts", 210), ("telefonia", "telephony", 70),
    ("brinquedos", "toys", 100), ("automotivo", "auto", 130),
]
VENDEDOR_UFS = ["SP"] * 7 + ["MG", "PR", "RJ", "SC", "RS"]


def fmt(d: datetime | None) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else ""


def gerar(destino: Path, qtd_pedidos: int = 3000, seed: int = 42) -> None:
    rnd = random.Random(seed)
    destino.mkdir(parents=True, exist_ok=True)
    uid = lambda: uuid.UUID(int=rnd.getrandbits(128)).hex  # noqa: E731

    vendedores = [(uid(), rnd.choice(VENDEDOR_UFS)) for _ in range(60)]
    produtos = [(uid(), *rnd.choice(CATEGORIAS)) for _ in range(300)]
    ufs, pesos = zip(*[(uf, p) for uf, (_, p) in UFS.items()])

    orders, customers, items, reviews = [], [], [], []
    inicio = datetime(2017, 1, 1)
    for _ in range(qtd_pedidos):
        order_id, customer_id = uid(), uid()
        uf = rnd.choices(ufs, pesos)[0]
        customers.append([customer_id, uid(), f"{rnd.randint(1000, 99999):05d}", UFS[uf][0], uf])

        compra = inicio + timedelta(days=rnd.randint(0, 600), seconds=rnd.randint(0, 86399))
        pico = (compra.month == 11 and compra.day >= 20) or compra.month in (2, 3) and compra.year == 2018
        status = rnd.choices(["delivered", "shipped", "canceled", "unavailable"], [95, 2, 2, 1])[0]

        aprovacao = compra + timedelta(hours=rnd.randint(1, 30))
        envio = aprovacao + timedelta(days=rnd.randint(1, 4)) if status in ("delivered", "shipped") else None
        prazo = 8 + DISTANCIA[uf] + rnd.randint(8, 14)
        prevista = (compra + timedelta(days=prazo)).replace(hour=0, minute=0, second=0)
        entrega = None
        if status == "delivered":
            dias = 3 + DISTANCIA[uf] + rnd.expovariate(1 / 5) + (rnd.uniform(5, 15) if pico else 0)
            entrega = compra + timedelta(days=dias)
        orders.append([order_id, customer_id, status, fmt(compra), fmt(aprovacao),
                       fmt(envio), fmt(entrega), fmt(prevista)])

        for n in range(1, rnd.choices([1, 2, 3], [85, 12, 3])[0] + 1):
            prod_id, _, _, preco_base = rnd.choice(produtos)
            vend_id, vend_uf = rnd.choice(vendedores)
            frete = 12 + DISTANCIA[uf] * 2.5 + rnd.uniform(0, 15)
            items.append([order_id, n, prod_id, vend_id, fmt(compra + timedelta(days=3)),
                          round(preco_base * rnd.uniform(0.5, 1.8), 2), round(frete, 2)])

        if status in ("delivered", "canceled") and rnd.random() < 0.97:
            atraso = entrega is not None and entrega.date() > prevista.date()
            nota = rnd.choices([1, 2, 3, 4, 5], [45, 15, 15, 10, 15] if atraso else [5, 3, 8, 20, 64])[0]
            if status == "canceled":
                nota = rnd.choice([1, 1, 2])
            criada = (entrega or compra) + timedelta(days=1)
            reviews.append([uid(), order_id, nota, "", "", fmt(criada), fmt(criada + timedelta(days=1))])

    def salvar(nome: str, cabecalho: list[str], linhas: list[list]) -> None:
        with open(destino / nome, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cabecalho)
            w.writerows(linhas)

    salvar("olist_orders_dataset.csv",
           ["order_id", "customer_id", "order_status", "order_purchase_timestamp", "order_approved_at",
            "order_delivered_carrier_date", "order_delivered_customer_date", "order_estimated_delivery_date"],
           orders)
    salvar("olist_customers_dataset.csv",
           ["customer_id", "customer_unique_id", "customer_zip_code_prefix", "customer_city", "customer_state"],
           customers)
    salvar("olist_order_items_dataset.csv",
           ["order_id", "order_item_id", "product_id", "seller_id", "shipping_limit_date", "price", "freight_value"],
           items)
    salvar("olist_order_reviews_dataset.csv",
           ["review_id", "order_id", "review_score", "review_comment_title", "review_comment_message",
            "review_creation_date", "review_answer_timestamp"],
           reviews)
    salvar("olist_sellers_dataset.csv",
           ["seller_id", "seller_zip_code_prefix", "seller_city", "seller_state"],
           [[v, f"{rnd.randint(1000, 99999):05d}", UFS.get(u, ("sao paulo",))[0], u] for v, u in vendedores])
    salvar("olist_products_dataset.csv",
           ["product_id", "product_category_name", "product_name_lenght", "product_description_lenght",
            "product_photos_qty", "product_weight_g", "product_length_cm", "product_height_cm", "product_width_cm"],
           [[p, cat, 40, 500, 1, rnd.randint(100, 5000), 20, 10, 15] for p, cat, _, _ in produtos])
    salvar("product_category_name_translation.csv",
           ["product_category_name", "product_category_name_english"],
           [[pt, en] for pt, en, _ in CATEGORIAS])
    print(f"Amostra sintética com {qtd_pedidos} pedidos gravada em {destino}/")


if __name__ == "__main__":
    pasta = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/amostra")
    qtd = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
    gerar(pasta, qtd)
