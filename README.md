# Bot Roleta Ibura Apostas — painéis completos

## Instalação no Windows
1. Extraia o ZIP.
2. Abra o Prompt de Comando dentro da pasta.
3. Rode `py -m pip install -r requirements.txt`.
4. Renomeie `.env.example` para `.env` e coloque seu token em `DISCORD_TOKEN`.
5. Rode `py bot.py`.

## Intents e permissões
O bot não exige intents privilegiadas. Convide com os escopos `bot` e `applications.commands`. Permissões: Ver canais, Enviar mensagens, Incorporar links, Anexar arquivos, Ler histórico e Gerenciar cargos. O cargo do bot deve ficar acima de todos os cargos vendidos.

## Painéis para publicar
- `/painel_roleta` — Roleta, Girar, Retomar giro pendente e Perfil.
- `/painel_comprar_giros` — menu de pacotes de giros comprados com Coins.
- `/painel_ibura_cash` — Ibura Apostas Cash, compra de Coins via PIX e perfil.
- `/painel_ibura_plus` — loja de cargos comprados com Coins.
- `/imagem_painel` — imagem individual dos painéis.

## PIX próprio
- `/configurar_qrcode` — envie a imagem do seu QR Code.
- `/comprar_coins` — escolha o pacote e gere um pedido pendente.
- `/enviar_comprovante` — envie o link HTTPS do comprovante.
- `/listar_pendentes` — veja pedidos aguardando conferência.
- `/aprovar_compra` — após conferir o pagamento, credita Coins automaticamente.

O QR Code é estático e não confirma pagamentos sozinho. A conferência é manual. Os pacotes PIX são definidos em `COIN_PACKAGES` no `bot.py`.

## Administração
`/adicionar_premio`, `/listar_premios`, `/remover_premio`, `/coins_admin`, `/giros_admin`, `/adicionar_cargo_loja`, `/remover_cargo_loja`, `/configurar_logs`, `/ranking_coins`.


## Painel Call Privada
- Publique com `/painel_call_privada`.
- Configure uma imagem com `/imagem_painel` escolhendo **Call Privada**.
- O botão **Comprar** abre um formulário com nome e duração.
- A duração mínima é 7 dias; o custo é 20 Coins por cada período de 7 dias iniciado.
- Após comprar, o usuário clica em **Criar Call** para criar um canal de voz privado.
- **Configurar** permite alterar nome e limite de usuários.
- Administradores podem definir a categoria padrão das novas calls com `/configurar_categoria_calls` e selecionar uma categoria do servidor. As próximas calls privadas serão criadas dentro dela.
- O bot precisa da permissão **Gerenciar Canais**. O canal fica oculto para @everyone e visível ao comprador e ao próprio bot.

### Transferência de Coins
No perfil, clique em **Transferir**, informe a menção ou o ID de um membro do mesmo servidor e a quantidade inteira de Coins. O bot valida o saldo, não permite transferir para si mesmo ou para bots, registra as duas movimentações no histórico e avisa o destinatário por mensagem privada (quando permitido).
