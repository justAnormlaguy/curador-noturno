"""Etapa 1: encher a fila. Roda no i3, não usa modelo nenhum,
leva segundos. Pode rodar quantas vezes quiser: é idempotente."""

import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

import feedparser
import requests
import yaml

from . import db

RAIZ = Path(__file__).resolve().parent.parent
MAX_TEXTO = 6000  # chars; o resto é ruído para um modelo pequeno
TIMEOUT_FEED = 20  # feedparser sozinho não tem timeout: um feed morto travaria a noite
UA = "curador-noturno/1.0 (homelab; +https://localhost)"


def limpar_html(bruto: str) -> str:
    texto = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", bruto or "", flags=re.S | re.I)
    texto = re.sub(r"<[^>]+>", " ", texto)
    return re.sub(r"\s+", " ", unescape(texto)).strip()


def carregar_fontes(caminho: Path | None = None) -> list[dict]:
    conf = yaml.safe_load(
        (caminho or RAIZ / "config" / "feeds.yaml").read_text(encoding="utf-8")
    )
    fontes = list(conf.get("feeds") or [])
    # Releases do GitHub são só um Atom — não precisa de API nem de token.
    for repo in conf.get("github_releases") or []:
        fontes.append(
            {
                "nome": f"GitHub: {repo}",
                "url": f"https://github.com/{repo}/releases.atom",
                "prioridade": conf.get("prioridade_github", 7),
            }
        )
    return fontes


def data_entrada(entrada) -> str | None:
    for campo in ("published_parsed", "updated_parsed"):
        t = entrada.get(campo)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc).isoformat(timespec="seconds")
    return None


def coletar(con, fontes: list[dict] | None = None, max_por_fonte: int = 25) -> dict:
    fontes = fontes if fontes is not None else carregar_fontes()
    novos = falhas = 0

    with db.Execucao(con, "coletar") as exec_:
        for fonte in fontes:
            try:
                if fonte["url"].startswith("file://"):
                    feed = feedparser.parse(fonte["url"])
                else:
                    r = requests.get(
                        fonte["url"], timeout=TIMEOUT_FEED, headers={"User-Agent": UA}
                    )
                    r.raise_for_status()
                    feed = feedparser.parse(r.content)
                if getattr(feed, "bozo", False) and not feed.entries:
                    raise ValueError(getattr(feed, "bozo_exception", "feed ilegível"))
            except Exception as e:  # feed fora do ar não derruba a noite
                falhas += 1
                print(f"  ! {fonte['nome']}: {e}")
                continue

            contador = 0
            teto = int(fonte.get("max") or max_por_fonte)
            for entrada in feed.entries[:teto]:
                url = entrada.get("link")
                titulo = (entrada.get("title") or "").strip()
                if not url or not titulo:
                    continue
                corpo = ""
                if entrada.get("content"):
                    corpo = entrada["content"][0].get("value", "")
                corpo = corpo or entrada.get("summary", "")
                if db.inserir_item(
                    con,
                    url=url,
                    fonte=fonte["nome"],
                    titulo=titulo,
                    texto=limpar_html(corpo)[:MAX_TEXTO],
                    publicado_em=data_entrada(entrada),
                    prioridade=int(fonte.get("prioridade", 5)),
                ):
                    contador += 1
            con.commit()
            novos += contador
            print(f"  + {fonte['nome']}: {contador} novos")

        exec_.processados, exec_.falhas = novos, falhas

    return {"novos": novos, "fontes_com_falha": falhas}
