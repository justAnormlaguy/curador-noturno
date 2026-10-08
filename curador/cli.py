"""Interface única. Cada subcomando é uma etapa isolada e re-executável —
você pode rodar qualquer uma na mão para depurar sem refazer as outras."""

import argparse
import sys

from . import coletor, db, dedup, digest, llm, workers


def cmd_init(args):
    con = db.conectar(args.banco)
    db.inicializar(con)
    print(f"banco pronto em {args.banco or db.CAMINHO_BANCO}")


def cmd_coletar(args):
    con = db.conectar(args.banco)
    print(coletor.coletar(con))


def cmd_triar(args):
    con = db.conectar(args.banco)
    print(workers.triar(con, workers.carregar_perfil(), limite=args.limite))


def cmd_deduplicar(args):
    con = db.conectar(args.banco)
    print(dedup.deduplicar(con))


def cmd_resumir(args):
    con = db.conectar(args.banco)
    print(workers.resumir(con, workers.carregar_perfil(), limite=args.limite))


def cmd_digest(args):
    con = db.conectar(args.banco)
    if args.seco:
        texto, ids = digest.montar(con)
        print(texto)
        print(f"\n-- {len(ids)} itens (nada foi marcado como entregue)")
    else:
        print(digest.entregar(con))


def cmd_checar_api(args):
    """Confere chave e modelo e faz uma chamada mínima de geração. Rode ANTES
    de automatizar: chave errada só apareceria às 23h30, item a item."""
    if not llm.configurado():
        print("✗ GEMINI_API_KEY não definida (veja o .env)")
        sys.exit(1)
    if not llm.esta_vivo():
        print(f"✗ chave ou modelo inválido: {llm.MODELO} em {llm.API_URL}")
        sys.exit(1)
    try:
        saida, dur = llm.completar(
            "Responda apenas o JSON.", 'Devolva {"ok": true}.',
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"]},
            max_tokens=20,
        )
    except llm.ErroLLM as e:
        print(f"✗ chamada falhou: {e}")
        sys.exit(1)
    print(f"✓ {llm.MODELO} respondeu {saida} em {dur:.1f}s")


def cmd_status(args):
    con = db.conectar(args.banco)
    print("itens por status:", db.estatisticas(con))
    for l in con.execute(
        """SELECT etapa, COUNT(*) n, ROUND(AVG(duracao_s),1) media_s,
                  ROUND(SUM(duracao_s)/60,1) total_min
           FROM chamadas WHERE ok=1 GROUP BY etapa"""
    ):
        print(
            f"  {l['etapa']:9s} {l['n']:4d} chamadas · "
            f"{l['media_s']}s em média · {l['total_min']} min no total"
        )


def cmd_noite(args):
    """O pipeline inteiro. É isso que o systemd chama às 23h30.

    Checa a API antes de gastar a noite: falhar aqui em 5 segundos é melhor
    que falhar item a item ao longo de uma hora. Se a cota diária acabar no
    meio, triagem/resumo param e o que sobrou fica na fila para amanhã.
    """
    con = db.conectar(args.banco)
    db.inicializar(con)

    print("== coleta ==")
    coletor.coletar(con)

    if not db.ha_trabalho_pendente(con):
        print("nada a processar; nenhuma chamada à API.")
        return

    print("== conferindo a API do Gemini ==")
    if not llm.esta_vivo():
        print(
            f"abortando: API do Gemini ({llm.MODELO}) não respondeu — confira "
            "GEMINI_API_KEY e a rede. A fila fica preservada para amanhã."
        )
        sys.exit(1)

    perfil = workers.carregar_perfil()
    print("== triagem ==")
    workers.triar(con, perfil, limite=args.limite)
    # Sem modelo e antes do resumo: cada duplicata pega aqui é um resumo
    # que não gasta cota da API.
    print("== deduplicação ==")
    dedup.deduplicar(con)
    print("== resumo ==")
    workers.resumir(con, perfil, limite=args.limite)

    print("== status ==")
    cmd_status(args)


def main(argv=None):
    p = argparse.ArgumentParser(prog="curador", description="Curador noturno")
    p.add_argument("--banco", default=None, help="caminho do SQLite")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(nome, fn, ajuda):
        s = sub.add_parser(nome, help=ajuda)
        s.set_defaults(func=fn)
        return s

    add("init", cmd_init, "cria o banco")
    add("coletar", cmd_coletar, "busca os feeds")
    add("triar", cmd_triar, "pontua os itens novos").add_argument(
        "--limite", type=int, default=200
    )
    add("deduplicar", cmd_deduplicar, "agrupa a mesma história entre fontes")
    add("resumir", cmd_resumir, "resume os aprovados").add_argument(
        "--limite", type=int, default=40
    )
    d = add("digest", cmd_digest, "monta e envia o digest")
    d.add_argument("--seco", action="store_true", help="só imprime, não envia")
    add("checar-api", cmd_checar_api, "testa a chave e o modelo do Gemini")
    add("status", cmd_status, "fila e tempos medidos")
    n = add("noite", cmd_noite, "pipeline completo da madrugada")
    n.add_argument("--limite", type=int, default=200)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
