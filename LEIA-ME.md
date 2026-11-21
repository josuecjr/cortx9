# CorteX9 2.3.0 — instalar e testar

## 1. Instalar (uma vez)

1. Descompacte `CorteX9-2.3.0-mac.zip`.
2. Clique com o **botão direito** em `Instalar CorteX9.command` → **Abrir** → **Abrir**.
   (O macOS bloqueia o duplo clique em scripts baixados. Se ainda bloquear: Ajustes do Sistema → Privacidade e Segurança → "Abrir Mesmo Assim".)
3. O Terminal instala Homebrew (se faltar), FFmpeg, Python, Whisper MLX e o modelo (~1,6 GB). Na primeira vez leva de 5 a 15 min.
4. No fim, o **CorteX9.app** abre sozinho. Ele fica em `~/Applications` — arraste para o Dock.

Para **atualizar**, rode o instalador novo do mesmo jeito: ele fecha o app aberto, atualiza e reabre. Dados, kits, pastas e chaves continuam.

## 2. Configurar (5 minutos)

**Conexões** — clique no botão **↗** de cada serviço para abrir a página onde se cria a chave, cole a chave, clique **Salvar** e depois **Testar**. Elas vão para o Keychain.

| Serviço | Obrigatório? | Onde pegar |
|---|---|---|
| Claude API | Recomendado (sem ela, o plano é feito localmente) | console.anthropic.com → API Keys |
| ChatGPT | Opcional (reserva) | platform.openai.com → API keys |
| Google Drive | Opcional | Instale o **Google Drive para desktop**: o app detecta a pasta sozinho. Para links, use uma conta de serviço com Drive compartilhado. |
| Base44 | Opcional | No app Base44 crie as entidades `CorteFicha`, `EpisodioCliente` e `CustoEpisodio`, depois pegue a chave da API, o ID e a URL do app. |
| ElevenLabs | Opcional | elevenlabs.io → Profile → API key |
| Epidemic Sound | Opcional | Token da Partner API (exige contrato de parceiro) |
| Celular | Opcional | Instale o app **ntfy** no celular, assine um tópico com nome difícil (ex.: `cortex9-joao-8f3k`) e cole o mesmo nome aqui |

**Kits por cliente** — edite o kit "Padrão" ou crie um por cliente: formatos, rotação do YoloBox, pasta do kit no Drive (o app acha `logo*.png`, `abertura*.mp4`, `encerramento*.mp4` e trilhas `.mp3/.wav`; nomeie trilhas pelo clima, ex. `epico_1.mp3`, `intimista.mp3`), legenda e efeitos.

**Pastas vigiadas** — a padrão é `~/Movies/CorteX9/Entrada`, ligada ao kit "Padrão". Crie uma pasta por cliente, se quiser.

> Na primeira vez que o app ler o pendrive ou o HD externo, o macOS pode pedir permissão. Se não pedir e der erro de permissão: Ajustes → Privacidade e Segurança → Acesso Total ao Disco → adicione o CorteX9.

## 3. Teste rápido (sem câmera)

Na tela **Fila**, clique em **Gerar episódio de teste**. O app cria um vídeo de ~3 min com fala sintetizada em português e duas "câmeras" com cores diferentes, e solta na pasta vigiada. A partir daí, o fluxo é o mesmo de um episódio real.

## 4. Teste completo com um episódio real

1. **Solte o arquivo**: copie o `.mp4` do YoloBox para `~/Movies/CorteX9/Entrada`. Não abra o app para nada.
2. **Detecção**: ~10 s depois do fim da cópia, o episódio aparece na **Fila** como "Na fila" → "Processando".
   - Teste a trava de cópia: comece a copiar um arquivo grande. Ele não pode entrar na fila antes da cópia acabar.
3. **Etapas automáticas** (acompanhe a barra): extração de áudio → transcrição Whisper → quadros-chave → IA diretora → limpeza/cor/legendas → prévias.
   - Estimativa para o M3 Pro (ainda não medida): episódio de 60 min ≈ 4–6 min de transcrição, 1–3 min de plano, prévias 540p em VideoToolbox de 10 a 20 s por variação.
