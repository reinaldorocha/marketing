# Revisão integral do sistema

Atualizado em 10 de novembro de 2026. Este arquivo registra achados, correções e validações da revisão iniciada após o erro na tela de grupos em produção.

## Progresso

| Área | Situação | Resultado |
| --- | --- | --- |
| Migrações e banco de dados | Em validação | Seis diferenças entre modelos e histórico de migrações foram identificadas. A mais urgente impedia a listagem de grupos porque a coluna `updated_at` de `WhatsAppGroupTag` não existia no PostgreSQL. |
| Interface em português | Em andamento | Traduzidos login, cadastro, perfil, painel inicial, configurações de espaço, menu principal, disparos, canais, contatos, fluxos, sequências e retornos de membros, organização, API, faturamento, análises e importação. Componentes compartilhados de filtros e diálogos também foram traduzidos. A revisão continua nas telas detalhadas e e-mails transacionais. |
| Fluxos, filas e integrações | Pendente | Será verificada a consistência de agendamento, idempotência, escopo por workspace e falhas de provedores. |
| Rotas, permissões e isolamento | Pendente | Será verificado o acesso por workspace e as rotas administrativas e públicas. |
| Produção | Pendente | Após cada lote, a implantação executará migrações, verificações do Django e testes de disponibilidade. |

## Achados confirmados

### Tela de grupos indisponível

**Sintoma:** `GET /w/<workspace>/groups/` retornava erro 500.

**Causa:** `WhatsAppGroupTag` herda o campo `updated_at`, mas a migração inicial não criou a coluna. O Django tentava selecioná-la e o PostgreSQL respondia `UndefinedColumn`.

**Correção:** foi criada a migração `whatsapp_groups.0003`, que inclui a coluna sem descartar dados existentes. A mesma migração fixa o estado dos gerenciadores de modelos no histórico.

### Tela de webhooks de leads indisponível

**Sintoma:** `GET /w/<workspace>/settings/lead-webhooks/` retornava erro 500.

**Causa:** a listagem tentava filtrar por `FlowStatus.PUBLISHED`, um membro que não existe no enum de fluxos. O estado que representa um fluxo publicado e disponível para automações é `FlowStatus.ACTIVE`.

**Correção:** a consulta passou a usar o estado válido e recebeu teste de regressão para garantir que a página abra e ofereça somente fluxos ativos.

### Histórico de migrações incompleto

O comando `makemigrations --check` encontrou alterações pendentes em `api`, `broadcasts`, `inbox`, `publishing` e `whatsapp_groups`. As migrações correspondentes foram adicionadas para que uma instalação nova e a VPS cheguem ao mesmo esquema.

### Idioma da interface

O documento HTML agora declara `pt-BR`. Foram removidos textos em inglês das telas de acesso, conta, espaço de trabalho, painel inicial, menu lateral e das etapas principais de criação e acompanhamento de disparos. A auditoria restante está registrada na tabela de progresso para evitar que telas secundárias fiquem de fora.

Os retornos de conexão, configuração e validação de WhatsApp, Telegram, SMS, Instagram, e-mail e canais também passaram para português. Mensagens técnicas de provedores continuam preservadas quando descrevem a falha original.

Nesta nova varredura foram corrigidas listas e filtros de contatos, fluxos e sequências, componentes comuns, canais e painéis de análises, endpoints e notificações. Permanecem em revisão as telas de detalhe, os formulários extensos e os e-mails transacionais, que serão tratados em lotes para preservar a revisão de cada texto.

O módulo de fluxos recebeu uma revisão específica: editor, importação, galeria de modelos, gatilhos, exportação, filtros e estados agora usam português. A migração `flows.0009` mantém os rótulos do banco sincronizados com os estados mostrados na interface.

### Editor de fluxos: canais e blocos

O editor agora identifica, em cada gatilho, os canais resolvidos para o fluxo. Ao criar uma etapa, os itens de envio mostram se funcionam em WhatsApp e Instagram ou se pertencem somente a SMS e e-mail. Ao editar **Enviar mensagem** ou **Fazer uma pergunta**, um painel de compatibilidade explica os formatos nativos de WhatsApp e Instagram, os limites de interação e a janela de 24 horas.

Também foram traduzidos os controles de etapas, prévia, gatilhos, ações, campos de mensagem, estados de salvamento e publicação. A validação continua no servidor, com a mesma tabela de capacidades dos provedores; a nova interface apenas deixa as regras visíveis antes do envio.

## Critério de encerramento

Esta revisão só será marcada como concluída depois de: migrações limpas; verificações do Django e testes relevantes aprovados; ausência de texto em inglês nas telas próprias; e confirmação de funcionamento da implantação em produção.
