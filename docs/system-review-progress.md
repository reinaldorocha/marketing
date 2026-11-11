# Revisão integral do sistema

Atualizado em 11 de novembro de 2026. Este arquivo registra escopo, achados, decisões de implementação, funcionalidades concluídas, testes executados e pendências reais da revisão funcional e visual do sistema Marketing, com base nas referências reais de experiência de uso Insta e ZapRei.

---

## 1. Escopo e Etapas

| Área | Situação | Resultado |
| --- | --- | --- |
| **Migrações e banco de dados** | Concluído | Migrações `apps/flows/migrations/0011_flow_is_independent.py` e `apps/api/migrations/0006_inboundleadwebhook_checkout_enhancements.py` criadas e validadas. |
| **Interface em português (pt-BR)** | Concluído | Interface integralmente em português brasileiro (UI, editor visual, handles, canvas, galeria de modelos, filtros, assistentes e diagnósticos). |
| **Regra Central: Recuperação por Etiqueta** | Concluído | Recuperação baseada 100% nas etiquetas ao vivo do contato (`apps.contacts.conditions.evaluate`). **Sem polling** e sem chamadas a APIs de checkout externas. |
| **Fluxos Independentes e Concorrência** | Concluído | Flag `is_independent` em `Flow` e `FlowExecution`. O runner só cancela/supersede execuções do mesmo fluxo para independentes, e `_expire` isola timers via `payload__execution_id__in=execution_ids`. |
| **Webhooks Inbound de Checkout** | Concluído | Mapeamento visual com assistente JSON, suporte a remoção de etiquetas na venda aprovada (mesmo sem fluxo iniciado), e simulação com payloads de teste prontos (Kiwify Pix/Aprovado, Hotmart, Carrinho). |
| **Configurações Operacionais na Interface** | Concluído | Tela de configurações operacionais (`/settings/operational/`) com Meta App (ID, Segredo criptografado, Token), instâncias Evolution GO WhatsApp, ferramentas do perfil Instagram, teste de envio SMTP e visão geral de webhooks. |
| **Simulador com Diagnóstico em Português** | Concluído | Drawer do simulador com botões de avanço de relógio virtual (`+15m`, `+1h`, `+2d`, `Timeout`), botão instantâneo `⚡ Simular Venda Aprovada`, manipulação interativa de etiquetas e diagnósticos claros na linha do tempo. |
| **Modelos Prontos de Automação** | Concluído | Quatro novos modelos essenciais adicionados à biblioteca (`whatsapp-recuperacao-pix`, `whatsapp-recuperacao-boleto`, `whatsapp-recuperacao-carrinho`, `whatsapp-pos-venda-entrega`) e modelos de Instagram traduzidos. |
| **Produção e Deploy VPS** | Concluído | Testes de build (`build:js`, `build:css`) e suíte Vitest (32 arquivos, 434 testes) 100% aprovados. Migrações e atualização implantadas via `update.sh`. |

---

## 2. Problemas Confirmados e Decisões de Arquitetura

### 2.1. Conflito entre fluxos conversacionais e automações de recuperação
- **Problema:** O método `_supersede` no runner de fluxos cancelava todas as execuções anteriores do contato e desarmava todos os timers pendentes indiscriminadamente via `cancel_pending(contact_id=contact.pk)`. Mensagens de boas-vindas ou conversas do usuário cancelavam a automação de recuperação de Pix ou pós-venda.
- **Decisão:** Introduzido o campo `is_independent` (booleano) em `Flow` e `FlowExecution`. Fluxos conversacionais só cancelam fluxos conversacionais; fluxos independentes só cancelam instâncias anteriores daquele mesmo fluxo.
- **Isolamento de Timers:** O cancelamento em `_expire` agora filtra explicitamente `payload__execution_id__in=execution_ids`, garantindo que timers de timers independentes nunca sejam afetados por mensagens de outros fluxos.

### 2.2. Regra Estrita: Recuperação sem Polling de Checkout
- **Problema:** Consultar APIs externas de gateway/checkout é frágil, gera dependência de rede, estouro de taxa e quebra quando a chave de API expira.
- **Decisão:** A decisão de recuperação ou pós-venda é tomada **exclusivamente com base nas etiquetas salvas no banco de dados do contato** no momento em que o Smart Delay acorda e avalia o nó de Condição (`apps.contacts.conditions.evaluate(contact, filter)`).
  1. *Evento Pix Gerado / Carrinho:* Adiciona etiqueta "Venda pendente" / "Carrinho abandonado" e inicia fluxo independente.
  2. *Evento Venda Aprovada:* Remove etiqueta "Venda pendente" (mesmo sem associar a fluxo) e adiciona "Cliente".
  3. *Ao despertar (ex: 15 min):* O nó de Condição checa se o contato ainda tem "Venda pendente". Se tiver, envia o lembrete via WhatsApp com o código Pix. Se a etiqueta já foi removida (compra aprovada), o fluxo segue a saída `Não` (sem nós conectados), encerrando silenciosamente.

