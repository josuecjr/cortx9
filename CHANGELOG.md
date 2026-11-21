# Histórico de versões do CorteX9

## 2.2.1 — Instalador de atualização · 21/11/2026

- O instalador agora serve também para **atualizar**: fecha o CorteX9 se estiver aberto, mostra a versão instalada, atualiza as dependências e reabre o app. Dados, kits, pastas vigiadas, biblioteca de estilo e chaves do Keychain são mantidos.

## 2.2.0 — Fotos sem repetição e com qualidade garantida · 21/11/2026

### Novo
- **Fotos repetidas nunca entram duas vezes.** O app identifica e mantém só uma:
  - pelo **nome do arquivo**: `IMG_1234.jpg`, `IMG_1234 (1).jpg`, `IMG_1234 cópia.jpg`, `IMG_1234.HEIC` contam como a mesma foto — a não ser que a imagem seja claramente outra (ex.: duas câmeras com a mesma numeração);
  - pelo **conteúdo da imagem**: arquivo idêntico, a mesma foto reduzida/recomprimida (ex.: exportada do WhatsApp), convertida de HEIC para JPG ou salva girada. A comparação usa a estrutura da imagem **e** as cores, para não confundir fotos de mesma composição;
  - fica a versão de **maior resolução**, depois a mais nítida, depois a de nome original. A lista do que foi removido aparece no projeto.
- **Ajuste automático de qualidade em cada foto**, medido foto a foto: balanço de branco, níveis com ganho limitado (corrige sem "virar outra foto"), exposição (clareia foto escura e recupera foto clara demais, com moderação), contraste, vibração de cor, redução de ruído em fotos escuras com grão e nitidez proporcional à ampliação.
  - Fotos de **baixa resolução** recebem upscale por IA no Magnific (se configurado; até 15 por vídeo) ou upscale local de alta qualidade.
  - Fotos **tremidas/borradas** são sinalizadas e a IA diretora evita usá-las em destaque.
  - O roteiro mostra os ajustes aplicados em cada foto.

## 2.1.0 — Vídeo de fotos · 21/11/2026

### Novo
- **Vídeo de fotos**: escolha uma pasta de fotos e uma trilha. O CorteX9 monta um vídeo completo, não um slideshow.
  - **Enquadramento sem distorção** em 1920×1080 (16:9), 9:16, 1:1 ou 4:5: foto com proporção parecida ganha recorte inteligente no ponto de interesse; foto muito diferente (ex.: vertical num vídeo horizontal) entra inteira sobre um fundo desfocado dela mesma, com sombra.
  - Respeita a orientação do celular (EXIF) e lê HEIC do iPhone. Correção automática de cor por foto (balanço de branco e níveis) antes da LUT.
  - **Sincronia com a música**: o app detecta BPM, batidas, energia e picos da trilha; as trocas de foto caem nas batidas, mais rápidas nas partes intensas.
  - **IA diretora com visão** escolhe ordem (cronológica pela data da foto quando faz sentido), quanto tempo cada foto fica, movimento de câmera (zoom, pan, deriva) no ponto de foco, transições (fade, dissolve, smooth, zoom, círculo, flash, corte seco…), LUT e efeitos.
  - **Textos e motion graphics**: título com desfoque e barra animada, subtítulo, frases de destaque com "pop", legendas que sobem, *lower thirds* com caixa deslizante e texto final.
  - 2 versões para aprovar (principal e dinâmica, com outra LUT), ajuste rápido em texto livre ("mais rápido", "sem texto", "título: …", "duração: 60") e render final em alta, também na fila.
  - **Largar e esquecer**: solte uma subpasta de fotos (com um MP3 dentro, se quiser) numa pasta vigiada e o vídeo começa sozinho quando a cópia termina.
  - Logo do kit, arquivamento no Drive, ficha no Base44, notificação e custo, como nos cortes.

## 2.0.0 — Modo automático ("largar e esquecer") · 21/11/2026

Reconstrução do CorteX9 como app nativo de Mac com fluxo totalmente automático: o usuário solta o arquivo da câmera e só volta para aprovar.

