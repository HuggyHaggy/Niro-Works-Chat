# Niro Chat

Aplicação comunitária com interface web responsiva, API FastAPI, banco SQLite e WebSocket para mensagens ao vivo.

## Executar localmente (Windows)

1. Instale Python 3.11 ou mais recente.
2. Execute `run_windows.bat`.
3. Abra a URL local mostrada no terminal.

## Publicação no Netlify

O Netlify publica apenas a interface estática na pasta `netlify_site`. O backend FastAPI, WebSocket e dados persistentes devem ser hospedados separadamente num serviço compatível com Python, WebSockets e armazenamento persistente. Siga `netlify_site/LEIA_ANTES_DE_PUBLICAR.md` e `GUIA_CONFIGURACAO_E_LANCAMENTO.md`.

Para produção, configure no backend `NIRO_ENV=production`, `NIRO_SECRET` (chave aleatória de pelo menos 32 caracteres), `NIRO_ALLOWED_ORIGINS` (domínios HTTPS exatos do frontend) e `NIRO_DATA_DIR` (volume persistente). Gere uma chave com `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Nunca coloque a chave no frontend ou no repositório.

## Limitações conhecidas

A galeria usa URLs de mídia; eventos ainda não incluem lembretes/RSVP; ideias não têm workflow completo; e anexos, reações, threads, prévias de links, notificações push e arquivamento automático não são módulos completos. Antes de abrir ao público, teste recuperação de conta, verificação de e-mail, antiabuso, monitoramento, backups e escala.
