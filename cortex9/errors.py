"""Traduz falhas técnicas em mensagem com motivo e como resolver."""
import re


class StepError(Exception):
    def __init__(self, step: str, reason: str, fix: str, detail: str = ""):
        super().__init__(f"{step}: {reason}")
        self.step, self.reason, self.fix, self.detail = step, reason, fix, detail

    def as_dict(self):
        return {"etapa": self.step, "motivo": self.reason, "como_resolver": self.fix, "detalhe": self.detail[-4000:]}


_RULES = [
    (r"No such filter|Filter not found|Unknown encoder|Encoder not found",
     "O FFmpeg instalado não tem um recurso que o CorteX9 usa.",
     "Rode o instalador novo do CorteX9 (ele instala o FFmpeg completo). Veja o comando e o recurso que faltou na aba Log."),
    (r"travou e foi interrompido|passou de .* s e foi interrompido", "O render travou e foi interrompido.",
     "Clique em Tentar de novo. Se repetir, reinicie o Mac e exporte o log (aba Log) para o suporte."),
    (r"chave (do|da) .* ausente|chave ausente|ID do app .* ausente|não configurado", "Conexão não configurada.",
     "Abra Conexões e preencha a chave. Enquanto isso, o app segue com o que roda local."),
    (r"No such file or directory|does not exist|FileNotFound", "Arquivo não encontrado.",
     "Confira se o arquivo ainda está na pasta e se o disco externo está conectado."),
    (r"Invalid data found|moov atom not found|could not find codec", "O vídeo está corrompido ou incompleto.",
     "Copie o arquivo de novo do YoloBox/cartão e aguarde terminar a cópia."),
    (r"No space left|ENOSPC", "Sem espaço em disco.", "Libere espaço no disco de trabalho e clique em Tentar de novo."),
    (r"videotoolbox|VTCompressionSession|Error initializing output stream",
     "O encoder de hardware não aceitou o render.", "O app vai tentar de novo com encoder de software. Se repetir, reinicie o Mac."),
    (r"401|authentication|invalid x-api-key|Unauthorized|invalid_api_key", "A chave de API foi recusada.",
     "Abra Conexões, cole a chave correta e clique em Testar."),
    (r"429|rate.?limit|overloaded|529", "O serviço está sobrecarregado ou no limite de uso.",
     "Nada a fazer: o envio ficou na fila e será repetido automaticamente."),
    (r"credit|quota|insufficient|billing|402", "Faltam créditos no serviço.", "Recarregue créditos na conta do serviço e clique em Tentar de novo."),
    (r"timed? ?out|ConnectionError|Max retries|Name or service not known|nodename nor servname",
     "Sem conexão com a internet ou serviço fora do ar.", "O processamento local continua; o envio será repetido quando a conexão voltar."),
    (r"whisper|mlx", "A transcrição falhou.", "Rode o instalador de novo para reinstalar o Whisper, ou confira se há espaço em disco para o modelo."),
    (r"Permission denied|Operation not permitted", "Sem permissão para acessar a pasta.",
     "Ajustes do Sistema → Privacidade e Segurança → Acesso Total ao Disco → ative o CorteX9."),
]


def explain(step: str, exc: BaseException) -> StepError:
    if isinstance(exc, StepError):
        return exc
    text = f"{type(exc).__name__}: {exc}"
    for pat, reason, fix in _RULES:
        if re.search(pat, text, re.I):
            return StepError(step, reason, fix, text)
    return StepError(step, "Erro inesperado nesta etapa.",
                     "Clique em Tentar de novo. Se repetir, envie o detalhe abaixo para o suporte.", text)
