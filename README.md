# Curador Noturno — infosec

Digest matinal no Telegram com notícia e pesquisa de segurança da informação,
produzido de madrugada por um modelo local. O i3 coordena e nunca carrega
modelo; o FX acorda por Wake-on-LAN, trabalha e desliga sozinho.

```
23:30  i3: coleta 21 fontes                     segundos, sem modelo
       i3: tem fila? -> WoL no FX
00:00  FX: triagem   título + trecho -> nota 0-10      ~100 itens
00:35  i3: dedup     mesma CVE / título parecido       sem modelo
00:36  FX: resumo    só o que passou e não é cópia     ~15 itens
01:00  i3: desliga o FX
07:00  i3: monta o digest e envia no Telegram
```

## As quatro decisões que carregam o projeto

**1. Cascata.** A triagem lê um trecho curto e devolve 20 tokens. O resumo,
caro, só roda no que sobreviveu. Corta ~70% do trabalho do modelo.

**2. Dedup sem modelo, antes do resumo.** A mesma falha sai no
BleepingComputer, no The Hacker News e no The Record — três URLs diferentes,
três notas 9, três vagas do digest. O agrupamento usa CVE em comum e
similaridade de título, roda em milissegundos no i3 e acontece *antes* do
resumo, então cada duplicata pega é também um resumo que o FX não gera.

**3. Saída travada por gramática.** O `llama-server` converte o JSON Schema em
GBNF e o modelo fica impossibilitado de fugir do formato. É isso que faz um 3B
entregar JSON válido em 100% das chamadas.

**4. Uma régua só.** A escala de notas vive inteira em `config/perfil.yaml`.
O prompt em `workers.py` não define critério nenhum — ver "Falha 2" abaixo
para entender por que isso importa mais do que parece.

Tudo é retomável: estado em SQLite, `INSERT OR IGNORE` por hash de URL, retry
com backoff e desistência após 3 tentativas. Se o FX travar às 2h, você roda o
mesmo comando de manhã e ele continua de onde parou.

---

# Passo a passo da implantação

## Convenções — leia antes de copiar qualquer comando

Duas máquinas, dois usuários, dois IPs. Confundi-los é o erro mais fácil de
cometer aqui. Todo comando abaixo vem marcado com onde ele roda.

| papel | IP | usuário | hostname | o que roda |
|---|---|---|---|---|
| **i3** — coordena, sempre ligado | `192.168.18.201` | `admin` | minecraft | o curador (este projeto) |
| **FX** — músculo, acorda por WoL | `192.168.18.200` | `homeserver` | homeserver | só o llama-server |

Valores derivados que aparecem na configuração:

| variável | valor | de onde vem |
|---|---|---|
| `LLM_URL` | `http://192.168.18.200:8085` | IP do FX + porta do llama-server |
| `FX_HOST` | `192.168.18.200` | IP do FX |
| `FX_BROADCAST` | `192.168.18.255` | broadcast da rede /24 |
| `FX_SSH_USER` | `homeserver` | usuário **do FX** |
| `FX_MAC` | você anota no passo 2 | placa de rede do FX |

**Os 25 arquivos deste projeto ficam todos no i3.** O FX recebe apenas o
llama.cpp e o modelo — nada deste repositório.

⚠️ Reserve os dois IPs no DHCP do roteador. Se o IP do FX mudar, o Wake-on-LAN
para de funcionar sem nenhuma mensagem de erro: o pacote mágico é UDP e ninguém
avisa que ele foi para o vazio.

### Mapa de dependências

Cada passo só funciona se o anterior estiver pronto. Os pontos onde isso
morde:

