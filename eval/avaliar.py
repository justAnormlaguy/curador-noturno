#!/usr/bin/env python3
"""Harness mínimo de avaliação da triagem.

É o que separa "mexi no prompt e achei que melhorou" de "medi".
Rode antes e depois de QUALQUER mudança em perfil.yaml, no prompt
ou no modelo:

    python eval/avaliar.py                 # com o perfil atual
    python eval/avaliar.py --limiar 6      # testa outro corte, sem rechamar

Cada exemplo em exemplos.jsonl tem {titulo, texto, fonte, esperado}
onde `esperado` é true se VOCÊ quer ver aquilo no digest.
Trinta exemplos bastam. Escreva os seus: os daqui são só o formato.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curador import llm, workers  # noqa: E402

AQUI = Path(__file__).resolve().parent


def carregar(caminho: Path) -> list[dict]:
    return [
        json.loads(l) for l in caminho.read_text(encoding="utf-8").splitlines() if l.strip()
    ]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--exemplos", default=str(AQUI / "exemplos.jsonl"))
    p.add_argument("--limiar", type=int, default=None)
    p.add_argument("--cache", default=str(AQUI / ".notas_cache.json"))
    p.add_argument("--refazer", action="store_true")
    args = p.parse_args()

    exemplos = carregar(Path(args.exemplos))
    perfil = workers.carregar_perfil()
    limiar = args.limiar if args.limiar is not None else int(perfil["limiar_nota"])
    system = workers.SYSTEM_TRIAGEM.format(perfil=perfil["descricao"].strip())

    cache_path = Path(args.cache)
    cache = (
        json.loads(cache_path.read_text())
        if cache_path.exists() and not args.refazer
        else {}
    )

    notas, tempos = [], []
    for i, ex in enumerate(exemplos):
        chave = ex["titulo"]
        if chave in cache:
            nota = cache[chave]
        else:
            texto = (
                f"FONTE: {ex.get('fonte', '?')}\nTÍTULO: {ex['titulo']}\n"
                f"TRECHO: {ex.get('texto', '')[:400]}"
            )
            t0 = time.monotonic()
            try:
                saida, _ = llm.completar(
                    system, texto, schema=workers.SCHEMA_TRIAGEM, max_tokens=80
                )
                nota = int(saida["nota"])
            except llm.ErroCota as e:
                # Sem cota: não grava -1 no cache, as notas que faltam são
                # pedidas de novo na próxima rodada.
                print(f"! {e} — rode de novo quando a cota renovar")
                cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
                sys.exit(1)
            except Exception as e:
                print(f"! {ex['titulo'][:50]}: {e}")
                nota = -1
            tempos.append(time.monotonic() - t0)
            cache[chave] = nota
            print(f"  {i + 1}/{len(exemplos)} nota={nota}")
        notas.append(nota)

    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1))

    vp = fp = vn = fn = 0
    erros = []
    for ex, nota in zip(exemplos, notas):
        previsto = nota >= limiar
        if ex["esperado"] and previsto:
            vp += 1
        elif ex["esperado"] and not previsto:
            fn += 1
            erros.append(f"  PERDEU  [{nota}] {ex['titulo'][:65]}")
        elif not ex["esperado"] and previsto:
            fp += 1
            erros.append(f"  RUÍDO   [{nota}] {ex['titulo'][:65]}")
        else:
            vn += 1

    precisao = vp / (vp + fp) if vp + fp else 0.0
    revocacao = vp / (vp + fn) if vp + fn else 0.0
    f1 = 2 * precisao * revocacao / (precisao + revocacao) if precisao + revocacao else 0.0

    print(f"\n=== limiar {limiar} · {len(exemplos)} exemplos ===")
    print(f"precisão  {precisao:.2f}  (do que passou, quanto presta)")
    print(f"revocação {revocacao:.2f}  (do que presta, quanto passou)")
    print(f"F1        {f1:.2f}   [vp={vp} fp={fp} fn={fn} vn={vn}]")

    # Os exemplos marcados "dificil" são os que parecem relevantes e não são:
    # post de fornecedor disfarçado de pesquisa, breach sem detalhe técnico.
    # É neles que um filtro fraco se entrega — a média geral esconde isso.
    dificeis = [(e, n) for e, n in zip(exemplos, notas) if e.get("dificil")]
    if dificeis:
        acertos = sum((n >= limiar) == ex["esperado"] for ex, n in dificeis)
        print(f"\ncasos difíceis: {acertos}/{len(dificeis)} corretos")
        for ex, n in dificeis:
            marca = "ok " if (n >= limiar) == ex["esperado"] else "ERRO"
            print(f"  {marca} [{n}] {ex['titulo'][:56]}")
    if tempos:
        print(f"tempo médio por triagem: {sum(tempos) / len(tempos):.1f}s")
    if erros:
        print("\nonde errou:")
        print("\n".join(erros))
    print(
        "\nDica: num digest, falso positivo custa pouco (você pula a linha) e "
        "falso negativo custa caro (você nunca fica sabendo). Priorize revocação."
    )


if __name__ == "__main__":
    main()
