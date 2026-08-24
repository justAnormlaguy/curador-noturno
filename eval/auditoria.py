#!/usr/bin/env python3
"""Auditoria de relevância: "esse curador entrega só o que me interessa?"

Diferente do avaliar.py, que testa o JULGAMENTO do modelo contra exemplos
rotulados, este script audita o SISTEMA rodando, com os seus dados reais:

  1. Invariante do filtro — nada chega ao digest sem passar pela triagem?
  2. Fontes mudas — algum feed parou de responder e você não percebeu?
  3. Volume e taxa de aprovação por fonte — quem inunda, quem nunca presta?
  4. Duplicatas — a mesma notícia ocupando 3 vagas do digest?
  5. Zona de fronteira — o que ficou logo abaixo e logo acima do limiar?

Rode depois de uma semana de coleta:

    python eval/auditoria.py
    python eval/auditoria.py --banco dados/curador.db --dias 7
"""

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curador import coletor, db, dedup, workers  # noqa: E402

def secao(titulo: str) -> None:
    print(f"\n{'=' * 68}\n{titulo}\n{'=' * 68}")


def checar_invariante(con, limiar: int) -> bool:
    """A pergunta central: existe algum caminho que leva um item ao digest
    sem que ele tenha passado pela triagem?"""
    secao("1. INVARIANTE DO FILTRO")

    vazados = con.execute(
        """SELECT id, nota, status, titulo FROM itens
           WHERE status IN ('resumido','entregue')
             AND (nota IS NULL OR nota < ?)""",
        (limiar,),
    ).fetchall()

    sem_resumo = con.execute(
        """SELECT COUNT(*) c FROM itens
           WHERE status IN ('resumido','entregue')
             AND (resumo IS NULL OR TRIM(resumo) = '')"""
    ).fetchone()["c"]

    if vazados:
        print(f"✗ FALHA: {len(vazados)} itens no digest sem passar do limiar {limiar}")
        for v in vazados[:5]:
            print(f"    nota={v['nota']} status={v['status']} {v['titulo'][:55]}")
    else:
        print(f"✓ Nenhum item com nota < {limiar} chegou ao digest.")

    if sem_resumo:
        print(f"✗ {sem_resumo} itens marcados como resumidos mas sem texto de resumo")
    else:
        print("✓ Todo item entregue tem resumo preenchido.")

    return not vazados and not sem_resumo


def checar_fontes_mudas(con, dias: int) -> None:
    """Um feed que morreu não dá erro visível: ele só para de trazer coisa.
    É a falha de relevância mais silenciosa que existe."""
    secao("2. FONTES MUDAS")

    corte = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
    ativas = {
        l["fonte"]
        for l in con.execute(
            "SELECT DISTINCT fonte FROM itens WHERE coletado_em >= ?", (corte,)
        )
    }
    try:
        configuradas = {f["nome"] for f in coletor.carregar_fontes()}
    except Exception as e:
        print(f"não consegui ler feeds.yaml: {e}")
        return

    mudas = sorted(configuradas - ativas)
    if mudas:
        print(f"✗ {len(mudas)} fontes sem nenhum item em {dias} dias:")
        for m in mudas:
            print(f"    {m}")
        print("  → rode 'curador coletar' e veja se aparece erro para elas")
    else:
        print(f"✓ Todas as {len(configuradas)} fontes trouxeram itens em {dias} dias.")

    orfas = sorted(ativas - configuradas)
    if orfas:
        print(f"\n  ({len(orfas)} fontes no banco não estão mais no feeds.yaml — histórico)")


def relatorio_por_fonte(con, limiar: int, dias: int) -> None:
    """Quem gasta o FX sem entregar nada é candidato a sair do feeds.yaml."""
    secao("3. VOLUME E APROVAÇÃO POR FONTE")

    corte = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
    linhas = con.execute(
        """SELECT fonte,
                  COUNT(*) coletados,
                  SUM(CASE WHEN nota >= ? THEN 1 ELSE 0 END) aprovados,
                  ROUND(AVG(nota), 1) nota_media
           FROM itens
           WHERE coletado_em >= ? AND nota IS NOT NULL
           GROUP BY fonte ORDER BY coletados DESC""",
        (limiar, corte),
    ).fetchall()

    if not linhas:
        print("sem itens triados no período.")
        return

    print(f"{'fonte':<34}{'triados':>8}{'aprov':>7}{'taxa':>7}{'nota méd':>10}")
    print("-" * 68)
    total_c = total_a = 0
    for l in linhas:
        taxa = l["aprovados"] / l["coletados"] if l["coletados"] else 0
        total_c += l["coletados"]
        total_a += l["aprovados"]
        alerta = ""
        if l["coletados"] >= 10 and taxa == 0:
            alerta = "  ← nunca aprova"
        elif taxa > 0.8 and l["coletados"] >= 10:
            alerta = "  ← aprova quase tudo"
        print(
            f"{l['fonte'][:33]:<34}{l['coletados']:>8}{l['aprovados']:>7}"
            f"{taxa:>6.0%}{l['nota_media']:>10}{alerta}"
        )
    print("-" * 68)
    print(
        f"{'TOTAL':<34}{total_c:>8}{total_a:>7}"
        f"{(total_a / total_c if total_c else 0):>6.0%}"
    )
    print(
        "\nLeitura: 'nunca aprova' = custa FX toda noite e não entrega — tire do "
        "feeds.yaml.\n'aprova quase tudo' = ou é uma fonte excelente, ou o "
        "limiar está frouxo para ela."
    )


