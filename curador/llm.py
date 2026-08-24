"""Cliente do llama-server (roda no FX). O i3 nunca carrega modelo:
ele só fala HTTP com o músculo."""

import json
import os
import re
import time

import requests

BASE_URL = os.environ.get("LLM_URL", "http://192.168.18.200:8085")
MODELO = os.environ.get("LLM_MODELO", "local")
TIMEOUT_S = int(os.environ.get("LLM_TIMEOUT", "420"))  # 7min: hardware lento


class ErroLLM(RuntimeError):
    pass


def esta_vivo(base_url: str = None) -> bool:
    try:
        r = requests.get(f"{base_url or BASE_URL}/health", timeout=5)
        return r.status_code == 200
    except requests.RequestException:
        return False


def esperar_ficar_vivo(timeout_s: int = 300, base_url: str = None) -> bool:
    """Depois do Wake-on-LAN o FX ainda precisa carregar o modelo na RAM.
    Nada de sleep fixo: pergunta até responder."""
    limite = time.monotonic() + timeout_s
    while time.monotonic() < limite:
        if esta_vivo(base_url):
            return True
        time.sleep(5)
    return False


def _extrair_json(texto: str) -> dict:
    """Rede de segurança: modelo pequeno às vezes enfeita a resposta."""
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
    """Uma chamada. Se `schema` vier, o llama.cpp converte para gramática GBNF
    e o modelo fica *impossibilitado* de sair do formato — é isso que faz um
    modelo 2B entregar JSON válido em 100% dos casos."""
    corpo = {
        "model": MODELO,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperatura,
        "cache_prompt": True,  # o system prompt fica em cache entre itens
    }
    if schema:
        corpo["response_format"] = {"type": "json_object", "schema": schema}

    t0 = time.monotonic()
    try:
        r = requests.post(
            f"{BASE_URL}/v1/chat/completions", json=corpo, timeout=TIMEOUT_S
        )
        r.raise_for_status()
        conteudo = r.json()["choices"][0]["message"]["content"]
    except requests.RequestException as e:
        raise ErroLLM(str(e)) from e
    except (KeyError, ValueError) as e:
        raise ErroLLM(f"resposta inesperada: {e}") from e
    duracao = time.monotonic() - t0

    return (_extrair_json(conteudo) if schema else conteudo.strip()), duracao
