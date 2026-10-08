# Curador Noturno — infosec

Digest matinal no Telegram com notícia e pesquisa de segurança da informação,
produzido de madrugada por um modelo de linguagem. O i3 coordena tudo e nunca
carrega modelo: a triagem e o resumo vão para a API gratuita do **Gemini**.
Sem segundo servidor, sem Wake-on-LAN, sem SSH.

```
23:30  i3: coleta 21 fontes                     segundos, sem modelo
23:31  API: triagem  título + trecho -> nota 0-10      ~100 itens
23:45  i3: dedup     mesma CVE / título parecido       sem modelo
23:45  API: resumo   só o que passou e não é cópia     ~15 itens
07:00  i3: monta o digest e envia no Telegram
```

## As quatro decisões que carregam o projeto

**1. Cascata.** A triagem lê um trecho curto e devolve 20 tokens. O resumo,
caro, só roda no que sobreviveu. Corta ~70% do trabalho do modelo.

**2. Dedup sem modelo, antes do resumo.** A mesma falha sai no
BleepingComputer, no The Hacker News e no The Record — três URLs diferentes,
três notas 9, três vagas do digest. O agrupamento usa CVE em comum e
similaridade de título, roda em milissegundos no i3 e acontece *antes* do
resumo, então cada duplicata pega é também uma chamada de resumo que não
gasta cota da API.

**3. Saída travada por schema.** O JSON Schema da triagem vai como
`responseJsonSchema` e a API devolve JSON que obedece a ele — nota inteira de
0 a 10 e motivo curto, sem texto em volta para limpar.

**4. Uma régua só.** A escala de notas vive inteira em `config/perfil.yaml`.
O prompt em `workers.py` não define critério nenhum — ver "Falha 2" abaixo
para entender por que isso importa mais do que parece.

Tudo é retomável: estado em SQLite, `INSERT OR IGNORE` por hash de URL, retry
com backoff e desistência após 3 tentativas. Se a API cair às 2h ou a cota
diária acabar, os itens que faltam ficam na fila **sem gastar tentativa** e a
noite seguinte continua de onde parou.

---

# Passo a passo da implantação

## Convenções — leia antes de copiar qualquer comando

Uma máquina e uma API. Todo comando abaixo roda no i3.

| papel | onde | usuário | o que roda |
|---|---|---|---|
| **i3** — coordena, sempre ligado | `192.168.18.201` | `admin` | o curador (este projeto) |
| **Gemini** — triagem e resumo | `generativelanguage.googleapis.com` | — | `gemini-3.1-flash-lite` (plano gratuito) |

Valores que aparecem na configuração:

| variável | valor | de onde vem |
|---|---|---|
| `GEMINI_API_KEY` | você cria no passo 1 | Google AI Studio |
| `GEMINI_MODELO` | `gemini-3.1-flash-lite` | modelo com plano gratuito |
| `GEMINI_RPM` | `10` | abaixo do limite por minuto do seu projeto |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | você anota no passo 2 | BotFather |

**Os arquivos deste projeto ficam todos no i3.** Ele precisa apenas de saída
HTTPS para a internet.

Sobre o plano gratuito: o Google pode usar o conteúdo enviado para melhorar os
produtos dele. Aqui isso é aceitável — o que vai para a API são títulos e
trechos de notícias públicas e o seu `perfil.yaml`. Não coloque nada sensível
no perfil.

### Mapa de dependências

Cada passo só funciona se o anterior estiver pronto. Os pontos onde isso
morde:

- a **chave do Gemini** (passo 1) e o **token do Telegram** (passo 2) vão no
  `.env` (passo 4)
- qualquer comando `curador ...` exige as dependências instaladas (passo 3)
- o teste da API real (passo 6) exige o `.env` pronto (passo 4)

Por isso a coleta de valores vem antes da configuração, e não intercalada.

---

## Passo 0 — i3: baixar e montar a estrutura

**Onde:** i3 (`192.168.18.201`), como `admin`.

A estrutura de pastas é obrigatória: o `db.py` localiza o `schema.sql` e a
pasta `config/` subindo um nível a partir de onde ele mesmo está. Achatar tudo
numa pasta só faz o `init` falhar com "no such file".

```bash
# do seu computador
scp curador.tar.gz admin@192.168.18.201:/home/admin/

# no i3
cd /home/admin && tar -xzf curador.tar.gz && cd curador
```

