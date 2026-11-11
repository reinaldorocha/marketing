# VPS: PostgreSQL existente e Nginx Proxy Manager

Este modo usa o `stack-postgres` e o Nginx Proxy Manager (`root-app-1`) existentes
na rede Docker `stack_default`. Não inicia outro PostgreSQL, Redis ou proxy.
Requer Linux, root, Git, Python 3, curl e Docker com Compose v2 ou superior.
O NPM deve usar seu banco SQLite padrão e possuir um administrador ativo.

## Instalação

O domínio deve apontar para a VPS, com portas 80 e 443 acessíveis para o HTTPS.
Em uma VPS preparada, execute em um único comando:

```bash
curl -fsSL https://raw.githubusercontent.com/reinaldorocha/marketing/main/scripts/install.sh | bash -s -- --domain marketing.profjonathanrocha.com.br --admin-email SEU_EMAIL
```

Ou, em um clone da `main`:

```bash
sudo bash scripts/install.sh --domain marketing.profjonathanrocha.com.br --admin-email SEU_EMAIL
```

O script cria o banco `marketing` e um usuário sem privilégios administrativos,
gera chaves independentes, compila CSS/JavaScript e a imagem, aplica migrações,
inicia aplicação/worker e registra o domínio e certificado no NPM pela API.
Somente a aplicação recebe o alias `marketing-web`; não publica portas no host.
O proxy redireciona HTTP para HTTPS. O worker executa a fila continuamente.

O primeiro administrador recebe uma senha aleatória, disponível somente para root:

```bash
sudo cat /etc/marketing/initial-admin.txt
```

Troque a senha após entrar. Repetir `install` não redefine contas/senhas existentes.
O script não envia mensagens nem e-mails durante a instalação.

## Atualização

```bash
sudo bash /opt/marketing/scripts/update.sh
```

A atualização busca a `main` e aceita apenas avanço rápido, exige checkout limpo,
compila a nova imagem antes de parar os serviços, faz backup do banco, mídias,
configuração e revisão, aplica migrações e verifica aplicação, worker e HTTPS.
Uma trava impede duas instalações/atualizações simultâneas. Há uma breve interrupção
durante backup final, migrações e troca de contêineres. Imagens recebem o SHA Git.
Os backups ficam em `/var/backups/marketing`, acessíveis somente para root.
Não há exclusão automática de backups ou imagens antigas; acompanhe o disco.

## Configuração

As credenciais ficam fora do Git em `/etc/marketing/marketing.env` (modo 600).
Nunca substitua `SECRET_KEY` ou `ENCRYPTION_KEY_SALT` ao atualizar: elas protegem
as credenciais de canais já cadastradas. Guarde cópia protegida fora da VPS.
O arquivo aceita as demais variáveis descritas em `.env.example`, incluindo SMTP,
Meta e armazenamento. Após editá-lo, execute `update.sh` para recriar os serviços.

SMTP fica para uma etapa posterior: recuperação de senha, convites e notificações
por e-mail dependem dessa configuração. WhatsApp/Meta e outros canais também
dependem das respectivas credenciais, cadastradas depois da instalação.

O IP do NPM na rede é registrado em `TRUSTED_PROXIES`. Se o NPM for recriado com
outro IP, execute `update.sh` para atualizar essa confiança.

## Diagnóstico e recuperação

```bash
docker compose --project-name marketing --env-file /etc/marketing/marketing.env -f /opt/marketing/deploy/docker-compose.vps.yml ps
curl --fail https://marketing.profjonathanrocha.com.br/healthz
```

Cada pasta de backup contém `database.dump` (formato custom do PostgreSQL),
`marketing.env`, `docker-compose.vps.yml`, `revision-before.txt` e, quando existente,
`media.tar.gz`. O backup imediatamente anterior às migrações é feito com aplicação
e worker parados. O NPM também recebe backup SQLite antes da configuração do proxy.

Falhas de compilação mantêm a versão anterior executando. Se uma migração ou etapa
posterior falhar, o script informa o backup e não tenta reverter o banco sozinho.
Uma volta a imagens antigas pode ser incompatível com novas migrações: primeiro
pare aplicação e worker, preserve o estado da falha, analise o esquema e restaure
somente o banco `marketing`, mídias, chaves e revisão correspondentes quando necessário.
Nunca restaure o cluster compartilhado inteiro para reverter apenas este projeto.
Nunca execute `docker compose down -v` nem remova os volumes para atualizar.
