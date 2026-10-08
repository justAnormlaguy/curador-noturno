"""Etapa 2.5 — deduplicação entre fontes.

A mesma falha sai no BleepingComputer, no The Hacker News e no The Record.
Três URLs distintas, três notas 9, três vagas do seu digest gastas no mesmo
assunto. O hash de URL não pega isso porque as URLs são realmente diferentes.

Este módulo NÃO usa modelo. É o caso do "isso precisa mesmo que o modelo
*escreva* algo?" — a resposta é não, e por isso roda em milissegundos no i3
sem chamar a API. Dois sinais bastam:

  1. CVE em comum — sinal forte e praticamente sem falso positivo no domínio
     de segurança. Dois textos que citam CVE-2026-4041 falam da mesma coisa.
  2. Similaridade de título por Jaccard sobre palavras de conteúdo — pega o
     caso sem CVE ("Grupo APT explora dia zero em cliente de e-mail").

O item canônico do grupo é o de maior (nota, prioridade da fonte, tamanho do
texto). Os outros viram status 'duplicado' e NUNCA são resumidos — o que
economiza cota da API, não só espaço no digest.
"""

import re
from datetime import datetime, timedelta, timezone

from . import db

RE_CVE = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)
LIMIAR_SIMILARIDADE = 0.45
JANELA_ANCORA_H = 72  # compara também com o que já foi entregue nos últimos 3 dias

# Palavras que aparecem em metade dos títulos de segurança e por isso não
# ajudam a distinguir um do outro.
VAZIAS = {
    "a", "o", "as", "os", "de", "da", "do", "das", "dos", "em", "no", "na",
    "para", "por", "com", "que", "e", "um", "uma", "ao", "aos", "sobre",
    "the", "of", "to", "in", "and", "for", "new", "how",
    "nova", "novo", "novas", "novos", "falha", "falhas", "vulnerabilidade",
    "vulnerabilidades", "ataque", "ataques", "seguranca", "segurança",
    "hackers", "atacantes", "pesquisadores", "critica", "crítica",
}


def normalizar(titulo: str) -> set[str]:
    palavras = re.findall(r"\w+", (titulo or "").lower())
    return {p for p in palavras if len(p) > 3 and p not in VAZIAS}


def cves(item) -> set[str]:
    alvo = f"{item['titulo']} {item['texto'] or ''}"
    return {m.upper() for m in RE_CVE.findall(alvo)}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a | b) else 0.0


def parecidos(a, b) -> str | None:
    """Retorna o motivo da duplicidade, ou None."""
    comuns = cves(a) & cves(b)
    if comuns:
        return f"mesma {sorted(comuns)[0]}"
    sim = jaccard(normalizar(a["titulo"]), normalizar(b["titulo"]))
    if sim >= LIMIAR_SIMILARIDADE:
        return f"títulos {sim:.0%} iguais"
    return None


def _peso(item) -> tuple:
    """Critério do canônico: nota primeiro; empatou, a fonte melhor;
    empatou de novo, o texto mais longo (mais detalhe técnico)."""
    return (
        item["nota"] or 0,
        item["prioridade"] or 5,
        len(item["texto"] or ""),
        -item["id"],
    )


def deduplicar(con) -> dict:
    """Roda sobre os itens 'triado'. Compara primeiro com o que já foi
    entregue (para não repetir a notícia de ontem) e depois entre si."""
    with db.Execucao(con, "deduplicar") as exec_:
        candidatos = list(
            con.execute("SELECT * FROM itens WHERE status='triado' ORDER BY id")
        )
        if not candidatos:
            return {"duplicados": 0, "grupos": 0}

        corte = (
            datetime.now(timezone.utc) - timedelta(hours=JANELA_ANCORA_H)
        ).isoformat(timespec="seconds")
        ancoras = list(
            con.execute(
                """SELECT * FROM itens
                   WHERE status IN ('resumido','entregue') AND coletado_em >= ?""",
                (corte,),
            )
        )

        duplicados, grupos = 0, 0
        restantes = []

        # 1. contra o que você já recebeu nos últimos 3 dias
        for item in candidatos:
            achou = next(
                ((a, m) for a in ancoras if (m := parecidos(item, a))), None
            )
            if achou:
                ancora, motivo = achou
                db.marcar_duplicado(con, item["id"], ancora["id"])
                duplicados += 1
                print(f"  ↳ já coberto ({motivo}): {item['titulo'][:52]}")
            else:
                restantes.append(item)

        # 2. entre os candidatos desta noite
        usados = set()
        for i, a in enumerate(restantes):
            if a["id"] in usados:
                continue
            grupo = [a]
            for b in restantes[i + 1 :]:
                if b["id"] not in usados and parecidos(a, b):
                    grupo.append(b)
                    usados.add(b["id"])
            if len(grupo) > 1:
                usados.add(a["id"])
                grupos += 1
                canonico = max(grupo, key=_peso)
                print(f"  agrupado: {canonico['titulo'][:52]}")
                for item in grupo:
                    if item["id"] != canonico["id"]:
                        db.marcar_duplicado(con, item["id"], canonico["id"])
                        duplicados += 1
                        print(f"    ↳ {item['fonte']}: {item['titulo'][:46]}")

        exec_.processados = duplicados
        economia = duplicados
        print(
            f"  {duplicados} duplicatas em {grupos} grupos "
            f"({economia} chamadas de resumo economizadas)"
        )
        return {"duplicados": duplicados, "grupos": grupos}