- o **MAC do FX** (passo 2) é necessário para escrever o `.env` (passo 6)
- o **token do Telegram** (passo 4) também vai no `.env` (passo 6)
- qualquer comando `curador ...` exige as dependências instaladas (passo 5)
- o teste de Wake-on-LAN real (passo 8) exige o `.env` pronto (passo 6)

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
├── .env.example               → vira .env no passo 6
├── curador/                   ← o pacote Python
│   ├── __init__.py            ← vazio, mas obrigatório
│   ├── cli.py  db.py  coletor.py  llm.py
│   └── workers.py  dedup.py  digest.py  energia.py
├── config/
│   ├── feeds.yaml             ← VOCÊ EDITA: as fontes
│   └── perfil.yaml            ← VOCÊ EDITA: o critério de relevância
├── eval/
│   ├── README.md  teste_fumaca.py  avaliar.py
│   ├── auditoria.py
│   └── exemplos.jsonl         ← VOCÊ EDITA com o tempo
└── systemd/                   ← copiados para /etc/systemd/system no passo 11
```

**Critério de saída:**

```bash
cd /home/admin/curador
test -f schema.sql && test -f curador/__init__.py && test -f config/perfil.yaml \
  && echo "estrutura ok" || echo "ESTRUTURA ERRADA"
find . -type f | wc -l    # deve dar 25
```

Se faltar um módulo do pacote, o erro só aparece como `ImportError` no passo 7,
longe da causa. Confira agora.

---

## Passo 1 — FX: subir o llama-server

**Onde:** FX (`192.168.18.200`), como `homeserver`.

```bash
sudo apt install -y build-essential cmake git libcurl4-openssl-dev
cd /home/homeserver
git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j$(nproc)
```

FX da série Bulldozer/Piledriver não tem AVX2 — não force flags de
arquitetura, deixe o cmake detectar.

Modelo: **Qwen2.5 3B Instruct Q4_K_M** (~2 GB).

```bash
mkdir -p /home/homeserver/modelos && cd /home/homeserver/modelos
curl -L -o qwen2.5-3b-instruct-q4_k_m.gguf \
  https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf
```

Serviço permanente. Precisa subir no boot, porque quem liga o FX é o pacote
mágico do WoL — não há ninguém para dar `start` depois:

```ini
# /etc/systemd/system/llama-server.service
[Unit]
Description=llama-server
After=network-online.target

[Service]
User=homeserver
ExecStart=/home/homeserver/llama.cpp/build/bin/llama-server \
  -m /home/homeserver/modelos/qwen2.5-3b-instruct-q4_k_m.gguf \
  --host 0.0.0.0 --port 8085 \
  --ctx-size 4096 --threads 4 --parallel 1 --cont-batching
Restart=always
RestartSec=5
StartLimitBurst=3

[Install]
WantedBy=multi-user.target
```

`--host 0.0.0.0` é o que permite o i3 acessar; `127.0.0.1` só serviria
localmente. `--threads`: no FX-8xxx, 8 "núcleos" são 4 módulos com FPU
compartilhada — meça `4` e `8`, muitas vezes empatam e o 4 esquenta menos.

`RestartSec=5` evita o laço de reinício: com erro de configuração, o
`Restart=always` sozinho faz o systemd bloquear a unit em cinco tentativas
("Start request repeated too quickly") e todo erro vira o mesmo diagnóstico
confuso.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now llama-server
systemctl status llama-server --no-pager
```

**Critério de saída,** ainda no FX:

```bash
curl -s localhost:8085/health     # {"status":"ok"}
```

Se falhar: `journalctl -u llama-server -n 30 --no-pager`. `status=217/USER`
significa que o `User=` não existe nessa máquina; depois de corrigir a unit,
`sudo systemctl reset-failed llama-server` antes de tentar de novo.

O acesso a partir do i3 é verificado no passo 8 — pode haver firewall no
caminho, e é cedo demais para testar isso agora.

---

## Passo 2 — FX: Wake-on-LAN, desligamento sem senha e o MAC

**Onde:** FX (`192.168.18.200`), como `homeserver`.

Primeiro, na BIOS: habilite "Power On By PCI-E" ou "Wake on LAN". Sem isso,
nada abaixo funciona.

```bash
sudo apt install -y ethtool
ip -br link                                # descubra a interface, ex. enp3s0
sudo ethtool enp3s0 | grep Wake-on         # precisa terminar em "g"
sudo ethtool -s enp3s0 wol g               # se estiver "d"
```

O `wol g` se perde no reboot. Torne permanente — troque o MAC pelo real:

```ini
# /etc/systemd/network/50-wol.link
[Match]
MACAddress=AA:BB:CC:DD:EE:FF

[Link]
WakeOnLan=magic
```

