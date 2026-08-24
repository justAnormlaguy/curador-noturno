"""Etapa 4: montar o digest e entregar. Roda no i3 às 7h,
independente do processamento — se a noite falhou, o digest sai
com o que deu certo em vez de não sair."""

import os
from collections import defaultdict
from datetime import datetime
from html import escape

import requests

from . import db

LIMITE_TELEGRAM = 4000


def selecionar(con, horas: int, maximo: int, max_por_fonte: int):
    """Escolhe o que entra no digest de hoje.

    Três correções em relação à primeira versão, que perdia itens em silêncio:

    1. Ordem `nota DESC, coletado_em ASC`. Antes era só nota, e como quase
       todo aprovado tira 9, o desempate virava alfabético por fonte: um item
       bom podia ser espremido por notícia nova para sempre. Agora, entre
       notas iguais, o mais velho passa na frente e a fila anda.
    2. Teto por fonte, para uma fonte de alto volume não tomar o digest todo.
    3. O que sobra continua 'resumido' e é reportado no rodapé — o leitor
       sabe que existe fila, em vez de o item sumir sem aviso.
    """
    candidatos = con.execute(
        """SELECT * FROM itens
           WHERE status='resumido'
           ORDER BY nota DESC, coletado_em ASC""",
    ).fetchall()

    escolhidos, por_fonte = [], defaultdict(int)
    for item in candidatos:                       # 1a passada: respeita o teto
        if len(escolhidos) >= maximo:
            break
        if por_fonte[item["fonte"]] < max_por_fonte:
            escolhidos.append(item)
            por_fonte[item["fonte"]] += 1

    if len(escolhidos) < maximo:                  # 2a passada: completa as vagas
        ids = {i["id"] for i in escolhidos}
        for item in candidatos:
            if len(escolhidos) >= maximo:
                break
            if item["id"] not in ids:
                escolhidos.append(item)

    return escolhidos, max(0, len(candidatos) - len(escolhidos))


def montar(con, horas: int = 72, maximo: int = 15, max_por_fonte: int = 4):
    expirados = db.expirar_antigos(con, horas)
    itens, na_fila = selecionar(con, horas, maximo, max_por_fonte)

    hoje = datetime.now().strftime("%d/%m")
    if not itens:
        return f"<b>Curadoria {hoje}</b>\n\nNada relevante hoje. Bom sinal.", []

    partes = [f"<b>Curadoria {hoje}</b> — {len(itens)} itens\n"]
    for item in itens:
        resumo = "\n".join(
            escape(l) for l in (item["resumo"] or "").splitlines() if l.strip()
        )
        # Mostrar quem mais cobriu o assunto prova que a dedup trabalhou —
        # e às vezes a segunda fonte é a que você prefere ler.
        outras = db.fontes_duplicadas(con, item["id"])
        eco = (
            f"\n<i>também em: {escape(', '.join(outras[:3]))}</i>" if outras else ""
        )
        partes.append(
            f"\n<b>[{item['nota']}]</b> <a href=\"{escape(item['url'], quote=True)}\">"
            f"{escape(item['titulo'][:110])}</a>\n"
            f"<i>{escape(item['fonte'])}</i>{eco}\n{resumo}\n"
        )

    stats = db.estatisticas(con)
    rodape = [
        f"{stats.get('descartado', 0)} descartados",
        f"{stats.get('duplicado', 0)} duplicatas",
    ]
    if na_fila:
        rodape.append(f"{na_fila} na fila para amanhã")
    if expirados:
        rodape.append(f"{expirados} expiraram")
    if stats.get("erro"):
        rodape.append(f"{stats['erro']} com erro")
    partes.append(f"\n<i>{' · '.join(rodape)}</i>")

    return "".join(partes), [i["id"] for i in itens]


def _fatiar(texto: str) -> list[str]:
    """Telegram corta em 4096 chars. Quebra em parágrafo, não no meio do link."""
    blocos, atual = [], ""
    for paragrafo in texto.split("\n\n"):
        if len(atual) + len(paragrafo) + 2 > LIMITE_TELEGRAM and atual:
            blocos.append(atual)
            atual = ""
        atual += paragrafo + "\n\n"
    if atual.strip():
        blocos.append(atual)
    return blocos


def enviar_telegram(texto: str) -> bool:
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_TOKEN/TELEGRAM_CHAT_ID ausentes — imprimindo:\n")
        print(texto)
        return False

    ok = True
    for bloco in _fatiar(texto):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": bloco,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"! falha ao enviar: {e}")
            ok = False
    return ok


def entregar(con, horas: int = 72) -> dict:
    with db.Execucao(con, "entregar") as exec_:
        texto, ids = montar(con, horas=horas)
        # Só marca como entregue se o Telegram confirmou: senão o próximo
        # ciclo tenta de novo em vez de perder o digest.
        if enviar_telegram(texto) and ids:
            db.marcar_entregue(con, ids)
            exec_.processados = len(ids)
        return {"itens": len(ids)}