def checar_duplicatas(con, limiar: int, dias: int) -> None:
    """Duas perguntas distintas: quantas a dedup PEGOU (valor entregue) e
    quantas ESCAPARAM (limiar de similaridade frouxo demais)."""
    secao("4. DUPLICATAS ENTRE FONTES")

    corte = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()

    pegas = con.execute(
        "SELECT COUNT(*) c FROM itens WHERE status='duplicado' AND coletado_em >= ?",
        (corte,),
    ).fetchone()["c"]
    grupos_pegos = con.execute(
        """SELECT COUNT(DISTINCT duplicado_de) c FROM itens
           WHERE status='duplicado' AND coletado_em >= ?""",
        (corte,),
    ).fetchone()["c"]
    print(
        f"✓ dedup agrupou {pegas} itens em {grupos_pegos} assuntos "
        f"({pegas} resumos de FX economizados)"
    )

    # Escaparam: itens que chegaram ao digest e ainda assim se parecem.
    itens = con.execute(
        """SELECT * FROM itens
           WHERE coletado_em >= ? AND status IN ('resumido','entregue')
           ORDER BY id""",
        (corte,),
    ).fetchall()

    escapou, usados = [], set()
    for i, a in enumerate(itens):
        for b in itens[i + 1 :]:
            if b["id"] in usados:
                continue
            motivo = dedup.parecidos(a, b)
            if motivo:
                escapou.append((a, b, motivo))
                usados.add(b["id"])

    if not escapou:
        print("✓ Nenhuma duplicata escapou para o digest.")
        return

    print(f"\n✗ {len(escapou)} pares escaparam da dedup:")
    for a, b, motivo in escapou:
        print(f"  [{a['nota']}] {a['titulo'][:52]}  ({a['fonte']})")
        print(f"   ↳ [{b['nota']}] {b['titulo'][:50]}  ({b['fonte']}) — {motivo}")
    print(
        "\n  → se forem assuntos realmente iguais, baixe LIMIAR_SIMILARIDADE\n"
        "    em curador/dedup.py (hoje 0.45). Cuidado: baixo demais funde\n"
        "    notícias diferentes sobre o mesmo produto."
    )


def zona_de_fronteira(con, limiar: int, dias: int) -> None:
    """O olho humano decide melhor que qualquer métrica aqui: se você discorda
    de uma linha, ela vira exemplo no exemplos.jsonl."""
    secao("5. ZONA DE FRONTEIRA (revise à mão)")

    corte = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
    print(f"— Ficaram DE FORA por pouco (nota {limiar - 2} e {limiar - 1}):")
    for l in con.execute(
        """SELECT nota, titulo, motivo FROM itens
           WHERE coletado_em >= ? AND nota BETWEEN ? AND ?
           ORDER BY nota DESC LIMIT 8""",
        (corte, limiar - 2, limiar - 1),
    ):
        print(f"  [{l['nota']}] {l['titulo'][:56]}")
        print(f"       motivo: {(l['motivo'] or '')[:60]}")

    print(f"\n— Entraram raspando (nota {limiar}):")
    for l in con.execute(
        """SELECT nota, titulo, motivo FROM itens
           WHERE coletado_em >= ? AND nota = ? LIMIT 8""",
        (corte, limiar),
    ):
        print(f"  [{l['nota']}] {l['titulo'][:56]}")
        print(f"       motivo: {(l['motivo'] or '')[:60]}")

    print(
        "\nDiscordou de alguma? Copie o título para eval/exemplos.jsonl com o\n"
        "rótulo que você queria e rode eval/avaliar.py. É assim que o filtro melhora."
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--banco", default=None)
    p.add_argument("--dias", type=int, default=7)
    p.add_argument("--limiar", type=int, default=None)
    args = p.parse_args()

    con = db.conectar(args.banco)
    perfil = workers.carregar_perfil()
    limiar = args.limiar if args.limiar is not None else int(perfil["limiar_nota"])

    print(f"AUDITORIA DE RELEVÂNCIA · limiar {limiar} · últimos {args.dias} dias")
    print(f"itens por status: {db.estatisticas(con)}")

    ok = checar_invariante(con, limiar)
    checar_fontes_mudas(con, args.dias)
    relatorio_por_fonte(con, limiar, args.dias)
    checar_duplicatas(con, limiar, args.dias)
    zona_de_fronteira(con, limiar, args.dias)

    secao("VEREDITO")
    print("Invariante do filtro:", "OK" if ok else "FALHOU — veja a seção 1")
    print(
        "As seções 2-5 não têm resposta automática: elas apontam onde olhar.\n"
        "Relevância é julgamento seu; o script só torna o julgamento barato."
    )


if __name__ == "__main__":
    main()
