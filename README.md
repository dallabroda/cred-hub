# Cred Hub

App [Frappe](https://frappeframework.com) com as customizações da [CredTech](https://credtechdigital.com.br) para o [Frappe CRM](https://github.com/frappe/crm). É instalado em todas as empresas do **Cred Hub** (https://hub.credtechdigital.com.br).

Código aberto sob **AGPL-3.0**, a mesma licença do Frappe CRM, que exige publicar as modificações de software oferecido pela internet.

## O que faz

### Enriquecimento automático por CNPJ

Ao salvar uma **Organização** ou um **Lead** com CNPJ:

1. O CNPJ é formatado (`00.000.000/0000-00`). Se os dígitos verificadores não conferirem, o usuário recebe um aviso e nada é consultado; o salvamento não é bloqueado (decisão de 24/10/2026).
2. Em segundo plano, o CNPJ é consultado na [OpenCNPJ](https://opencnpj.org), com reserva na [BrasilAPI](https://brasilapi.com.br).
3. Os campos da seção **Dados da Receita (CNPJ)** são preenchidos: razão social, nome fantasia, situação, CNAE principal, natureza jurídica, **porte resumido** (ME, EPP, Demais, Não informado), **É MEI?** (Sim/Não), **início da atividade**, **tempo de mercado em anos** (recalculado todo dia), capital social, telefone e e-mail da Receita e a **data da consulta** (só o dia). **UF, cidade e bairro** também vêm da Receita, mas ficam na seção **Detalhes** (logo depois do Endereço, na Organização), não na seção da Receita (pedido do dono, 24/10/2026).
4. Na Organização, o **endereço** vira um Endereço padrão do Frappe ligado a ela (aparece em Detalhes; um endereço escolhido à mão não é trocado), e o **setor de atuação** é definido pelo CNAE principal com a tabela de-para de `cred_hub/cnpj/sectors.py` (~60 setores, por divisão ou grupo do CNAE). No Lead, o setor também é preenchido.
5. Na Organização, cada sócio (QSA) vira um **Contato** ligado a ela, sem duplicar.
6. No Lead, o campo Organização é preenchido com o nome fantasia (ou a razão social) se estiver vazio.
7. Um comentário na linha do tempo registra a consulta e a fonte.

Regras:
- **Idioma:** nomes internos dos campos e setores em inglês; a interface usa `cred_hub/locale/pt_BR.po` para quem está em pt-BR (decisão do dono). A v0.2 migrou os campos da v0.1 (nomes em português) e reprocessou os CNPJs existentes (`patches/v0_2/english_fieldnames.py`).
- Os campos da Receita refletem o CNPJ informado: mudar o CNPJ consulta e preenche de novo.
- Salvar sem mudar o CNPJ não consulta de novo, exceto se a última tentativa falhou por indisponibilidade das APIs.
- CNPJ inexistente é marcado como consultado, para não repetir a cada salvamento.
- As datas de consulta (CNPJ e presença digital) guardam só o dia; a v0.4 trocou os campos de data e hora (`*_checked_at`) por campos de data (`*_checked_on`), copiando o dia.

### Telas

- **Território** (região de vendas do Frappe CRM) não aparece em Lead, Negócio e Organização (painel, cadastro rápido e campos): a CredTech não usa (pedido do dono, 24/10/2026). O dado continua no banco.
- **Contato:** saudação, sobrenome, gênero e endereço saem do painel lateral por padrão (pedido do dono, 26/10/2026). Aplicado uma vez por site: se o admin recolocar algum pelo editor de layout, o próximo `migrate` não tira de novo.
- **Presença digital** não tem mais seção própria no painel (era redundante, 26/10/2026): o **LinkedIn** fica em Detalhes logo depois do Site (uma vez por site) e os funcionários aparecem só pela faixa padrão do CRM (`no_of_employees`).

### Presença digital (site, LinkedIn, funcionários)

Ao salvar um Lead ou uma Organização com nome de empresa, um job em segundo plano (fila longa, uma vez por registro; com CNPJ válido, espera a Receita) busca o **site oficial**, o **LinkedIn** e o **nº de funcionários**: Wikidata → busca na web (DuckDuckGo, `ddgs`) → link do LinkedIn no próprio site → contagem pública (Wikidata/web, porte grande) → **piso SEBRAE** pelo porte e CNAE da Receita. **Funcionários pelo LinkedIn desligados** por decisão do dono (24/10/2026): o LinkedIn bloqueia acesso automático e os termos de uso não permitem; só o endereço do perfil é guardado (`USE_LINKEDIN_EMPLOYEES` em `company/discover.py`). Preenche o LinkedIn, os campos de funcionários (`employee_*`, fora do painel) e o `no_of_employees` do CRM (o "1-10" padrão do CRM conta como vazio); não sobrescreve site, LinkedIn ou nº de funcionários que o usuário preencheu. Qualquer falha só vai para o log: nunca impede salvar.

Para não trazer homônimo, o LinkedIn achado pela busca só é aceito se o endereço do perfil tiver todas as palavras do nome; o do próprio site ou do Wikidata vale sempre.

### Padrões de cada site (`site_setup`)

`bench --site <site> execute cred_hub.site_setup.apply_from_env`: idioma pt-BR e BRL, acesso só por convite, traduções de etapas, conta de e-mail (Brevo) com boas-vindas e login Google/Microsoft/GitHub (Auth0). Segredos por variável de ambiente (`SMTP_*`, `MAILER_SENDER_EMAIL`, `AUTH0_*`); grupo sem variável é pulado. Rodado pelo deploy em todo site e pelo cadastro em cada empresa nova.

### Cadastro self-service de empresas (`signup`, páginas `/cadastro`)

Só no site central (`credhub_signup: 1` no site_config). O pedido só vira empresa depois da confirmação do e-mail; a criação roda na fila `credhub_provision` (`cred_hub.provision_site`, banco com prefixo `chsite`, usuário de banco `CREDHUB_PROVISIONER_DB_*` por variável de ambiente). Pedidos no doctype **Cred Hub Signup**; falha avisa os System Managers e pode ser tentada de novo com `cred_hub.signup.retry_provision`.

### Leads de anúncios e formulários (`meta_leads`, `lead_intake`)

Meta Lead Ads (conectar com Facebook pelo site central, webhook `leadgen`, tokens de página criptografados, rede de segurança a cada 10 min) e entrada por webhook para Google Ads, site (`public/js/lead-form.js`), Typeform e outros sistemas. Cada lead vira card no funil da origem, distribuído e sem duplicar (no crm). Variáveis: `FB_APP_ID`, `FB_APP_SECRET`, `CREDHUB_META_VERIFY_TOKEN`, `META_LOGIN_CONFIG_ID` (opcional).

### Boas-vindas pelo WhatsApp (`welcome`)

Lead novo de anúncio/formulário recebe um template aprovado pela Meta pelo Cred Chat da empresa (conta + chave de acesso de um administrador, criptografada; caixa e template por origem; desligado por padrão). Envio na fila depois do commit do card, sem repetição automática; resultado em "Últimos leads". Variável opcional: `CREDHUB_CHAT_URL` (padrão `https://chat.credtechdigital.com.br`).

### Painel do Cred Hub na conversa do Cred Chat (`chat_panel`, página `/chat-panel`)

Na conversa do Cred Chat, uma aba "Cred Hub" (Dashboard App) mostra os cards do contato e permite mudar **etapa** e **valor** sem sair do chat (credtech#45). Decisões do dono (26/10/2026):

- **Login:** usa a sessão do Cred Hub no mesmo navegador e as permissões do próprio usuário (só vê e altera os cards que já pode abrir). Cred Chat e Cred Hub são o mesmo site (`credtechdigital.com.br`), então o cookie vale dentro do iframe. Sem login, o painel mostra "Entrar no Cred Hub" (abre em nova aba; o login do Google não abre dentro de iframe).
- **Instalação automática:** ao conectar o Cred Chat (mesma conexão da boas-vindas), o app cria o Dashboard App na conta, ou reaproveita o que já aponta para cá; ao desconectar, tira. A chave precisa ser de um **administrador** do Cred Chat; com chave de agente a conexão funciona e o painel fica com o aviso em `panel_error`. Conexões anteriores ao painel são instaladas em segundo plano no `migrate`; `cred_hub.chat_panel.install_chat_panel` tenta de novo.
- **Sem card:** só avisa ("Nenhum card para este contato"); criar lead pela conversa é a credtech#43 (congelada).

Busca pelo telefone com a mesma regra da entrada de leads (número nacional, sem o 55 quando há mais de 11 dígitos; (48) e (38) com os mesmos 9 dígitos são pessoas diferentes) ou pelo e-mail, no próprio card ou num contato ligado a ele; abertos primeiro, até 20. A página só aceita ser posta em iframe pelo Cred Chat (`frame-ancestors`) e só ouve mensagens vindas dele. Etapa com campos obrigatórios ou motivo de perda mostra o erro do Cred Hub e volta à etapa anterior.

### Limite de usuários e cobrança dos extras (`billing`)

14 dias de teste com usuários ilimitados, contados da criação da empresa; depois, 1 usuário grátis por empresa e extras a R$ 60/mês pelo Asaas, no padrão de mercado (aumento libera na hora e cobra a diferença por link com 3 dias; diminuição vale no próximo vencimento; 3 dias de tolerância antes de pausar). **Desligado por padrão:** só vale com `credhub_billing: 1` no common_site_config; desligado, não há limite, a tela some e o Asaas não é chamado. Variáveis: `ASAAS_API_KEY`, `ASAAS_ENVIRONMENT`, `CREDHUB_ASAAS_WEBHOOK_TOKEN`; `credhub_unlimited_users: 1` deixa uma empresa sem limite.

### Bureaus: Assertiva Localize e CENPROT (`integrations`, credtech#290)

Contratos da própria empresa, configurados em **Configurações › Integrações › Bureaus** (Single `Cred Hub Provider Settings`, só System Manager). Toda consulta paga e toda operação no cartório ficam em **Cred Hub Bureau Query** (quem, quando, finalidade LGPD, código e resposta), que também é o cache: a mesma consulta dentro do prazo (padrão 30 dias) não é paga de novo, salvo pedido explícito de nova consulta paga. Feito pela documentação da pasta `api documentation` (Localize v3; manual CENPROT Empresas v2.2 de 2024), ainda sem teste com conta real.

- **Assertiva Localize** (`assertiva.py`, `localize.py`): botão **Buscar contatos** na Organização. Consulta o CNPJ (`idFinalidade` das configurações, padrão 2) e cria ou atualiza um Contato por sócio pessoa física (`cpf`, cargo em `designation`, `share_percentage`); reaproveita o sócio que a Receita cadastrou só com o nome. Com "Também buscar telefones e e-mails de cada sócio" (desligado por padrão: o cliente paga e decide), consulta cada CPF: telefones em E.164 (celular com WhatsApp e maior assertividade como principal, salvo se o celular atual também veio), fixo como telefone, e-mails sem duplicar, e `has_whatsapp`/`phone_assertiveness`/`phone_carrier` do celular principal. Telefone e e-mail da empresa só preenchem os da Receita vazios. Token OAuth2 no Redis (renova no 401); 429 e 5xx repetem com espera; erro da Assertiva volta como mensagem (sem `frappe.throw`, para o histórico ficar gravado).
- **CENPROT** (`cenprot.py`, `protest.py`): ligar a CENPROT cria o funil **Debt Collection** (Cobrança) com 8 etapas, os campos `protest_*` no formulário do funil e liga as etapas nas configurações. **Enviar a protesto** (botão do card ou entrar na etapa de envio, se o cliente ligou `auto_send_on_stage`, desligado por padrão): `EnviarTitulo` com o credor das configurações e o devedor da Organização do card (CNPJ, endereço da Receita) ou, sem ela, do contato principal com CPF; texto em ASCII maiúsculo cortado nos tamanhos do manual; `<token>` fora do `<titulo>`. Só título vencido e só com status vazio, INEXISTENTE ou DEVOLVIDO; número e "nosso número" vazios recebem os dígitos do código do card. **Consulta diária** (`daily_long`) e botão "Atualizar status": `ConsultarTitulo`; mudou o status, grava, move o card (CONFIRMADO → no cartório, PROTESTADO, PAGO, CANCELADO/RETIRADO → acordo), comenta e notifica o responsável. **Retirar** (botão ou etapa de acordo, se o cliente ligou `auto_withdraw_on_stage`, desligado por padrão): `OperacaoTitulo` REMOCAO, DESISTENCIA ou CANCELAMENTO conforme o status. Token da sessão no Redis; WS_ERR_002 autentica de novo uma vez. O cliente cadastra no IEPTB o IP de saída do servidor (`protest.server_ip`, padrão 2.31.80.97, `credhub_outbound_ip` no site_config troca), mostrado na tela.

## Instalação

```sh
bench get-app https://github.com/dallabroda/cred-hub
bench --site <site> install-app cred_hub
```

Requer Frappe 16 e Frappe CRM. Campos e layouts são criados na instalação e reaplicados em todo `bench migrate`.

## Testes

```sh
# Regras puras (sem Frappe)
python -m pytest cred_hub/tests/test_cnpj_mapping.py
# Integração (num bench com o CRM instalado)
bench --site <site> run-tests --app cred_hub
```

## Desenvolvimento

Ambiente local igual ao servidor (Frappe 16 + Frappe CRM, versões do repositório da plataforma em `infra/hetzner/credhub/frappe/`):

```sh
# 1. Imagem base (uma vez, ~20 min): instalador oficial frappe_docker com o apps.json do CRM
git clone https://github.com/frappe/frappe_docker && cd frappe_docker
docker build --build-arg FRAPPE_BRANCH=version-16 --secret id=apps_json,src=<apps.json> \
  -t credtech/credhub:local -f images/layered/Containerfile .

# 2. Stack com este app (de volta neste repositório)
docker compose -f dev/docker-compose.yml up -d --build
docker compose -f dev/docker-compose.yml exec backend bench new-site dev.localhost \
  --mariadb-user-host-login-scope=% --db-root-password dev --admin-password admin \
  --install-app crm --install-app cred_hub
docker compose -f dev/docker-compose.yml exec backend bench --site dev.localhost set-config allow_tests true
```

CRM em http://localhost:8090/crm (usuário `Administrator`, senha `admin`). Depois de mudar o código: `docker compose -f dev/docker-compose.yml up -d --build`.
