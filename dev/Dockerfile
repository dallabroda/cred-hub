# Imagem de desenvolvimento: a imagem do Cred Hub (Frappe + CRM) com este app instalado em modo editável.
ARG BASE_IMAGE=credtech/credhub:local
FROM ${BASE_IMAGE}
COPY --chown=frappe:frappe . /home/frappe/frappe-bench/apps/cred_hub
RUN cd /home/frappe/frappe-bench && (./env/bin/pip install -q -e apps/cred_hub || uv pip install --python env/bin/python -e apps/cred_hub)
