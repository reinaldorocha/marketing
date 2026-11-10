# Revisão integral do sistema

Atualizado em 10 de novembro de 2026. Este arquivo registra achados, correções e validações da revisão iniciada após o erro na tela de grupos em produção.

## Progresso

| Área | Situação | Resultado |
| --- | --- | --- |
| Migrações e banco de dados | Em correção | Cinco diferenças entre modelos e histórico de migrações foram identificadas. A mais urgente impedia a listagem de grupos porque a coluna `updated_at` de `WhatsAppGroupTag` não existia no PostgreSQL. |
| Interface em português | Em andamento | Iniciada a tradução dos estados de campanhas. A revisão cobre textos de telas, mensagens, formulários, rótulos de modelos e acessibilidade. |
| Fluxos, filas e integrações | Pendente | Será verificada a consistência de agendamento, idempotência, escopo por workspace e falhas de provedores. |
| Rotas, permissões e isolamento | Pendente | Será verificado o acesso por workspace e as rotas administrativas e públicas. |
| Produção | Pendente | Após cada lote, a implantação executará migrações, verificações do Django e testes de disponibilidade. |

## Achados confirmados

### Tela de grupos indisponível

**Sintoma:** `GET /w/<workspace>/groups/` retornava erro 500.

**Causa:** `WhatsAppGroupTag` herda o campo `updated_at`, mas a migração inicial não criou a coluna. O Django tentava selecioná-la e o PostgreSQL respondia `UndefinedColumn`.

**Correção:** foi criada a migração `whatsapp_groups.0003`, que inclui a coluna sem descartar dados existentes. A mesma migração fixa o estado dos gerenciadores de modelos no histórico.

### Histórico de migrações incompleto

O comando `makemigrations --check` encontrou alterações pendentes em `api`, `broadcasts`, `inbox`, `publishing` e `whatsapp_groups`. As migrações correspondentes foram adicionadas para que uma instalação nova e a VPS cheguem ao mesmo esquema.

## Critério de encerramento

Esta revisão só será marcada como concluída depois de: migrações limpas; verificações do Django e testes relevantes aprovados; ausência de texto em inglês nas telas próprias; e confirmação de funcionamento da implantação em produção.
