"""Chaves de API guardadas no Keychain do macOS (via keyring). Nunca em texto puro."""
import logging

log = logging.getLogger("cortex9.secrets")
SERVICE = "CorteX9"
_memory: dict[str, str] = {}
_backend_ok = None


def _keyring():
    global _backend_ok
    try:
        import keyring
        from keyring.backends import fail
        kr = keyring.get_keyring()
        if isinstance(kr, fail.Keyring):
            raise RuntimeError("sem backend")
        _backend_ok = True
        return keyring
    except Exception:  # Linux de teste sem cofre: só memória, nunca disco
        if _backend_ok is not False:
            log.warning("Keychain indisponível: chaves ficam só na memória desta sessão.")
        _backend_ok = False
        return None


def get(name: str) -> str | None:
    kr = _keyring()
    if kr:
        try:
            return kr.get_password(SERVICE, name)
        except Exception as e:
            log.error("Erro lendo %s do Keychain: %s", name, e)
            return None
    return _memory.get(name)


def set(name: str, value: str | None):
    kr = _keyring()
    if not value:
        delete(name)
        return
    if kr:
        kr.set_password(SERVICE, name, value)
    else:
        _memory[name] = value


def delete(name: str):
    kr = _keyring()
    if kr:
        try:
            kr.delete_password(SERVICE, name)
        except Exception:
            pass
    _memory.pop(name, None)


def has(name: str) -> bool:
    return bool(get(name))


def storage_name() -> str:
    _keyring()
    return "Keychain do macOS" if _backend_ok else "memória (sem Keychain neste sistema)"