Se a sua rede é gerenciada pelo NetworkManager e não pelo systemd-networkd,
esse arquivo é ignorado; nesse caso use
`nmcli connection modify <conexão> 802-3-ethernet.wake-on-lan magic`.

Autorize o desligamento remoto:

```bash
echo 'homeserver ALL=(ALL) NOPASSWD: /sbin/poweroff' | sudo tee /etc/sudoers.d/curador
sudo visudo -c        # valida a sintaxe; sudoers quebrado tranca a máquina
```

**Critério de saída — anote estes dois valores, você vai precisar no passo 6:**

```bash
ip link show enp3s0 | awk '/ether/ {print "FX_MAC=" $2}'
sudo ethtool enp3s0 | grep Wake-on        # confirme que é "g"
```

---

## Passo 3 — i3: chave SSH para o FX

**Onde:** i3 (`192.168.18.201`), como `admin`.

Gere como `admin`, que é o usuário que vai rodar o serviço no passo 11 — o
systemd usa o `~/.ssh` **dele**.

```bash
ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_curador
ssh-copy-id -i ~/.ssh/id_curador.pub homeserver@192.168.18.200
```

Repare que o usuário troca de lado: a chave nasce em `admin` no i3 e é
instalada na conta `homeserver` do FX. O nome do arquivo `.pub` precisa bater
exatamente com o que o `ssh-keygen` criou.

**Critério de saída.** Teste com ssh puro — o projeto ainda não está instalado.
`BatchMode=yes` é o mesmo modo do código, então falha se pedir senha:

```bash
ssh -o BatchMode=yes -i ~/.ssh/id_curador -o IdentitiesOnly=yes \
    homeserver@192.168.18.200 'echo funcionou'
```

Precisa imprimir `funcionou`. Enquanto não imprimir, não siga: o desligamento é
a única parte do sistema que falha **cara** e em silêncio — o FX passaria a
noite ligado e você descobriria pela conta de luz.

Como a chave tem nome fora do padrão (`id_curador`, não `id_ed25519`), o ssh
**não a usa automaticamente**. Guarde o caminho para o `FX_SSH_KEY` do passo 6:
`/home/admin/.ssh/id_curador`.

---

## Passo 4 — Telegram: token e chat id

**Onde:** qualquer lugar com navegador; os valores vão para o i3 no passo 6.

1. Fale com `@BotFather` no Telegram → `/newbot` → guarde o token.
2. **Mande qualquer mensagem para o seu bot** — sem isso o passo 3 vem vazio.
3. Pegue o chat id:

```bash
curl -s "https://api.telegram.org/bot<SEU_TOKEN>/getUpdates" \
  | grep -o '"id":[-0-9]*' | head -1
```

**Critério de saída:** você tem em mãos o token e um número de chat id.

---

## Passo 5 — i3: instalar as dependências

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

## Passo 6 — i3: configuração e banco

**Onde:** i3 (`192.168.18.201`), como `admin`.
**Precisa:** o MAC do passo 2 e o token do passo 4.

```bash
cd /home/admin/curador
cp .env.example .env && chmod 600 .env
nano .env
```

O `.env` completo desta instalação:

```
LLM_URL=http://192.168.18.200:8085
LLM_MODELO=qwen2.5-3b-instruct-q4_k_m
LLM_TIMEOUT=420

FX_MAC=AA:BB:CC:DD:EE:FF            # o que você anotou no passo 2
FX_HOST=192.168.18.200
FX_BROADCAST=192.168.18.255
FX_SSH_USER=homeserver
FX_SSH_KEY=/home/admin/.ssh/id_curador

TELEGRAM_TOKEN=...                   # do passo 4
TELEGRAM_CHAT_ID=...
```

Repare que `FX_SSH_USER` é `homeserver` — o usuário **do FX**, não o seu.
É o erro mais comum aqui.

Agora as fontes. `config/feeds.yaml` traz 13 feeds e 8 repositórios, com três
campos por fonte:

- `url` — o feed
- `max` — teto por noite; BleepingComputer e The Hacker News publicam 10-15
  por dia cada e afogariam o digest. Já vêm limitados.
