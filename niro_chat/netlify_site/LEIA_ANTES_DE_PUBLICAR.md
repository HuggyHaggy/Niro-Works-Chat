# Niro Chat no Netlify — antes de publicar

O Netlify publica a interface web estática em `netlify_site`. A API FastAPI, o WebSocket, os uploads e o banco SQLite precisam ficar em um serviço separado que suporte Python, WebSockets e armazenamento persistente. Publicar somente esta pasta não disponibiliza o backend.

1. Hospede o backend `main.py` em um serviço compatível e com volume persistente.
2. No backend configure as variáveis privadas:
   - `NIRO_ENV=production`
   - `NIRO_SECRET` = uma chave aleatória forte, com pelo menos 32 caracteres.
   - `NIRO_DATA_DIR` = diretório/volume persistente, conforme o provedor.
   - `NIRO_ALLOWED_ORIGINS` = domínio HTTPS exato do site Netlify, por exemplo `https://nome-do-site.netlify.app` (sem barra final). Para domínios próprios, inclua-os separados por vírgula.
3. Gere uma chave localmente, no PowerShell do Windows:
   `python -c "import secrets; print(secrets.token_urlsafe(48))"`
   Copie o resultado diretamente para a variável privada `NIRO_SECRET` do backend.
4. Edite `netlify_site/config.js` e defina `API_BASE` para a URL HTTPS pública do backend, sem acrescentar `/api`. Exemplo: `https://api.seudominio.com`.
5. Publique no Netlify a partir desta pasta do projeto; o `netlify.toml` já define `netlify_site` como diretório de publicação.
6. Teste criação de conta, login, mensagens ao vivo, avatares/uploads, backup e persistência após reiniciar o backend.

**Não use `40381810` como chave de produção.** É uma sequência numérica curta e já foi compartilhada na conversa. Não coloque a chave privada em `config.js`, HTML, JavaScript, Git, `netlify.toml` ou no ZIP.

## Limitações honestas antes de abrir ao público

- A galeria trabalha com URLs de mídia; não é um sistema completo de upload de arte/vídeo.
- Eventos não têm lembretes nem confirmação de presença.
- Anúncios são tipos de mensagem; ideias não têm um fluxo completo de aprovação/votação/estados.
- Enquetes permitem votar e alterar o voto; não equivalem a um sistema avançado de pesquisa.
- Anexos completos, reações, threads, prévias de links, notificações push e arquivamento automático não são módulos completos só porque existem opções de configuração.
- Ainda é necessário validar recuperação de senha, verificação de e-mail, controles antiabuso, monitoramento, cópias de segurança e escala antes de tratar o serviço como plataforma pública de produção.
- Netlify hospeda o frontend estático; o backend e o armazenamento precisam de hospedagem própria e persistente.