4. **Notificação**: quando as prévias acabam, chega a notificação do macOS (e no celular, se configurado): "*N cortes prontos para aprovar (levou X min)*".
5. **Aprovação** (clique no episódio):
   - Confira: 10 a 15 cortes, 2 a 3 variações cada (V1/V2/V3), e pelo menos um card roxo **💡 corte criativo · experimental**.
   - Abra "Por que a IA escolheu isso" em alguns cortes.
   - Em um corte, escreva um **ajuste rápido** (ex.: `mais curto e outra LUT` ou `gancho: VOCÊ NÃO SABIA DISSO`) → Ajustar. Novas prévias chegam em instantes.
   - **Reprove** um corte com motivo.
   - Escolha variações diferentes em alguns cortes e clique **Aprovar tudo e renderizar**.
6. **Finais**: os cortes passam para "Renderizando final" → "Final pronto". Os arquivos ficam em `~/Movies/CorteX9/Finais/AAAA-MM-DD/cliente/projeto/`, um por formato do kit. Clique no formato do card para abrir no Finder.
7. **Conferir integrações**:
   - Drive: `Meu Drive/CorteX9/AAAA-MM-DD/cliente/projeto/`.
   - Base44: uma `CorteFicha` por corte aprovado; ao final, `EpisodioCliente` (o link do cliente aparece no topo do episódio) e `CustoEpisodio`.
   - **Custos**: custo do episódio e média.
   - **Biblioteca de estilo**: as decisões que você tomou. O próximo episódio já usa essas preferências.
8. **Teste de falha**: desligue o Wi-Fi e solte outro episódio. A transcrição, o plano local e as prévias continuam; os envios ficam em Conexões → "Envios na fila de reenvio" e saem sozinhos quando a rede volta.

## 5. Vídeo de fotos

1. Menu **Vídeo de fotos** → escolha a **pasta de fotos** e a **trilha** (MP3, WAV ou M4A).
2. Opcional: título, subtítulo, estilo (cinematográfico, dinâmico, elegante…), duração e formatos. Para 1920×1080, deixe marcado **16:9**.
3. Em **Onde salvar o vídeo**, escolha a pasta do computador (vazio = pasta do kit) e se o final vai para o Google Drive (e em qual pasta dentro de "Meu Drive"). Dá para mudar depois, na tela do projeto, antes de aprovar.
4. Clique **Criar vídeo**. A IA enquadra sem distorcer, ordena, sincroniza com as batidas, anima e cria os textos.
5. Quando chegar a notificação, compare **V1** e **V2**, peça um ajuste se quiser (ex.: `mais rápido`, `título: Viagem à Serra`, `duração: 45`) e clique **Aprovar e renderizar**.
6. O final vai para a pasta escolhida e, se marcado, para o Drive. A tela do projeto mostra os dois caminhos.

Fotos repetidas (mesmo nome, cópias "(1)", versão do WhatsApp, HEIC + JPG, foto girada) são removidas automaticamente — fica só a melhor. Cada foto recebe ajuste automático de cor, exposição, ruído e nitidez; o **Roteiro** mostra o que foi feito em cada uma.

Atalho: arraste uma **pasta de fotos** (com o MP3 dentro) para `~/Movies/CorteX9/Entrada`. O vídeo começa sozinho.

## Quando algo der errado: aba Log

1. Abra a aba **Log** (ou o botão **Log** do projeto com erro).
2. O erro mais importante já vem aberto, com o **detalhe técnico** (comando do FFmpeg e resposta dele).
3. Abra **Diagnóstico do Mac e do FFmpeg**: se aparecer `textos/legendas (libass): NÃO`, rode o instalador desta versão de novo.
4. Para pedir ajuda, clique **Copiar** (ou **Exportar .txt**) e cole/envie o texto.

## Onde ficam as coisas

- App: `~/Applications/CorteX9.app`
- Dados, prévias, LUTs e banco: `~/Library/Application Support/CorteX9/`
- Log: aba **Log** no app; arquivo completo em `~/Library/Application Support/CorteX9/logs/cortex9.log`; exportações vão para a Mesa