- `prioridade` (1-10) — **só desempata duplicatas**; não influencia a nota

Tire o que você não usa, principalmente os repositórios: release de ferramenta
que você não roda é ruído garantido e custa uma chamada de FX.

Crie o banco:

```bash
python3 -m curador.cli init
```

**Critério de saída:** imprime `banco pronto em .../dados/curador.db`. Numa
instalação nova não há mensagem de migração.

---

## Passo 7 — i3: teste de fumaça, sem tocar no FX

**Onde:** i3. **Duração:** 1 segundo.

```bash
cd /home/admin/curador
python3 eval/teste_fumaca.py
```

Roda o pipeline inteiro com um modelo falso: coleta, idempotência, retry com
backoff, deduplicação, teto do digest e fila. Valida a lógica sem gastar um
minuto de FX.

**Critério de saída:** termina com `TUDO OK ✓`. Se falhar aqui, é problema de
instalação — não adianta seguir.

---

## Passo 8 — i3: verificar a ligação com o FX

**Onde:** i3. **Precisa:** passos 1, 2, 3 e 6 completos.

Este passo não existia nas versões anteriores deste documento e é onde a maior
parte dos problemas de rede aparece. Quatro verificações, nesta ordem:

```bash
cd /home/admin/curador
set -a && source .env && set +a
```

**1. O i3 alcança o llama-server?** No passo 1 você testou por `localhost`, o
que não prova nada sobre a rede:

```bash
curl -s http://192.168.18.200:8085/health     # {"status":"ok"}
```

Sem resposta, com o serviço rodando no FX: firewall. No FX,
`sudo ufw allow from 192.168.18.201 to any port 8085`.

**2. O SSH funciona pelo `.env`?**

```bash
python3 -m curador.cli checar-ssh
```

Diferente do teste do passo 3, este usa `FX_SSH_USER` e `FX_SSH_KEY` do
arquivo — ou seja, valida a configuração, não só a chave.

**3. O desligamento funciona?**

```bash
python3 -m curador.cli dormir
sleep 30 && ping -c2 192.168.18.200      # não deve responder
```

**4. O Wake-on-LAN funciona?** Com o FX desligado do passo anterior:

```bash
python3 -m curador.cli acordar
```

Ele envia o pacote mágico e espera o `/health` responder — sem `sleep` fixo,
porque o tempo de boot mais carga do modelo varia. Deve terminar com
`FX pronto.`

**Critério de saída:** as quatro passam. Só aqui você sabe que o ciclo
liga-trabalha-desliga fecha. Se o WoL falhar, revise a BIOS e o `ethtool` do
passo 2 — e confirme que o roteador entrega broadcast na `192.168.18.255`.

---

## Passo 9 — primeira noite, na mão

**Onde:** i3, com o FX ligado pelo passo 8.

Rode uma etapa por vez e observe. Comece pequeno: `--limite 20` na triagem.

```bash
cd /home/admin/curador
set -a && source .env && set +a

python3 -m curador.cli coletar             # segundos, sem modelo
python3 -m curador.cli triar --limite 20   # aqui o FX trabalha
python3 -m curador.cli status              # tempo medido por chamada
python3 -m curador.cli deduplicar          # instantâneo, sem modelo
python3 -m curador.cli resumir --limite 5
python3 -m curador.cli digest --seco       # imprime, não envia
python3 -m curador.cli dormir
```

`status` é o número que decide tudo: com ele você extrapola quantos itens
cabem na madrugada. Esperado no FX com 3B Q4: **15–25 s por triagem** e
**40–70 s por resumo**. 100 triagens + 15 resumos ≈ 1 h.

Passou de 40 s por triagem? Reduza `--ctx-size` para 2048 na unit do FX e
corte o trecho enviado em `_texto_triagem` (`curador/workers.py`).

**Critério de saída:** o `digest --seco` imprime algo que você teria gostado de
receber. Se vier cheio de marketing, é o passo 10 que resolve — não mexa nos
feeds ainda.

---

## Passo 10 — calibrar o julgamento do modelo

**Onde:** i3, **com o FX ligado** (`python3 -m curador.cli acordar` antes).

