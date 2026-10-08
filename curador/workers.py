"""Etapas 2 e 3. Duas passadas propositalmente separadas — é uma cascata:
a triagem lê só título + 400 chars e cospe 20 tokens (rápida);
o resumo, caro, só roda no que sobreviveu. Numa noite típica isso corta
70% do trabalho do modelo."""

from pathlib import Path

import yaml

from . import db, llm

RAIZ = Path(__file__).resolve().parent.parent

SCHEMA_TRIAGEM = {
    "type": "object",
    "properties": {
        "nota": {"type": "integer", "minimum": 0, "maximum": 10},
        "motivo": {"type": "string", "maxLength": 120},
    },
    "required": ["nota", "motivo"],
}

# CUIDADO AO EDITAR: este prompt NÃO define escala de notas.
# A escala inteira vive em config/perfil.yaml. Antes havia uma segunda rubrica
# genérica aqui embaixo ("7-10 = diretamente útil"), depois do perfil — ou
# seja, na posição de maior peso. Duas rubricas competindo, e a genérica
# fazia uma pergunta TEMÁTICA ("é sobre o assunto dele?"), que não separa nada
# quando todas as fontes já são do mesmo domínio. Resultado: nota alta para
# tudo. Se você precisar mudar o critério, mude o perfil.yaml, não este texto.
SYSTEM_TRIAGEM = """Você avalia itens para o digest pessoal de um leitor \
específico. O critério dele está abaixo e é a ÚNICA régua: siga a escala de
notas exatamente como ele a descreve, sem acrescentar critério próprio.

===== CRITÉRIO DO LEITOR =====
{perfil}
===== FIM DO CRITÉRIO =====

Julgue o item pelo que está escrito nele, não pelo que o título promete.
Na dúvida entre duas notas, escolha a menor.
Responda apenas o JSON, com a nota e um motivo de no máximo 12 palavras."""

SYSTEM_RESUMO = """Você resume itens técnicos para um digest matinal.
Escreva EXATAMENTE 3 linhas curtas, em português, sem markdown, sem título:
Fato: descreva objetivamente o que aconteceu.
Técnica: qual vulnerabilidade, ferramenta ou método foi usado.
Relevância: por que isso importa para {perfil_curto}.
Nunca invente número ou versão que não esteja no texto."""


def carregar_perfil(caminho: Path | None = None) -> dict:
    return yaml.safe_load(
        (caminho or RAIZ / "config" / "perfil.yaml").read_text(encoding="utf-8")
    )


def _texto_triagem(item) -> str:
    return (
        f"FONTE: {item['fonte']}\n"
        f"TÍTULO: {item['titulo']}\n"
        f"TRECHO: {(item['texto'] or '')[:400]}"
    )


def triar(con, perfil: dict, limite: int = 200) -> dict:
    limiar = int(perfil.get("limiar_nota", 6))
    system = SYSTEM_TRIAGEM.format(perfil=perfil["descricao"].strip())
    aprovados = 0
    sem_cota = False

    with db.Execucao(con, "triar") as exec_:
        while not sem_cota:
            lote = db.pegar_lote(con, "novo", limite=25)
            if not lote or exec_.processados >= limite:
                break
            for item in lote:
                try:
                    saida, dur = llm.completar(
                        system,
                        _texto_triagem(item),
                        schema=SCHEMA_TRIAGEM,
                        max_tokens=80,
                    )
                    nota = max(0, min(10, int(saida["nota"])))
                    db.marcar_triado(con, item["id"], nota, saida.get("motivo", ""), limiar)
                    db.registrar_chamada(con, item["id"], "triar", llm.MODELO, dur, True)
                    exec_.processados += 1
                    aprovados += nota >= limiar
                    print(
                        f"  [{nota:2d}] {item['titulo'][:60]:60s} {dur:5.1f}s"
                        f"{' ✓' if nota >= limiar else ''}"
                    )
                except llm.ErroCota as e:
                    # Não é falha do item: não gasta tentativa, a fila espera.
                    print(f"  ! {e} — triagem para aqui, o resto fica na fila")
                    sem_cota = True
                    break
                except Exception as e:
                    db.registrar_falha(con, item["id"], str(e))
                    db.registrar_chamada(con, item["id"], "triar", llm.MODELO, 0, False)
                    exec_.falhas += 1
                    print(f"  ! {item['titulo'][:50]}: {e}")

    return {"triados": exec_.processados, "aprovados": aprovados,
            "falhas": exec_.falhas, "sem_cota": sem_cota}


def resumir(con, perfil: dict, limite: int = 40) -> dict:
    system = SYSTEM_RESUMO.format(perfil_curto=perfil.get("resumo_curto", ""))
    sem_cota = False

    with db.Execucao(con, "resumir") as exec_:
        while not sem_cota:
            lote = db.pegar_lote(con, "triado", limite=10)
            if not lote or exec_.processados >= limite:
                break
            for item in lote:
                try:
                    entrada = (
                        f"TÍTULO: {item['titulo']}\n\n"
                        f"TEXTO:\n{(item['texto'] or '')[:3000]}"
                    )
                    saida, dur = llm.completar(system, entrada, max_tokens=220)
                    linhas = [l.strip("-• ") for l in saida.splitlines() if l.strip()][:3]
                    if not linhas:
                        raise llm.ErroLLM("resumo vazio")
                    db.marcar_resumido(con, item["id"], "\n".join(linhas))
                    db.registrar_chamada(con, item["id"], "resumir", llm.MODELO, dur, True)
                    exec_.processados += 1
                    print(f"  ✓ {item['titulo'][:60]:60s} {dur:5.1f}s")
                except llm.ErroCota as e:
                    print(f"  ! {e} — resumo para aqui, o resto fica na fila")
                    sem_cota = True
                    break
                except Exception as e:
                    db.registrar_falha(con, item["id"], str(e))
                    db.registrar_chamada(con, item["id"], "resumir", llm.MODELO, 0, False)
                    exec_.falhas += 1
                    print(f"  ! {item['titulo'][:50]}: {e}")

    return {"resumidos": exec_.processados, "falhas": exec_.falhas,
            "sem_cota": sem_cota}
