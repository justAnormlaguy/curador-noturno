"""Camada de estado. Todo o resto do sistema é sem memória:
se algo falhar, o estado está aqui e a retomada é automática."""

import hashlib
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
CAMINHO_BANCO = Path(__file__).resolve().parent.parent / "dados" / "curador.db"

MAX_TENTATIVAS = 3
BACKOFF_BASE_S = 60  # 1min, 4min, 9min


def agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conectar(caminho: Path | None = None) -> sqlite3.Connection:
    caminho = Path(caminho or CAMINHO_BANCO)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(caminho, timeout=30)
    con.row_factory = sqlite3.Row
    # WAL: o coletor pode escrever enquanto o worker lê, sem travar.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def inicializar(con: sqlite3.Connection) -> None:
    con.executescript((RAIZ / "schema.sql").read_text(encoding="utf-8"))
    migrar(con)
    con.commit()


def migrar(con: sqlite3.Connection) -> None:
    """Adiciona colunas novas em bancos que já existiam. Roda toda vez e é
    inofensivo: sem isso, atualizar o código quebraria uma base em produção."""
    colunas = {linha["name"] for linha in con.execute("PRAGMA table_info(itens)")}
    novas = [
        ("duplicado_de", "INTEGER"),
        ("prioridade", "INTEGER NOT NULL DEFAULT 5"),
    ]
    for nome, tipo in novas:
        if nome not in colunas:
            con.execute(f"ALTER TABLE itens ADD COLUMN {nome} {tipo}")
            print(f"migração: coluna {nome} adicionada")
    # Só agora a coluna existe com certeza.
    con.execute("CREATE INDEX IF NOT EXISTS idx_itens_dup ON itens(duplicado_de)")
    con.commit()


def hash_url(url: str) -> str:
    return hashlib.sha256(url.strip().lower().encode()).hexdigest()[:32]


def inserir_item(
    con, *, url, fonte, titulo, texto, publicado_em, prioridade: int = 5
) -> bool:
    """Retorna True se o item é novo. INSERT OR IGNORE = idempotência:
    rodar o coletor 5x na mesma noite não duplica nada.

    `prioridade` vem do feeds.yaml e só é usada para desempatar duplicatas:
    quando três veículos cobrem a mesma falha, fica o de maior prioridade."""
    cur = con.execute(
        """INSERT OR IGNORE INTO itens
           (url_hash, url, fonte, titulo, texto, publicado_em, coletado_em,
            prioridade)
           VALUES (?,?,?,?,?,?,?,?)""",
        (hash_url(url), url, fonte, titulo, texto, publicado_em, agora(),
         prioridade),
    )
    return cur.rowcount > 0


def pegar_lote(con, status: str, limite: int = 50) -> list[sqlite3.Row]:
    """Puxa trabalho pronto para rodar (respeitando o backoff)."""
    return con.execute(
        """SELECT * FROM itens
           WHERE status = ?
             AND (proxima_tentativa IS NULL OR proxima_tentativa <= ?)
           ORDER BY id LIMIT ?""",
        (status, agora(), limite),
    ).fetchall()


def marcar_triado(con, item_id: int, nota: int, motivo: str, limiar: int) -> None:
    novo_status = "triado" if nota >= limiar else "descartado"
    con.execute(
        "UPDATE itens SET status=?, nota=?, motivo=?, erro=NULL WHERE id=?",
        (novo_status, nota, motivo, item_id),
    )
    con.commit()


def marcar_resumido(con, item_id: int, resumo: str) -> None:
    con.execute(
        "UPDATE itens SET status='resumido', resumo=?, erro=NULL WHERE id=?",
        (resumo, item_id),
    )
    con.commit()


def marcar_duplicado(con, item_id: int, canonico_id: int) -> None:
    """Não vira erro nem descarte: é um estado próprio, para você conseguir
    auditar depois quantas vagas a dedup salvou."""
    con.execute(
        "UPDATE itens SET status='duplicado', duplicado_de=? WHERE id=?",
        (canonico_id, item_id),
    )
    con.commit()


def fontes_duplicadas(con, canonico_id: int) -> list[str]:
    return [
        l["fonte"]
        for l in con.execute(
            "SELECT DISTINCT fonte FROM itens WHERE duplicado_de=?", (canonico_id,)
        )
    ]


def expirar_antigos(con, horas: int) -> int:
    """Item resumido que nunca coube no digest não pode ficar preso para
    sempre competindo com notícia fresca. Depois da janela, sai de cena."""
    corte = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat(
        timespec="seconds"
    )
    cur = con.execute(
        "UPDATE itens SET status='expirado' WHERE status='resumido' AND coletado_em < ?",
        (corte,),
    )
    con.commit()
    return cur.rowcount


def marcar_entregue(con, ids: list[int]) -> None:
    con.executemany(
        "UPDATE itens SET status='entregue' WHERE id=?", [(i,) for i in ids]
    )
    con.commit()


def registrar_falha(con, item_id: int, erro: str) -> None:
    """Backoff quadrático e desistência após MAX_TENTATIVAS.
    Um item quebrado nunca trava a fila inteira."""
    linha = con.execute(
        "SELECT tentativas FROM itens WHERE id=?", (item_id,)
    ).fetchone()
    n = (linha["tentativas"] if linha else 0) + 1
    if n >= MAX_TENTATIVAS:
        con.execute(
            "UPDATE itens SET status='erro', tentativas=?, erro=? WHERE id=?",
            (n, erro[:500], item_id),
        )
    else:
        proxima = (
            datetime.now(timezone.utc) + timedelta(seconds=BACKOFF_BASE_S * n * n)
        ).isoformat(timespec="seconds")
        con.execute(
            "UPDATE itens SET tentativas=?, proxima_tentativa=?, erro=? WHERE id=?",
            (n, proxima, erro[:500], item_id),
        )
    con.commit()


def registrar_chamada(con, item_id, etapa, modelo, duracao_s, ok) -> None:
    con.execute(
        """INSERT INTO chamadas (item_id, etapa, modelo, duracao_s, ok, criado_em)
           VALUES (?,?,?,?,?,?)""",
        (item_id, etapa, modelo, duracao_s, 1 if ok else 0, agora()),
    )
    con.commit()


class Execucao:
    """Context manager que cronometra uma etapa e grava o resultado."""

    def __init__(self, con, etapa: str):
        self.con, self.etapa = con, etapa
        self.processados = self.falhas = 0

    def __enter__(self):
        self._t0 = time.monotonic()
        cur = self.con.execute(
            "INSERT INTO execucoes (etapa, inicio) VALUES (?,?)", (self.etapa, agora())
        )
        self.id = cur.lastrowid
        self.con.commit()
        return self

    def __exit__(self, *exc):
        self.con.execute(
            "UPDATE execucoes SET fim=?, processados=?, falhas=?, detalhe=? WHERE id=?",
            (
                agora(),
                self.processados,
                self.falhas,
                f"{time.monotonic() - self._t0:.1f}s",
                self.id,
            ),
        )
        self.con.commit()
        return False


def ha_trabalho_pendente(con) -> bool:
    linha = con.execute(
        """SELECT COUNT(*) c FROM itens
           WHERE status IN ('novo','triado')
             AND (proxima_tentativa IS NULL OR proxima_tentativa <= ?)""",
        (agora(),),
    ).fetchone()
    return linha["c"] > 0


def estatisticas(con) -> dict:
    linhas = con.execute(
        "SELECT status, COUNT(*) c FROM itens GROUP BY status"
    ).fetchall()
    return {l["status"]: l["c"] for l in linhas}
