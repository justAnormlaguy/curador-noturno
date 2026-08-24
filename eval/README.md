# Ferramentas de avaliação

Três scripts com propósitos diferentes. Confundi-los é a maneira mais fácil
de achar que o sistema está bom quando não está.

| script | pergunta que responde | precisa do FX? |
|---|---|---|
| `teste_fumaca.py` | O código funciona? | não |
| `avaliar.py` | O **modelo** julga bem? | sim |
| `auditoria.py` | O **sistema rodando** entrega só o relevante? | não |

## teste_fumaca.py

Roda o pipeline inteiro com um modelo falso em 1 segundo. Valida coleta,
idempotência, retry com backoff, dedup, teto do digest e fila. Rode a cada
alteração de código.

Ele também trava as três correções: se alguém devolver a rubrica genérica ao
prompt, ou a dedup parar de agrupar, ou o excedente do digest voltar a sumir
em silêncio, o teste falha.

## avaliar.py

Mede o julgamento do modelo contra `exemplos.jsonl`: 30 itens rotulados por
você, 15 relevantes e 15 ruído, dos quais **5 marcados como difíceis** — post
de fornecedor disfarçado de pesquisa, relatório patrocinado, breach sem
detalhe técnico, CVE de produto que você não usa.

```bash
python eval/avaliar.py                 # limiar do perfil.yaml
python eval/avaliar.py --limiar 6      # outro corte, sem rechamar o modelo
python eval/avaliar.py --refazer       # ignora o cache de notas
```

As notas ficam em cache, então testar limiares diferentes é instantâneo.
Rode antes e depois de qualquer mudança em `perfil.yaml`, no prompt ou no
modelo. Olhe a linha dos casos difíceis: a média geral esconde exatamente o
tipo de erro que enche o digest de marketing.

Num digest, **falso negativo custa mais que falso positivo**: um item ruim
você pula com o olho; um item bom perdido você nunca descobre. Priorize
revocação.

## auditoria.py

Roda contra o banco real depois de uma semana de coleta.

```bash
python eval/auditoria.py --dias 7
```

Cinco checagens: invariante do filtro (nada entra sem passar da nota),
fontes que emudeceram, volume e taxa de aprovação por fonte, duplicatas
pegas e escapadas, e a zona de fronteira para revisão humana.

O ciclo de melhoria é: rode a auditoria → discorde de alguma linha da zona de
fronteira → copie o título para `exemplos.jsonl` com o rótulo certo → rode
`avaliar.py` → ajuste `perfil.yaml` → rode `avaliar.py` de novo e compare.
