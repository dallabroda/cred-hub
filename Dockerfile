# Hub Tech = Twenty (https://github.com/twentyhq/twenty, AGPL-3.0) com a marca da CredTech.
# A versão do Twenty é fixada por digest; para atualizar, troque TWENTY_IMAGE e confira o rebrand.
ARG TWENTY_IMAGE=twentycrm/twenty@sha256:6398458f42b760312aa59c4ade78cbaaa62063a0817e42cb21c66628fc310b3f
FROM ${TWENTY_IMAGE}

USER root
COPY brand /tmp/brand
COPY rebrand.mjs /tmp/rebrand.mjs
RUN node /tmp/rebrand.mjs /tmp/brand && rm -rf /tmp/brand /tmp/rebrand.mjs
USER 1000
