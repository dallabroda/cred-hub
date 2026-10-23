# Cred Hub

App [Frappe](https://frappeframework.com) com as customizações da [CredTech](https://credtechdigital.com.br) para o [Frappe CRM](https://github.com/frappe/crm). É instalado em todas as empresas do **Cred Hub** (https://hub.credtechdigital.com.br).

Código aberto sob **AGPL-3.0**, a mesma licença do Frappe CRM, que exige publicar as modificações de software oferecido pela internet.

## O que faz

### Enriquecimento automático por CNPJ

Ao salvar uma **Organização** ou um **Lead** com CNPJ:

1. O CNPJ é formatado (`00.000.000/0000-00`). Se os dígitos verificadores não conferirem, o usuário recebe um aviso e nada é consultado; o salvamento não é bloqueado (decisão de 24/10/2026).
2. Em segundo plano, o CNPJ é consultado na [OpenCNPJ](https://opencnpj.org), com reserva na [BrasilAPI](https://brasilapi.com.br).
3. Os campos da seção **Dados da Receita (CNPJ)** são preenchidos: razão social, nome fantasia, situação cadastral, data de abertura, CNAE principal, natureza jurídica, porte, capital social, telefone, e-mail e endereço.
4. Na Organização, cada sócio (QSA) vira um **Contato** ligado a ela, sem duplicar.
5. No Lead, o campo Organização é preenchido com o nome fantasia (ou a razão social) se estiver vazio.
6. Um comentário na linha do tempo registra a consulta e a fonte.

Regras:
- Os campos da Receita refletem o CNPJ informado: mudar o CNPJ consulta e preenche de novo.
- Salvar sem mudar o CNPJ não consulta de novo, exceto se a última tentativa falhou por indisponibilidade das APIs.
- CNPJ inexistente é marcado como consultado, para não repetir a cada salvamento.

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