### Novo
- **Pasta vigiada**: pastas de entrada configuráveis. O episódio só começa quando o arquivo para de crescer (3 leituras iguais, ~9 s) e o FFmpeg consegue abri-lo. Nada é perguntado.
- **Kit por cliente/projeto**: formatos de saída, rotação do YoloBox, trilha, logo, abertura, encerramento, estilo de legenda, efeitos, faixa de cortes e duração, voz, fonte da trilha, capa Magnific, pasta de saída e contexto para a IA. Cada pasta vigiada está ligada a um kit.
- **Fila em segundo plano** com três trilhas (análise, prévias ×2, finais). Vários episódios na fila; a interface nunca trava; jobs interrompidos voltam à fila quando o app reabre.
- **Pré-render para aprovação**: prévias 540p, bitrate baixo, VideoToolbox (h264) em tempo real. O final em alta (16 Mb/s, lanczos + nitidez leve no upscale) só roda depois da aprovação, também na fila, em todos os formatos do kit.
- **Variações criativas**: 2 a 3 por corte, com gancho, trilha, efeitos e LUT diferentes (uma variação sempre troca a LUT).
- **Corte ousado por lote**: todo episódio tem pelo menos um corte experimental 💡 (cold open, noir, letterbox, glitch), marcado na tela.
- **Biblioteca de estilo**: aprovações, reprovações, variações preteridas e pedidos de ajuste (gancho, duração, trilha, LUT, efeitos, nota da IA) viram referência para os próximos planos e para o plano local.
- **Notificação** do macOS e no celular (ntfy) quando as prévias ficam prontas, com nº de cortes e tempo total; e quando os finais terminam.
- **Tela de aprovação em lote**: grade com todas as prévias, troca de variação, aprovar, reprovar (com motivo), ajuste rápido em texto livre e botão "Aprovar tudo e renderizar".
- **Erros explicados**: cada falha mostra etapa, motivo e como resolver; o resto da fila continua. Botão "Tentar de novo".
- **Cor automática**: balanço de branco (mundo cinza), exposição/contraste e igualação entre câmeras (`normalize` por cena) antes da LUT. Biblioteca de 6 LUTs prontas com clima: épico, intimista, noir, sonho, saturado, dessaturado. A IA escolhe; o FFmpeg aplica.
- **IA diretora**: quadros-chave + transcrição + kit + biblioteca de estilo vão para o Claude (visão, saída JSON estruturada). Volta um plano por corte: início/fim, gancho, tema, LUT, clima e entrada/saída da trilha, zooms, textos animados, efeitos, transições, estrutura, justificativa e variações. A justificativa fica guardada no corte ("Por que a IA escolheu isso").
- **Limpeza de fala**: remove respiros longos, vícios ("ééé", "hã", "hum") e palavras gaguejadas; tratamento de voz local (filtro, redução de ruído, compressor, loudnorm −14 LUFS) e ElevenLabs opcional.
- **Legendas** palavra a palavra com destaque, revisadas pelo Claude (ortografia, acentos, nomes).
- **Trilha com ducking** (sidechain) que abaixa sob a voz, com fade de entrada e saída nos pontos do plano.
- **Tela de Conexões** com status (conectado, com erro, faltando chave) e botão Testar: Claude (com ChatGPT de reserva), Magnific, ElevenLabs, Epidemic Sound, Google Drive, Base44 e celular. Chaves no Keychain do macOS.
- **Arquivamento no Drive** em `CorteX9/AAAA-MM-DD/cliente/projeto`, pela pasta do Google Drive para desktop ou conta de serviço (gera link).
- **Base44**: ficha técnica de cada corte aprovado (`CorteFicha`), link do cliente com o episódio e os cortes (`EpisodioCliente`) e custo por episódio com média (`CustoEpisodio`).
- **Custo por episódio**: tokens e créditos por serviço, preços editáveis.
- **Fila de reenvio**: se uma conexão cai, o app segue com o que roda local e reenvia depois (espera exponencial até 1 h).
- **Sem nuvem, sem travar**: sem chave ou sem internet, o plano é feito localmente (densidade de fala, palavras de impacto e histórico de aprovações).

### Técnico
- App nativo (janela WebKit própria via pywebview, sem navegador), instalado em `~/Applications/CorteX9.app`.
- Processamento local: Whisper MLX (`whisper-large-v3-turbo`) no Apple Silicon, FFmpeg + VideoToolbox, `caffeinate` enquanto o app está aberto.
- Dados em `~/Library/Application Support/CorteX9` (SQLite, prévias, LUTs, logs).
