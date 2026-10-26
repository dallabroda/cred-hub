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
