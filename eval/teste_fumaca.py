#!/usr/bin/env python3
"""Teste de fumaça: roda o pipeline inteiro com um modelo falso.

Serve para validar coleta, idempotência, máquina de estados, retry,
montagem do digest e o cliente do Gemini (HTTP simulado) SEM chave e sem
internet. Rode sempre que mexer no código:

    python eval/teste_fumaca.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curador import coletor, db, dedup, digest, llm, workers  # noqa: E402

FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <title>Teste</title>
 <entry><title>llama.cpp ganha KV cache quantizado</title>
  <link href="https://exemplo.test/1"/><updated>2026-08-16T10:00:00Z</updated>
  <content type="html">&lt;p&gt;--cache-type-k q4_0 corta a RAM pela metade.&lt;/p&gt;</content></entry>
 <entry><title>Startup levanta 300 milhoes em serie C</title>
  <link href="https://exemplo.test/2"/><updated>2026-08-16T11:00:00Z</updated>
  <content type="html">Rodada liderada por fundo europeu.</content></entry>
 <entry><title>Wake-on-LAN em placas AMD antigas</title>
  <link href="https://exemplo.test/3"/><updated>2026-08-16T12:00:00Z</updated>
  <content type="html">Parametro do driver r8169 e opcoes de BIOS.</content></entry>
 <entry><title>ITEM QUE SEMPRE FALHA</title>
  <link href="https://exemplo.test/4"/><updated>2026-08-16T13:00:00Z</updated>
  <content type="html">Serve para testar o retry.</content></entry>
 <entry><title>llama.cpp recebe correcao de KV cache quantizado</title>
  <link href="https://exemplo.test/5"/><updated>2026-08-16T14:00:00Z</updated>
  <content type="html">Outra fonte cobrindo o mesmo assunto.</content></entry>
 <entry><title>Wake-on-LAN quebra em placas AMD antigas, diz relato</title>
  <link href="https://exemplo.test/6"/><updated>2026-08-16T15:00:00Z</updated>
  <content type="html">CVE-2026-9999 mencionada aqui e no item 3.</content></entry>
</feed>
"""

RELEVANTES = ("llama", "wake-on-lan")


def modelo_falso(system, user, *, schema=None, max_tokens=300, temperatura=0.1):
    if "SEMPRE FALHA" in user:
        raise llm.ErroLLM("timeout simulado")
    if schema:
        nota = 9 if any(p in user.lower() for p in RELEVANTES) else 2
        return {"nota": nota, "motivo": "teste"}, 0.01
    return "Aconteceu X.\nDetalhe tecnico Y.\nImporta porque Z.", 0.01


class _RespostaFalsa:
    def __init__(self, status, corpo):
        self.status_code = status
        self._corpo = corpo
        self.text = json.dumps(corpo)

    def json(self):
        return self._corpo


def testar_cliente_gemini(completar_real):
    """Formato da requisição, parse da resposta e 429 de cota diária."""
    import requests

    post_real = requests.post
    chamadas, respostas = [], []

    def post_falso(url, json=None, timeout=None, headers=None):
        chamadas.append({"url": url, "json": json, "headers": headers})
        return respostas.pop(0)

    chave_real, rpm_real = llm.API_KEY, llm.RPM
    llm.API_KEY, llm.RPM = "chave-teste", 0
    requests.post = post_falso
    try:
        # triagem: JSON com schema; partes de raciocínio são ignoradas
        respostas.append(_RespostaFalsa(200, {"candidates": [{"content": {"parts": [
            {"text": "pensando...", "thought": True},
            {"text": '{"nota": 8, "motivo": "exploit publico"}'},
        ]}}]}))
        saida, _ = completar_real(
            "sys", "user", schema=workers.SCHEMA_TRIAGEM, max_tokens=80)
        assert saida == {"nota": 8, "motivo": "exploit publico"}, saida
        c = chamadas[-1]
        assert c["url"].endswith(f"/models/{llm.MODELO}:generateContent"), c["url"]
        assert c["headers"]["x-goog-api-key"] == "chave-teste"
        gc = c["json"]["generationConfig"]
        assert gc["responseMimeType"] == "application/json"
        assert gc["responseJsonSchema"] == workers.SCHEMA_TRIAGEM
        assert c["json"]["systemInstruction"]["parts"][0]["text"] == "sys"

        # resumo: texto puro
        respostas.append(_RespostaFalsa(200, {"candidates": [{"content": {"parts": [
            {"text": "Fato: a.\nTécnica: b.\nRelevância: c.\n"}]}}]}))
        texto, _ = completar_real("sys", "user", max_tokens=220)
        assert texto.splitlines()[0] == "Fato: a.", texto
        assert "responseMimeType" not in chamadas[-1]["json"]["generationConfig"]

        # 429 de cota diária → ErroCota, sem retentar
        respostas.append(_RespostaFalsa(429, {"error": {
            "message": "Quota exceeded for GenerateRequestsPerDayPerProjectPerModel"}}))
        try:
            completar_real("sys", "user")
            raise AssertionError("esperava ErroCota")
        except llm.ErroCota:
            pass
        assert not respostas
    finally:
        requests.post = post_real
        llm.API_KEY, llm.RPM = chave_real, rpm_real
    print("   cliente Gemini ok")


