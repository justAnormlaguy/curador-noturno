"""Liga e desliga o FX. É a peça que transforma 'PC ligado a noite toda'
em '40 minutos de FX por noite'."""

import os
import socket
import subprocess
import time

from . import llm

MAC_FX = os.environ.get("FX_MAC", "AA:BB:CC:DD:EE:FF")
HOST_FX = os.environ.get("FX_HOST", "192.168.18.200")
BROADCAST = os.environ.get("FX_BROADCAST", "192.168.18.255")
USUARIO_SSH = os.environ.get("FX_SSH_USER", "homeserver")
# Chave com nome fora do padrão (id_ed25519/id_rsa) NÃO é usada
# automaticamente pelo ssh. Como este código roda por um usuário de serviço,
# depender do ~/.ssh/config dele é frágil: melhor apontar a chave explícita.
CHAVE_SSH = os.environ.get("FX_SSH_KEY", "")


def _pacote_magico(mac: str) -> bytes:
    limpo = mac.replace(":", "").replace("-", "")
    if len(limpo) != 12:
        raise ValueError(f"MAC inválido: {mac}")
    return b"\xff" * 6 + bytes.fromhex(limpo) * 16


def acordar(esperar_s: int = 300) -> bool:
    """Manda o pacote mágico e espera o llama-server responder /health.
    Sem sleep fixo: o tempo de boot + carga do modelo varia."""
    if llm.esta_vivo():
        print("FX já está de pé.")
        return True

    pacote = _pacote_magico(MAC_FX)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(3):  # UDP não garante entrega
            s.sendto(pacote, (BROADCAST, 9))
            time.sleep(0.5)
    print(f"WoL enviado para {MAC_FX}; aguardando llama-server...")

    if llm.esperar_ficar_vivo(esperar_s):
        print("FX pronto.")
        return True
    print("! FX não respondeu a tempo.")
    return False


def _cmd_ssh(comando: str) -> list[str]:
    base = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if CHAVE_SSH:
        base += ["-i", CHAVE_SSH, "-o", "IdentitiesOnly=yes"]
    return base + [f"{USUARIO_SSH}@{HOST_FX}", comando]


def checar_ssh() -> bool:
    """Verifica o acesso SSH sem desligar nada. Rode ANTES de automatizar:
    se o dormir falhar em silêncio, o FX passa a noite ligado e você só
    descobre na conta de luz."""
    try:
        r = subprocess.run(
            _cmd_ssh("echo ok"), capture_output=True, timeout=20, text=True
        )
        if r.returncode == 0 and "ok" in r.stdout:
            print(f"✓ SSH para {USUARIO_SSH}@{HOST_FX} funciona sem senha.")
            return True
        print(f"✗ SSH falhou: {(r.stderr or r.stdout).strip()[:200]}")
    except subprocess.TimeoutExpired:
        print(f"✗ SSH para {HOST_FX} deu timeout (host desligado?)")
    except FileNotFoundError:
        print("✗ cliente ssh não instalado neste host: apt install openssh-client")
        return False
    if not CHAVE_SSH:
        print("  dica: se a sua chave tem nome fora do padrão, defina FX_SSH_KEY")
    return False


def dormir() -> bool:
    """Desliga o FX. Exige sudoers sem senha para /sbin/poweroff no FX."""
    try:
        subprocess.run(
            _cmd_ssh("sudo /sbin/poweroff"),
            check=True, capture_output=True, timeout=30,
        )
        print("FX desligando.")
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        # poweroff derruba a conexão SSH: código != 0 aqui é normal.
        print(f"desligamento solicitado ({type(e).__name__})")
        return True
    except FileNotFoundError:
        # Nunca deixe isso virar traceback: o dormir roda no finally do
        # 'noite', e uma exceção aqui esconderia o erro real da noite.
        print("! cliente ssh ausente — FX vai continuar LIGADO")
        return False