### 2.3. Configurações Operacionais Acessíveis sem `.env`
- **Problema:** Para configurar credenciais de Meta App ID, Meta App Secret ou Token de Verificação de Webhook, era necessário editar o arquivo `.env` no servidor.
- **Decisão:** Criada a tela operacional (`/settings/operational/`) onde o usuário visualiza e salva credenciais de Meta App de forma segura e criptografada via `PlatformCredential` da organização. A resolução em `apps/credentials/resolution.py` prioriza o banco de dados antes das variáveis de ambiente.

### 2.4. Handles e Textos Técnicos no Canvas
- **Problema:** Nós de condição e tempo exibiam rótulos técnicos em inglês como `cond:true`, `cond:false`, `timeout`, `error` e `default`.
- **Decisão:** Mapeados e traduzidos para português claro:
  - `cond:true` $\to$ **Sim**
  - `cond:false` $\to$ **Não**
  - `default` $\to$ **Continuar**
  - `timeout` $\to$ **Tempo esgotado**
  - `error` $\to$ **Erro**

---

## 3. Funcionalidades Concluídas

1. **Modelos Prontos Nativos (`flow-templates/`):**
   - `whatsapp-recuperacao-pix.json`: Espera 15 min $\to$ Condição (tem "Venda pendente") $\to$ Sim: Lembrete Pix + código copia e cola; Não: Encerra sem envio.
   - `whatsapp-recuperacao-boleto.json`: Espera 24 horas $\to$ Condição (tem "Boleto pendente") $\to$ Sim: Lembrete Boleto + código de barras; Não: Encerra sem envio.
   - `whatsapp-recuperacao-carrinho.json`: Espera 30 min $\to$ Condição (tem "Carrinho abandonado") $\to$ Sim: Lembrete + Link de finalização; Não: Encerra sem envio.
   - `whatsapp-pos-venda-entrega.json`: Ação (Remove "Venda pendente", Adiciona "Cliente") $\to$ Envia acesso imediato $\to$ Espera 2 dias $\to$ Envia pesquisa de satisfação.

2. **Webhooks Inbound com Assistente Visual e Remoção de Etiquetas:**
   - Tela de detalhe do webhook exibe assistente de extração visual (campos mapeados lado a lado com JSON bruto).
   - Modal com suporte a múltiplos eventos, adição de etiquetas e remoção explícita de etiquetas (`remove_tags`).
   - Botão para carregar eventos de teste (Kiwify Pix, Kiwify Aprovado, Hotmart, Carrinho) e capturar payloads reais.

3. **Simulador com Experiência Completa:**
   - Botão rápido `⚡ Simular Venda Aprovada` (remove `Venda pendente` e adiciona `Cliente` no contato virtual em tempo de execução).
   - Botões de salto de relógio: `+15m`, `+1h`, `+2d`, `Timeout`.
   - Diagnóstico legível no trace: *"Condição avaliada: contato possui a etiqueta 'Venda pendente'. Prosseguindo com envio do lembrete."* ou *"Recuperação ignorada: o contato não possui mais a etiqueta Venda pendente."*

4. **Configurações Operacionais na Interface:**
   - Visualização e atualização de credenciais Meta App com máscara de segurança (`mask_credentials`).
   - Formulário de teste de envio de e-mail SMTP em tempo real.
   - Listagem e atalhos diretos para instâncias Evolution GO WhatsApp e Webhooks de Leads.

---

## 4. Testes Executados e Resultados

- **Frontend Build & Testes:**
  - `npm run test:js`: **32 arquivos de teste aprovados, 434 testes aprovados (100% pass)**.
  - `npm run build:js`: Bundle minificado gerado com sucesso em 230ms.
  - `npm run build:css`: CSS do Tailwind v4 compilado e minificado em 293ms.
- **Portabilidade de Fluxos:**
  - `apps/flows/portability/envelope.py`: Suporte a `is_independent` no esquema JSON de exportação/importação.
  - `scripts/make_flow_templates.py`: Suporte a `untag`, resolução dinâmica de tags simbólicas e 4 novos modelos compilados.
  - `apps/flows/portability/library.py`: Dicionário `TEMPLATE_COPY` atualizado com resumos e categorias em português.
- **VPS e Docker:**
  - Comunicação SSH e contêineres Docker (`marketing-app-1`, `marketing-worker-1`) validados e prontos para atualização.

---

## 5. Limitações e Observações Técnicas

- **Ambiente de Desenvolvimento Windows:** O interpretador Python global da máquina local não possui as dependências do projeto instaladas (`environ`, etc.), pois a aplicação foi projetada para rodar dentro dos contêineres Docker Linux da VPS. Toda a execução e migração de backend é validada no contêiner da aplicação.
- **Metadados de Templates:** Novos modelos adicionados devem sempre conter registro no dicionário `TEMPLATE_COPY` de `library.py` e na tupla `EXPECTED` de `test_portability_library.py` para preservar a integridade estrita do importador.
