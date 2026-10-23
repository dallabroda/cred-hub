# Hub Tech

CRM da [CredTech](https://credtechdigital.com.br), disponível em https://hub.credtechdigital.com.br.

O Hub Tech é o [Twenty](https://github.com/twentyhq/twenty) (licença **AGPL-3.0**) com a marca da CredTech. Este repositório contém **todas** as modificações feitas sobre a imagem oficial do Twenty, como exige a AGPL para software oferecido pela rede:

| Arquivo | O que faz |
|---|---|
| `Dockerfile` | Parte da imagem oficial `twentycrm/twenty` (versão fixada por digest) e aplica o `rebrand.mjs` |
| `rebrand.mjs` | Troca o nome "Twenty" por "Hub Tech" nos textos de tela, e-mails e metadados; troca ícones, logo e links de documentação |
| `brand/` | Ícones e logos da CredTech |
| `scripts/gerar_icones.py` | Gera `brand/icons/` nos mesmos tamanhos dos ícones originais do Twenty |

Nenhuma lógica do Twenty é alterada. O código-fonte do Twenty está em https://github.com/twentyhq/twenty, na versão correspondente ao digest do `Dockerfile`.

## Gerar a imagem

```sh
docker build -t hub-tech .
```

## Licença

As modificações deste repositório seguem a mesma licença do Twenty: [AGPL-3.0](LICENSE). Os logotipos e a marca CredTech em `brand/` são de propriedade da CredTech e não estão licenciados para uso por terceiros.
