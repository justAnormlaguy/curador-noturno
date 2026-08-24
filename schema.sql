-- Estado do curador noturno. Tudo persistido: se a máquina cair no meio
-- da noite, o próximo worker retoma exatamente de onde parou.

CREATE TABLE IF NOT EXISTS itens (
    id                INTEGER PRIMARY KEY,
    url_hash          TEXT    NOT NULL UNIQUE,  -- idempotência: nunca processa 2x
    url               TEXT    NOT NULL,
    fonte             TEXT    NOT NULL,
    titulo            TEXT    NOT NULL,
    texto             TEXT,
    publicado_em      TEXT,
    coletado_em       TEXT    NOT NULL,

    -- máquina de estados:
    -- novo -> triado -> resumido -> entregue
    --      \-> descartado  (nota abaixo do limiar)
    --      \-> duplicado   (mesma história já coberta por outra fonte)
    --      \-> expirado    (resumido, mas velho demais para entregar)
    --      \-> erro        (esgotou tentativas)
    status            TEXT    NOT NULL DEFAULT 'novo',
    duplicado_de      INTEGER,   -- aponta para o item canônico do grupo
    prioridade        INTEGER NOT NULL DEFAULT 5,  -- qualidade da fonte, 1-10
    tentativas        INTEGER NOT NULL DEFAULT 0,
    proxima_tentativa TEXT,
    erro              TEXT,

    nota              INTEGER,   -- 0..10 dado pela triagem
    motivo            TEXT,      -- justificativa curta da triagem
    resumo            TEXT       -- 3 linhas
);

CREATE INDEX IF NOT EXISTS idx_itens_status ON itens(status, proxima_tentativa);
CREATE INDEX IF NOT EXISTS idx_itens_coletado ON itens(coletado_em);
-- NOTA: o índice de duplicado_de NÃO fica aqui. Num banco criado por uma
-- versão anterior, este script roda antes da migração e o índice falharia
-- sobre uma coluna que ainda não existe. Ele é criado em db.migrar().

-- Log de execuções: serve para você medir quanto tempo cada etapa levou
-- e decidir se vale trocar de modelo.
CREATE TABLE IF NOT EXISTS execucoes (
    id          INTEGER PRIMARY KEY,
    etapa       TEXT NOT NULL,
    inicio      TEXT NOT NULL,
    fim         TEXT,
    processados INTEGER DEFAULT 0,
    falhas      INTEGER DEFAULT 0,
    detalhe     TEXT
);

-- Métricas por chamada de modelo. É o que permite responder
-- "quanto tempo por item?" sem chutar.
CREATE TABLE IF NOT EXISTS chamadas (
    id          INTEGER PRIMARY KEY,
    item_id     INTEGER,
    etapa       TEXT NOT NULL,
    modelo      TEXT,
    duracao_s   REAL,
    ok          INTEGER NOT NULL,
    criado_em   TEXT NOT NULL
);