Todos os passos anteriores provam que a máquina funciona. Este é o único que
mede se o **modelo** entendeu o seu critério.

```bash
python3 -m curador.cli acordar
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
python3 -m curador.cli dormir
```

Ajuste `limiar_nota` em `config/perfil.yaml` para o corte com melhor revocação
sem encher de marketing. **Num digest, falso negativo custa mais que falso
positivo:** um item ruim você pula com o olho; um item bom perdido você nunca
descobre.

**Critério de saída:** você escolheu um limiar com base em número, não em
impressão.

---

## Passo 11 — automatizar

**Onde:** i3, como `admin` com sudo.

As units já vêm com `User=admin` e `/home/admin/curador`. **Se você usou a rota
A do passo 5** (sem venv), comente a linha `ExecStart` do venv e descomente a
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

`noite` faz coleta → WoL → triagem → dedup → resumo → desliga, e o desligamento
acontece **mesmo se o worker explodir** (o `finally` em `cmd_noite`). O digest
é um timer separado de propósito: se a noite falhar, às 7h você recebe o que
deu certo em vez de não receber nada.

**Critério de saída:** `list-timers` mostra os dois com próximo disparo. Na
manhã seguinte, confira `journalctl -u curador-noite --since yesterday` e —
principalmente — que o FX está desligado.

---

## Passo 12 — auditar depois de uma semana

**Onde:** i3, FX pode estar desligado.

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
`duplicado` — **nunca resumidos**, o que economiza tempo de FX além de vaga.
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
writeup de bug bounty e uma fuga de contêiner, ambos nota 9. O FX gastou tempo
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

- **Cascata de verdade:** suba um segundo `llama-server` na porta 8086 com um
  **Qwen2.5 0.5B** só para a triagem. Cai para ~4 s/item e o 3B fica só nos
  resumos. Agora você tem o `avaliar.py` para provar que a qualidade não caiu.
- **Dedup semântica:** embeddings pegam a mesma história contada com palavras
  totalmente diferentes, que o Jaccard não alcança. Roda rápido na CPU e
  também não gera texto.
- **Fila genérica:** quando o organizador de arquivos chegar, `db.py` vira o
  worker pool compartilhado — a tabela `itens` já é uma fila com estado.

# Problemas comuns

| Sintoma | Causa provável |
|---|---|
| `acordar` sempre falha | `ethtool` mostra `Wake-on: d`, ou o roteador não repassa broadcast — use o broadcast da sua sub-rede em `FX_BROADCAST` |
| FX acorda mas `/health` não responde | `llama-server` não está `enable`d, ou ainda carregando o modelo (aumente o timeout em `acordar`); confira também se a porta do `.env` bate com a da unit |
| `status=217/USER` na unit | o `User=` não existe nessa máquina — `id USUARIO` para confirmar |
| `Start request repeated too quickly` | rate limit do systemd após falhas seguidas: `systemctl reset-failed llama-server` |
| `ensurepip is not available` | falta `python3.12-venv`; ou instale-o, ou use a rota A do passo 3 |
| `python3.12-venv não tem candidato` | componente `universe` desligado: `sudo add-apt-repository universe && sudo apt update` |
| `.venv/bin/python: No such file` | o venv não foi criado (veja acima) ou você está na rota A: use `python3` |
| `ssh-copy-id: failed to open ID file` | o nome do arquivo não bate com o do `ssh-keygen`; aponte para o `.pub` |
| FX amanhece ligado | `curador checar-ssh` — provavelmente a chave não é encontrada; defina `FX_SSH_KEY` |
| Itens presos em `novo` | `SELECT erro, tentativas FROM itens WHERE status='erro'` |
| JSON inválido na triagem | `response_format` não foi aceito: confirme que o llama.cpp é recente (`/props` responde) |
| Digest vazio toda manhã | limiar alto demais; `eval/avaliar.py --limiar 5` |
| Digest cheio de marketing | limiar baixo, ou perfil sem exclusões suficientes; olhe os casos difíceis no `avaliar.py` |
| Assunto repetido no digest | `LIMIAR_SIMILARIDADE` em `dedup.py` está alto (hoje 0.45); a auditoria mostra os pares que escaparam |
