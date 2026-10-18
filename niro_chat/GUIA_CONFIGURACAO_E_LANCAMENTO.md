# Niro Chat — Guia de configuração e lançamento

Este guia cobre a execução local no Windows, configuração inicial, uso dos novos recursos, backup e publicação em um servidor. O projeto é um aplicativo web FastAPI + HTML/CSS/JavaScript, com banco SQLite persistente.

## 1. Requisitos

- Windows 10/11 ou Linux para executar o servidor.
- Python 3.11 ou mais recente (Python 3.12/3.13 recomendado).
- Navegador atualizado.
- Internet apenas para instalação dos pacotes; a interface pode carregar fontes externas do Google Fonts.

## 2. Executar localmente no Windows

1. Extraia `niro_chat_hierarquia_moderacao.zip` para uma pasta permanente, por exemplo `C:\NiroChat`.
2. Instale Python pelo site oficial e marque a opção **Add Python to PATH** durante a instalação.
3. Abra a pasta do projeto no Explorador de Arquivos.
4. Na barra de endereço da pasta, digite `cmd` e pressione Enter.
5. Execute os comandos abaixo, um por vez:

```powershell
py -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

6. Configure uma chave secreta antes de iniciar (PowerShell):

```powershell
$env:NIRO_SECRET = python -c "import secrets; print(secrets.token_urlsafe(48))"
```

7. Inicie o servidor:

```powershell
uvicorn main:app --host 127.0.0.1 --port 8000
```

8. Abra **http://127.0.0.1:8000** no navegador. Para parar, volte ao terminal e pressione `Ctrl+C`.

O comando de `NIRO_SECRET` acima vale para aquela janela do PowerShell. Guarde a chave e use a mesma em reinicializações para evitar invalidar tokens existentes. Para uma configuração permanente, crie uma variável de ambiente do Windows chamada `NIRO_SECRET` ou use um script local privado que defina a chave antes de iniciar.

## 3. Primeiro uso

1. Clique em **Criar conta** e crie o primeiro usuário.
2. Crie uma comunidade e personalize nome, ícone e descrição.
3. Use **＋** na seção de canais para criar canais de texto.
4. Use o ícone de pastinha ao lado de **CANAIS DE TEXTO** para criar categorias, por exemplo `FA-ARTS`, `ANÚNCIOS` e `PROJETOS`.
5. Arraste um canal sobre uma pasta para organizá-lo. Arraste para a área de canais sem pasta para removê-lo da categoria.
6. Clique no nome ou na seta da pasta para abrir/fechar. O estado recolhido é guardado no navegador de cada pessoa.
7. Em **Vitrine**, personalize título, descrição e banner da comunidade.
8. Em **Eventos**, administradores podem publicar eventos com data, local/link e descrição.
9. Em **Galeria**, membros podem publicar criações usando um link público `http://` ou `https://` para a mídia. Use links diretos de imagem para que a prévia apareça.
10. No seletor ao lado do campo de mensagem, escolha **Mensagem**, **Anúncio**, **Enquete** ou **Ideia**. Para enquete, escreva a pergunta e depois separe as opções por vírgula. Cada usuário pode votar e alterar seu voto.
11. Nas configurações da comunidade, continuam disponíveis os controles de cargos, permissões e as opções avançadas já existentes.

## 4. Onde os dados ficam

Por padrão, o Niro Chat guarda dados fora da pasta do código:

