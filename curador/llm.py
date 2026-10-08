"""Cliente da API do Gemini (Google AI Studio). O i3 nunca carrega modelo:
ele só fala HTTPS com a API.

Fala direto com a API REST via `requests` — sem SDK extra. O plano gratuito
tem limite por minuto (RPM) e por dia (RPD): este módulo espaça as chamadas
e, quando a cota diária acaba, levanta ErroCota para os workers pararem sem
queimar tentativas — a fila fica intacta para a noite seguinte.
"""

import json
import os
import re
import threading
import time

import requests

API_URL = os.environ.get(
    "GEMINI_API_URL", "https://generativelanguage.googleapis.com/v1beta",
).rstrip("/")
API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODELO = os.environ.get("GEMINI_MODELO", "gemini-3.1-flash-lite")
TIMEOUT_S = int(os.environ.get("GEMINI_TIMEOUT", "60"))
RPM = float(os.environ.get("GEMINI_RPM", "10"))  # margem abaixo do limite free
TENTATIVAS = int(os.environ.get("GEMINI_TENTATIVAS", "4"))
# "minimal"/"low" deixam o modelo rápido e barato em tokens; vazio = padrão da API
PENSAMENTO = os.environ.get("GEMINI_PENSAMENTO", "minimal").strip()


class ErroLLM(RuntimeError):
    pass


class ErroCota(ErroLLM):
    """Cota do plano gratuito esgotada (429 persistente)."""


_trava = threading.Lock()
_ultima_chamada = 0.0


def configurado() -> bool:
    return bool(API_KEY)


def esta_vivo() -> bool:
    """Confere chave e modelo com uma consulta leve (não gasta cota de geração)."""
    if not configurado():
        return False
    try:
        r = requests.get(
            f"{API_URL}/models/{MODELO}",
            headers={"x-goog-api-key": API_KEY}, timeout=10,
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


def _esperar_vez() -> None:
    """Espaça as chamadas para respeitar o RPM configurado."""
    global _ultima_chamada
    if RPM <= 0:
        return
    intervalo = 60.0 / RPM
    with _trava:
        espera = _ultima_chamada + intervalo - time.monotonic()
        if espera > 0:
            time.sleep(espera)
        _ultima_chamada = time.monotonic()


def _atraso_sugerido(r: requests.Response) -> float | None:
    """Lê o RetryInfo que o Gemini manda junto com o 429 (ex: "17s")."""
    try:
        for d in r.json().get("error", {}).get("details", []):
            if d.get("@type", "").endswith("RetryInfo"):
                return float(str(d.get("retryDelay", "")).rstrip("s"))
    except (ValueError, AttributeError):
        pass
    return None


def _cota_diaria(r: requests.Response) -> bool:
    texto = r.text.lower()
    return "perday" in texto or "per_day" in texto or "requests per day" in texto


def _gerar(user: str, *, system: str | None = None,
           config: dict | None = None) -> tuple[str, float]:
    if not configurado():
        raise ErroLLM("GEMINI_API_KEY não configurada")

    corpo = {"contents": [{"role": "user", "parts": [{"text": user}]}]}
    if system:
        corpo["systemInstruction"] = {"parts": [{"text": system}]}
    config = dict(config or {})
    if PENSAMENTO:
        config["thinkingConfig"] = {"thinkingLevel": PENSAMENTO}
    if config:
        corpo["generationConfig"] = config

    url = f"{API_URL}/models/{MODELO}:generateContent"
    t0 = time.monotonic()
    for tentativa in range(1, TENTATIVAS + 1):
        _esperar_vez()
        try:
            r = requests.post(
                url, json=corpo, timeout=TIMEOUT_S,
                headers={"x-goog-api-key": API_KEY},
            )
        except requests.RequestException as e:
            if tentativa == TENTATIVAS:
                raise ErroLLM(str(e)) from e
            time.sleep(2 ** tentativa)
            continue

        if r.status_code == 429:
            if _cota_diaria(r):
                raise ErroCota("cota diária do Gemini esgotada")
            if tentativa == TENTATIVAS:
                raise ErroCota(f"limite de requisições: {r.text[:200]}")
            time.sleep(_atraso_sugerido(r) or 2 ** (tentativa + 2))
            continue
        if r.status_code >= 500 and tentativa < TENTATIVAS:
            time.sleep(2 ** tentativa)
            continue
        if r.status_code != 200:
            raise ErroLLM(f"HTTP {r.status_code}: {r.text[:300]}")

        try:
            candidato = r.json()["candidates"][0]
            texto = "".join(
                p.get("text", "") for p in candidato["content"]["parts"]
                if not p.get("thought")
            )
        except (KeyError, IndexError, ValueError) as e:
            motivo = ""
            try:
                motivo = r.json().get("promptFeedback", {}).get("blockReason", "")
            except ValueError:
                pass
            raise ErroLLM(f"resposta inesperada ({motivo or e})") from e
        if not texto.strip():
            raise ErroLLM(
                f"resposta vazia (finishReason={candidato.get('finishReason')})")
        return texto, time.monotonic() - t0

    raise ErroLLM("tentativas esgotadas")  # inalcançável, por segurança


def _extrair_json(texto: str) -> dict:
    """Rede de segurança: com responseJsonSchema a resposta já vem limpa."""
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", texto, re.S)
    if not m:
        raise ErroLLM(f"sem JSON na resposta: {texto[:200]!r}")
    return json.loads(m.group(0))


def completar(
    system: str,
    user: str,
    *,
    schema: dict | None = None,
    max_tokens: int = 300,
    temperatura: float = 0.1,
) -> tuple[str | dict, float]:
    """Uma chamada. Se `schema` vier, vai como `responseJsonSchema` e a API
    devolve JSON que obedece ao schema — o equivalente à gramática GBNF que
    o llama-server usava."""
    config = {
        "temperature": temperatura,
        # folga para tokens de raciocínio, que contam no limite de saída
        "maxOutputTokens": max(max_tokens, 1024),
    }
    if schema:
        config["responseMimeType"] = "application/json"
        config["responseJsonSchema"] = schema

    texto, duracao = _gerar(user, system=system, config=config)
    return (_extrair_json(texto) if schema else texto.strip()), duracao
