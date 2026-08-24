"""Interface única. Cada subcomando é uma etapa isolada e re-executável —
você pode rodar qualquer uma na mão para depurar sem refazer as outras."""

import argparse
import os
import sys

from . import coletor, db, dedup, digest, energia, llm, workers


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


def cmd_acordar(args):
    sys.exit(0 if energia.acordar() else 1)


def cmd_dormir(args):
    energia.dormir()


def cmd_checar_ssh(args):
    sys.exit(0 if energia.checar_ssh() else 1)


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

    FX_GERENCIA_ENERGIA controla as duas etapas de energia:
      sim (padrão) — WoL para acordar, SSH para desligar no fim
      nao          — assume o FX já ligado (tomada manual, por exemplo)

    Com 'nao' o pipeline não aborta por WoL falho nem desliga uma máquina que
    você controla por fora. Ele ainda checa /health antes de gastar a noite,
    porque falhar aqui em 5 segundos é melhor que falhar item a item por
    timeout ao longo de uma hora.
    """
    gerencia = os.environ.get("FX_GERENCIA_ENERGIA", "sim").strip().lower()
    gerencia = gerencia not in ("nao", "não", "no", "0", "false")

    con = db.conectar(args.banco)
    db.inicializar(con)

    print("== coleta ==")
    coletor.coletar(con)

    if not db.ha_trabalho_pendente(con):
        print("nada a processar; nada a fazer no FX.")
        return

    if gerencia:
        print("== acordando FX ==")
        if not energia.acordar():
            print("abortando: FX indisponível. O estado fica na fila para amanhã.")
            sys.exit(1)
    else:
        print("== energia manual: conferindo se o FX está de pé ==")
        if not llm.esta_vivo():
            print(
                f"abortando: {llm.BASE_URL} não responde.\n"
                "Ligue o FX (tomada) e rode 'curador noite' de novo — a fila "
                "está preservada."
            )
            sys.exit(1)
        print("FX respondendo.")

    perfil = workers.carregar_perfil()
    try:
        print("== triagem ==")
        workers.triar(con, perfil, limite=args.limite)
        # Sem modelo e antes do resumo: cada duplicata pega aqui é um resumo
        # caro que o FX não precisa gerar.
        print("== deduplicação ==")
        dedup.deduplicar(con)
        print("== resumo ==")
        workers.resumir(con, perfil, limite=args.limite)
    finally:
        # Mesmo se o worker explodir, o FX não passa a noite ligado.
        if gerencia and not args.manter_ligado:
            print("== desligando FX ==")
            energia.dormir()
        elif not gerencia:
            print("== energia manual: desligue o FX quando quiser ==")

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
    add("acordar", cmd_acordar, "Wake-on-LAN no FX")
    add("dormir", cmd_dormir, "desliga o FX")
    add("checar-ssh", cmd_checar_ssh, "testa o SSH para o FX sem desligar nada")
    add("status", cmd_status, "fila e tempos medidos")
    n = add("noite", cmd_noite, "pipeline completo da madrugada")
    n.add_argument("--limite", type=int, default=200)
    n.add_argument("--manter-ligado", action="store_true")

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