Árvore resultante:

```
/home/admin/curador/
├── README.md
├── requirements.txt
├── schema.sql                 ← lido por db.py, precisa ficar na raiz
├── .env.example               → vira .env no passo 4
├── curador/                   ← o pacote Python
│   ├── __init__.py            ← vazio, mas obrigatório
│   ├── cli.py  db.py  coletor.py  llm.py
│   └── workers.py  dedup.py  digest.py
├── config/
│   ├── feeds.yaml             ← VOCÊ EDITA: as fontes
│   └── perfil.yaml            ← VOCÊ EDITA: o critério de relevância
├── eval/
│   ├── README.md  teste_fumaca.py  avaliar.py
│   ├── auditoria.py
│   └── exemplos.jsonl         ← VOCÊ EDITA com o tempo
└── systemd/                   ← copiados para /etc/systemd/system no passo 9
```

**Critério de saída:**

```bash
cd /home/admin/curador
test -f schema.sql && test -f curador/__init__.py && test -f config/perfil.yaml \
  && echo "estrutura ok" || echo "ESTRUTURA ERRADA"
find . -type f | wc -l    # deve dar 25
```

Se faltar um módulo do pacote, o erro só aparece como `ImportError` no passo 5,
longe da causa. Confira agora.

---

## Passo 1 — Gemini: criar a chave da API (gratuita)

**Onde:** qualquer lugar com navegador; a chave vai para o i3 no passo 4.

1. Entre em <https://aistudio.google.com/apikey> com uma conta Google.
2. **Create API key** → escolha (ou crie) um projeto. Não precisa de cartão.
3. Em **Rate limits**, anote o limite gratuito por minuto (RPM) e por dia
   (RPD) do `gemini-3.1-flash-lite` no seu projeto — o Google muda esses
   números com frequência.

Conta da noite: com `--limite 200` na triagem e ~15 resumos, são até ~215
chamadas. Se o RPD do seu projeto for menor que isso, baixe o `--limite` do
`noite` na unit do passo 9 — o que não couber fica na fila para a noite
seguinte, sem perder nada.

**Critério de saída:** você tem a chave em mãos e sabe o RPM/RPD do projeto.

---

## Passo 2 — Telegram: token e chat id

**Onde:** qualquer lugar com navegador; os valores vão para o i3 no passo 4.

1. Fale com `@BotFather` no Telegram → `/newbot` → guarde o token.
2. **Mande qualquer mensagem para o seu bot** — sem isso o item 3 vem vazio.
3. Pegue o chat id:

```bash
curl -s "https://api.telegram.org/bot<SEU_TOKEN>/getUpdates" \
  | grep -o '"id":[-0-9]*' | head -1
```

**Critério de saída:** você tem em mãos o token e um número de chat id.

---

## Passo 3 — i3: instalar as dependências

**Onde:** i3 (`192.168.18.201`), como `admin`.

São três: `feedparser`, `PyYAML` e `requests`. Escolha **uma** rota.

### Rota A — pacotes do sistema (recomendada)

Sem ambiente virtual para quebrar num upgrade de Python — o que importa para
um serviço que roda sozinho toda madrugada.

```bash
sudo apt install python3-feedparser python3-yaml python3-requests
```

Nesta rota, **todo comando `python3 -m curador.cli ...` roda direto**.

### Rota B — ambiente virtual

```bash
cd /home/admin/curador
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Deu `ensurepip is not available`? Falta o pacote de venv:

```bash
sudo apt update
apt-cache policy python3.12-venv     # "Candidate: (none)" = universe desligado
sudo add-apt-repository universe     # só nesse caso
sudo apt update && sudo apt install python3.12-venv
```

Não conseguiu? Use a rota A — o projeto não perde nada. Na rota B, todo
comando abaixo troca `python3` por `.venv/bin/python`.

**Critério de saída:**

```bash
cd /home/admin/curador
python3 -c "import feedparser, yaml, requests; print('deps ok')"
```

---

## Passo 4 — i3: configuração e banco

**Onde:** i3 (`192.168.18.201`), como `admin`.
**Precisa:** a chave do passo 1 e o token do passo 2.

```bash
cd /home/admin/curador
cp .env.example .env && chmod 600 .env
nano .env
```

O `.env` completo desta instalação:

```
GEMINI_API_KEY=...                   # do passo 1
GEMINI_MODELO=gemini-3.1-flash-lite
GEMINI_RPM=10
GEMINI_PENSAMENTO=minimal