- Windows: `%USERPROFILE%\.niro_chat\niro_chat.db`
- Fotos enviadas: `%USERPROFILE%\.niro_chat\uploads\`

Essa separação permite atualizar os arquivos do aplicativo sem apagar contas, mensagens, comunidades, pastas, eventos, galeria e cargos. **Não apague a pasta `.niro_chat`.** Se quiser mudar a localização, defina `NIRO_DATA_DIR` antes de iniciar o servidor; use sempre o mesmo caminho entre reinicializações.

## 5. Backup e restauração

- Dentro do perfil/configurações existe a opção de baixar um backup. Ele contém o banco SQLite e imagens de perfil.
- Guarde o ZIP em local seguro e privado. Faça backups antes de atualizar o projeto.
- Para restauração manual, pare o servidor, faça uma cópia da pasta atual de dados e restaure o banco e a pasta `uploads` no diretório de dados configurado. Só substitua o banco com o servidor parado.
- Os links externos publicados na galeria são referências; os arquivos desses links não são copiados para o backup.

## 6. Publicar para outras pessoas acessarem

**Não use `--host 0.0.0.0` diretamente numa rede pública sem HTTPS e sem configurar a implantação.** Para disponibilizar o Niro Chat na internet, use um VPS Linux com domínio, HTTPS e armazenamento persistente. Uma opção de configuração com Docker está no `Dockerfile` incluído.

Exemplo de variáveis que o servidor deve definir:

```bash
NIRO_SECRET='uma-chave-aleatoria-longa-e-privada'
NIRO_DATA_DIR='/data/niro_chat'
```

Com Docker, monte um volume persistente em `/data` e passe essas duas variáveis ao contêiner. O servidor da aplicação deve escutar internamente na porta 8000; um proxy reverso (por exemplo, Caddy ou Nginx) deve terminar TLS/HTTPS e encaminhar tráfego HTTP e WebSocket para a aplicação. Configure firewall para expor apenas 80/443 e SSH restrito, não a porta interna 8000.

### Checklist antes de abrir ao público

- [ ] Definir uma chave `NIRO_SECRET` longa, aleatória e privada; nunca publicar a chave no Git ou no ZIP.
- [ ] Usar HTTPS e proxy reverso com suporte a WebSocket.
- [ ] Configurar armazenamento persistente e confirmar que `/data/niro_chat` sobrevive a reinicializações e atualizações.
- [ ] Automatizar backups e testar a restauração.
- [ ] Atualizar o sistema operacional e dependências regularmente.
- [ ] Testar registro, login, permissões, convites, uploads, pastas, eventos, galeria e votos.
- [ ] Definir política de privacidade, termos de uso, contato de suporte e regras de moderação.
- [ ] Planejar rate limiting, proteção anti-spam, verificação de e-mail, recuperação de senha, logs e monitoramento antes de tratar a aplicação como serviço público em escala.

## 7. O que esta versão inclui

- Pastas de canais recolhíveis, arrastar/soltar e persistência no SQLite.
- Vitrine de comunidade com título, descrição e banner por URL.
- Eventos comunitários criados pela administração.
- Tipos de publicação: mensagem, anúncio, enquete com votos persistentes e ideia.
- Galeria comunitária com título, descrição e URL de mídia.
- Configurações anteriores de perfil, comunidade, canal e cargo.
- Migrações para preservar bancos de versões anteriores e manter apenas canais de texto.

## 8. Limitações honestas

Esta é uma base funcional para desenvolvimento e testes, mas ainda não é uma plataforma de produção em grande escala. A galeria usa URLs externas, não faz upload próprio de imagens de publicação. Eventos não têm lembretes automáticos nem RSVP. Anúncios/ideias são formatos de mensagem; não existe fluxo de aprovação ou votação para ideias. Enquetes aceitam uma opção por pessoa e permitem mudar o voto. Antes de uma abertura pública ampla, implemente os itens de segurança e operação do checklist.

## 9. Diagnóstico rápido

- **Não abre a página:** confira se o terminal mostra `Uvicorn running on http://127.0.0.1:8000` e se a porta 8000 está livre.
- **`py` não é reconhecido:** reinstale Python e marque `Add Python to PATH`, ou tente `python` em vez de `py`.
- **Módulo ausente:** ative o ambiente virtual e execute `pip install -r requirements.txt`.
- **Conta/comunidade parece sumir após atualizar:** confira se o processo usa o mesmo `NIRO_DATA_DIR` e se a pasta `.niro_chat` não foi apagada.
- **Outra pessoa não consegue acessar localmente:** `127.0.0.1` só aceita conexões do próprio computador. Para acesso público, configure VPS, domínio, HTTPS e proxy conforme a seção 6; não exponha diretamente o servidor de desenvolvimento.
- **Verificar API:** acesse `http://127.0.0.1:8000/api/health` localmente.

## 10. Hierarquia de cargos, permissões e moderação

1. Entre na comunidade como dono e abra **Configurações da comunidade → Gerenciar cargos e permissões**.
2. Escolha **Configurar hierarquia e permissões de todos os cargos**. Essa tela é exclusiva do dono.
3. A lista mostra os cargos do topo para baixo. Use as setas para reordenar; o Dono permanece no topo e Membro permanece no fim.
4. Marque ou desmarque permissões de cada cargo e clique em **Salvar hierarquia e permissões**. A hierarquia é verificada pela API, não apenas pela interface. Uma permissão desativada num nível superior bloqueia essa permissão para cargos inferiores.
5. Em cada canal, abra a engrenagem de configurações. **Somente leitura** impede mensagens de membros sem permissão de administrar canais. **Visível para todos os membros** desativado restringe o canal aos cargos de equipe autorizados. **Intervalo entre mensagens** configura o modo lento; escolha um cargo para aplicar o atraso a ele e aos cargos abaixo, deixando os cargos superiores isentos. O modo lento padrão é usado quando o intervalo principal está em zero.
6. Na lista de membros, os cargos autorizados podem castigar temporariamente, remover ou banir pessoas que estejam abaixo de seu próprio nível. Apagar mensagens aparece junto à mensagem para o autor e para cargos com permissão de exclusão.
7. Para reverter um banimento, abra **Configurações da comunidade → Gerenciar banimentos** e selecione **Desbanir**.

**Notas:** o modo lento e o estado de somente leitura são verificados tanto na API HTTP quanto no WebSocket. A configuração de visibilidade restringe a lista de canais e o acesso direto à API. Opções de anexos, reações, tópicos, prévias de links, notificações e arquivamento automático não são módulos completos nesta versão; não devem ser tratados como recursos já implementados.