def testar_cota_esgotada(tmp: Path, fontes: list, perfil: dict):
    """Cota acaba no meio da triagem: para sem queimar tentativa e a fila
    fica intacta para a noite seguinte."""
    def modelo_sem_cota(*a, **k):
        raise llm.ErroCota("cota diária do Gemini esgotada")

    llm.completar = modelo_sem_cota
    con = db.conectar(tmp / "teste_cota.db")
    db.inicializar(con)
    coletor.coletar(con, fontes)
    t = workers.triar(con, perfil)
    assert t["sem_cota"] and t["triados"] == 0 and t["falhas"] == 0, t
    assert db.estatisticas(con).get("novo") == 6
    assert con.execute(
        "SELECT COUNT(*) FROM itens WHERE tentativas > 0").fetchone()[0] == 0, (
        "cota esgotada não pode gastar tentativa do item"
    )
    llm.completar = modelo_falso
    print("   cota esgotada ok")


def main():
    completar_real = llm.completar
    llm.completar = modelo_falso
    tmp = Path(tempfile.mkdtemp())
    (tmp / "feed.atom").write_text(FEED, encoding="utf-8")
    fontes = [{"nome": "Teste", "url": (tmp / "feed.atom").as_uri()}]

    con = db.conectar(tmp / "teste.db")
    db.inicializar(con)
    perfil = workers.carregar_perfil()

    print("-- coleta --")
    r1 = coletor.coletar(con, fontes)
    print("-- coleta de novo (idempotência) --")
    r2 = coletor.coletar(con, fontes)
    assert r1["novos"] == 6, r1
    assert r2["novos"] == 0, r2

    print("-- triagem --")
    t = workers.triar(con, perfil)
    assert t["aprovados"] == 4, t
    assert t["falhas"] == 1, t

    # FALHA 1 CORRIGIDA: 4 aprovados viram 2 assuntos.
    print("-- deduplicação --")
    d = dedup.deduplicar(con)
    assert d["duplicados"] == 2 and d["grupos"] == 2, d

    print("-- resumo --")
    s = workers.resumir(con, perfil)
    assert s["resumidos"] == 2, f"duplicata foi resumida à toa: {s}"

    print("-- digest --")
    texto, ids = digest.montar(con)
    assert len(ids) == 2 and "llama.cpp" in texto
    assert "também em" in texto, "eco da dedup não apareceu no digest"
    print(texto)

    # FALHA 3 CORRIGIDA: com teto de 1, o excedente NÃO some — fica na fila
    # e o rodapé avisa.
    print("-- teto do digest e fila --")
    con.execute("UPDATE itens SET status='resumido' WHERE status='entregue'")
    con.commit()
    texto_teto, ids_teto = digest.montar(con, maximo=1)
    assert len(ids_teto) == 1
    assert "na fila para amanhã" in texto_teto, texto_teto[-200:]
    print("   rodapé:", texto_teto.splitlines()[-1])

    st = db.estatisticas(con)
    print("\nstatus:", st)
    assert st["duplicado"] == 2, st
    assert st["descartado"] == 1
    # o item que falhou continua 'novo', agendado para retry
    assert st["novo"] == 1
    linha = con.execute(
        "SELECT tentativas, proxima_tentativa FROM itens WHERE titulo LIKE '%FALHA%'"
    ).fetchone()
    assert linha["tentativas"] == 1 and linha["proxima_tentativa"]
    print("retry agendado para", linha["proxima_tentativa"])

    db.marcar_entregue(con, ids)
    assert db.estatisticas(con).get("entregue") == 2

    # FALHA 2 CORRIGIDA: a escala de notas vem só do perfil.yaml.
    prompt = workers.SYSTEM_TRIAGEM.format(perfil=perfil["descricao"].strip())
    assert "ESCALA DE NOTAS" in prompt, "perfil não carrega a escala"
    assert "diretamente útil para o que ele faz" not in prompt, (
        "rubrica genérica voltou a competir com a do perfil"
    )

    print("\n-- cliente Gemini (HTTP simulado) --")
    testar_cliente_gemini(completar_real)
    print("-- cota esgotada --")
    testar_cota_esgotada(tmp, fontes, perfil)
    print("\nTUDO OK ✓")


if __name__ == "__main__":
    main()