TELEGRAM_TOKEN=...                   # do passo 2
TELEGRAM_CHAT_ID=...
```

- `GEMINI_RPM` espaça as chamadas. Deixe abaixo do limite do seu projeto;
  se o 429 aparecer com frequência no log, baixe.
- `GEMINI_PENSAMENTO=minimal` deixa o raciocínio interno no mínimo: triagem e
  resumo de 3 linhas não ganham nada com mais, e cada token pensado conta.

Agora as fontes. `config/feeds.yaml` traz 13 feeds e 8 repositórios, com três
campos por fonte:

- `url` — o feed
- `max` — teto por noite; BleepingComputer e The Hacker News publicam 10-15
  por dia cada e afogariam o digest. Já vêm limitados.
- `prioridade` (1-10) — **só desempata duplicatas**; não influencia a nota

Tire o que você não usa, principalmente os repositórios: release de ferramenta
que você não roda é ruído garantido e custa uma chamada à API.

Crie o banco:

```bash
python3 -m curador.cli init
```

**Critério de saída:** imprime `banco pronto em .../dados/curador.db`. Numa
instalação nova não há mensagem de migração.

---

## Passo 5 — i3: teste de fumaça, sem chave nem internet

**Onde:** i3. **Duração:** 1 segundo.

```bash
cd /home/admin/curador
python3 eval/teste_fumaca.py
```

Roda o pipeline inteiro com um modelo falso: coleta, idempotência, retry com
backoff, deduplicação, teto do digest e fila — e o cliente do Gemini com HTTP
simulado: formato da requisição, leitura da resposta e parada por cota
esgotada. Valida a lógica sem gastar nenhuma chamada.

**Critério de saída:** termina com `TUDO OK ✓`. Se falhar aqui, é problema de
instalação — não adianta seguir.

---

## Passo 6 — i3: verificar a API do Gemini

**Onde:** i3. **Precisa:** passos 1 e 4 completos.

```bash
cd /home/admin/curador
set -a && source .env && set +a
python3 -m curador.cli checar-api
```

Confere a chave e o modelo e faz uma chamada mínima com schema — o mesmo
caminho da triagem. Deve terminar com
`✓ gemini-3.1-flash-lite respondeu {'ok': True} em ...`.

**Critério de saída:** o `checar-api` passa. Se disser chave ou modelo
inválido, confira a chave no AI Studio e o nome em `GEMINI_MODELO`; se der
timeout, o i3 não tem saída HTTPS (proxy ou firewall).

---

## Passo 7 — primeira noite, na mão

**Onde:** i3, com o `checar-api` do passo 6 passando.

Rode uma etapa por vez e observe. Comece pequeno: `--limite 20` na triagem.

```bash
cd /home/admin/curador
set -a && source .env && set +a

python3 -m curador.cli coletar             # segundos, sem modelo
python3 -m curador.cli triar --limite 20   # aqui a API trabalha
python3 -m curador.cli status              # tempo medido por chamada
python3 -m curador.cli deduplicar          # instantâneo, sem modelo
python3 -m curador.cli resumir --limite 5
python3 -m curador.cli digest --seco       # imprime, não envia
```

`status` mostra o tempo medido por chamada. Na API, cada chamada leva
**1–3 s**; o que manda no tempo total é o espaçamento do `GEMINI_RPM`
(10 RPM = uma chamada a cada 6 s). 100 triagens + 15 resumos ≈ 12 min.

Muitos `429` no log? Baixe `GEMINI_RPM`. Itens parados com "cota diária
esgotada"? Baixe o `--limite` ou aceite que o resto sai na noite seguinte.

**Critério de saída:** o `digest --seco` imprime algo que você teria gostado de
receber. Se vier cheio de marketing, é o passo 8 que resolve — não mexa nos
feeds ainda.

---

## Passo 8 — calibrar o julgamento do modelo

**Onde:** i3, com a chave no `.env` (30 chamadas, uma por exemplo).

Todos os passos anteriores provam que a máquina funciona. Este é o único que
mede se o **modelo** entendeu o seu critério.

```bash
python3 eval/avaliar.py
```

Roda os 30 exemplos rotulados de `eval/exemplos.jsonl` e imprime precisão,
revocação e F1, mais uma linha separada para os **5 casos difíceis** — post de
fornecedor disfarçado de pesquisa, relatório patrocinado, breach sem detalhe
técnico, CVE de produto que você não usa. É neles que um filtro fraco se
entrega, e a média geral esconde isso.

As notas ficam em cache, então testar limiares é instantâneo:

```bash
python3 eval/avaliar.py --limiar 6
python3 eval/avaliar.py --limiar 8
```

Ajuste `limiar_nota` em `config/perfil.yaml` para o corte com melhor revocação
sem encher de marketing. **Num digest, falso negativo custa mais que falso
positivo:** um item ruim você pula com o olho; um item bom perdido você nunca
descobre.

**Critério de saída:** você escolheu um limiar com base em número, não em
impressão.

---

## Passo 9 — automatizar

**Onde:** i3, como `admin` com sudo.

As units já vêm com `User=admin` e `/home/admin/curador`. **Se você usou a rota
A do passo 3** (sem venv), comente a linha `ExecStart` do venv e descomente a
do `/usr/bin/python3` — as duas estão nos arquivos.

```bash
cd /home/admin/curador
nano systemd/curador-noite.service     # escolha o ExecStart
nano systemd/curador-digest.service

sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now curador-noite.timer curador-digest.timer
systemctl list-timers 'curador-*'
```

`noite` faz coleta → checagem da API → triagem → dedup → resumo. Se a API
não responder, aborta em segundos com a fila intacta; se a cota acabar no
meio, para a etapa e deixa o resto para a noite seguinte. O digest é um timer
separado de propósito: se a noite falhar, às 7h você recebe o que deu certo
em vez de não receber nada.

**Critério de saída:** `list-timers` mostra os dois com próximo disparo. Na
manhã seguinte, confira `journalctl -u curador-noite --since yesterday` e
procure por `cota` ou `429` — sinal de que `GEMINI_RPM` ou `--limite` precisam
de ajuste.

---

## Passo 10 — auditar depois de uma semana

**Onde:** i3. Não usa a API.

```bash
cd /home/admin/curador
python3 eval/auditoria.py --dias 7
```

Cinco checagens sobre os seus dados reais: invariante do filtro, fontes que
emudeceram, volume e taxa de aprovação por fonte, duplicatas pegas e escapadas,
e a zona de fronteira.

O ciclo de melhoria, que é o que faz o digest ficar bom com o tempo: rode a
auditoria → discorde de uma linha da zona de fronteira → copie o título para
`eval/exemplos.jsonl` com o rótulo certo → rode `eval/avaliar.py` → ajuste
`config/perfil.yaml` → rode de novo e compare os números.

Sem esse ciclo você está adivinhando.

**Critério de saída:** a seção 1 diz `Invariante do filtro: OK`, a seção 2 não
lista fonte muda, e você reconhece o que está na seção 5 como julgamento seu —
não do modelo.

---

# O que mudou nesta versão

Três falhas de relevância foram encontradas auditando o sistema com 34 notícias
reais de segurança e corrigidas.

## Falha 1 — duplicatas ocupavam o digest

**Sintoma:** o CVE-2026-4041 do nginx entrou três vezes (BleepingComputer, The
Hacker News, The Record), todas nota 9. Com quatro veículos de notícia no
`feeds.yaml`, isso acontece com toda falha grande. Um digest de 15 vagas
entregava 11 assuntos.

**Causa:** o único dedup existente era hash de URL exata, e as três URLs eram
de fato diferentes.

**Correção:** módulo `curador/dedup.py`, etapa própria entre triagem e resumo.
Agrupa por CVE em comum (sinal forte, quase sem falso positivo neste domínio)
e por similaridade de título via Jaccard sobre palavras de conteúdo. Compara
também com o que já foi entregue nos últimos 3 dias, então a notícia de ontem
não volta hoje com outra roupa. O canônico é escolhido por
`(nota, prioridade da fonte, tamanho do texto)`, e os demais viram status
`duplicado` — **nunca resumidos**, o que economiza chamadas além de vaga.
No digest, aparecem como "também em: ...".

Não usa modelo. É o caso do "isso precisa mesmo que o modelo *escreva* algo?"
em que a resposta é não.

## Falha 2 — duas rubricas competindo no prompt

**Sintoma:** tendência a aprovar itens fracos.

**Causa:** `SYSTEM_TRIAGEM` acrescentava a própria escala genérica ("7-10 =
diretamente útil para o que ele faz") **depois** do perfil, ou seja, na posição
de maior peso. E essa pergunta é *temática* — como todas as fontes já são de
segurança, ela empurrava tudo para cima.

**Correção:** o prompt não define mais critério nenhum. A escala completa de
0 a 10 vive em `config/perfil.yaml`, com um aviso explícito ao modelo de que o
tema não diferencia nada aqui. O `teste_fumaca.py` falha se a rubrica genérica
voltar.

## Falha 3 — itens resumidos morriam sem serem entregues

**Sintoma:** 19 itens resumidos, 15 entregues. Quatro sumiram — incluindo um
writeup de bug bounty e uma fuga de contêiner, ambos nota 9. O modelo gastou tempo
gerando resumos que ninguém leu.

**Causa:** ordenação só por nota. Como quase todo aprovado tira 9, o desempate
virava alfabético por fonte, e um item bom podia ser espremido por notícia
nova indefinidamente.

**Correção:** ordem `nota DESC, coletado_em ASC` — entre notas iguais, o mais
velho passa na frente e a fila anda. Teto de 4 itens por fonte, para uma fonte
de alto volume não tomar o digest inteiro, com segunda passada preenchendo as
vagas que sobrarem. O excedente continua na fila e é anunciado no rodapé
("3 na fila para amanhã"). Depois de 72 h, o que nunca coube vira `expirado`
em vez de competir para sempre com notícia fresca.

## Sobre a CISA

A agência aposentou os feeds RSS em maio de 2025 — alertas e catálogo KEV
passaram a sair só por e-mail (GovDelivery) e redes sociais, sem substituto.
Por isso não há feed da CISA aqui. O KEV continua disponível como JSON e daria
um coletor separado de ~20 linhas, se você quiser.

---

# Quando estabilizar

- **Cascata de modelos:** triagem no `gemini-3.1-flash-lite` e resumo num
  modelo maior (variável própria para o resumo em `llm.py`). Confira antes se
  o modelo maior tem cota gratuita suficiente para ~15 resumos por noite, e
  use o `avaliar.py` para provar que valeu.
- **Dedup semântica:** embeddings pegam a mesma história contada com palavras
  totalmente diferentes, que o Jaccard não alcança. Roda rápido na CPU e
  também não gera texto.
- **Fila genérica:** quando o organizador de arquivos chegar, `db.py` vira o
  worker pool compartilhado — a tabela `itens` já é uma fila com estado.

# Problemas comuns

| Sintoma | Causa provável |
|---|---|
| `GEMINI_API_KEY não definida` | faltou `set -a && source .env && set +a`, ou o `EnvironmentFile=` da unit aponta para outro lugar |
| `checar-api` diz chave/modelo inválido | chave errada ou o nome do modelo mudou — confira no AI Studio |
| `HTTP 429` frequente no log | baixe `GEMINI_RPM` no `.env` |
| Itens parados com "cota diária esgotada" | RPD do projeto menor que o volume da noite: baixe `--limite` do `noite`; o resto sai na noite seguinte |
| `HTTP 400` citando `thinkingConfig` | o modelo não aceita esse nível; deixe `GEMINI_PENSAMENTO=` vazio |
| `status=217/USER` na unit | o `User=` não existe nessa máquina — `id USUARIO` para confirmar |
| `ensurepip is not available` | falta `python3.12-venv`; ou instale-o, ou use a rota A do passo 3 |
| `python3.12-venv não tem candidato` | componente `universe` desligado: `sudo add-apt-repository universe && sudo apt update` |
| `.venv/bin/python: No such file` | o venv não foi criado (veja acima) ou você está na rota A: use `python3` |
| Itens presos em `novo` | `SELECT erro, tentativas FROM itens WHERE status='erro'` |
| JSON inválido na triagem | resposta bloqueada ou cortada — o erro traz o `finishReason`; rode `checar-api` |
| Digest vazio toda manhã | limiar alto demais; `eval/avaliar.py --limiar 5` |
| Digest cheio de marketing | limiar baixo, ou perfil sem exclusões suficientes; olhe os casos difíceis no `avaliar.py` |
| Assunto repetido no digest | `LIMIAR_SIMILARIDADE` em `dedup.py` está alto (hoje 0.45); a auditoria mostra os pares que escaparam |
