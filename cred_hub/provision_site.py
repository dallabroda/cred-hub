"""Cria o site de uma empresa nova (credtech#25). Chamado pelo cadastro, no worker credhub_provision:

	cd sites && ../env/bin/python -m cred_hub.provision_site <site> <db_name>

Faz o mesmo que `bench new-site`, com duas diferenças: as senhas vêm de variáveis de ambiente
(CREDHUB_PROVISIONER_DB_USER/PASSWORD) e não da linha de comando, onde ficariam visíveis na lista de processos;
e, se algo falhar, desfaz tudo (banco, usuário de banco e pasta), para o endereço poder ser tentado de novo.
O `bench new-site` só desfaz se alguém responder "sim" no terminal.
"""

import os
import secrets
import sys

import frappe
from frappe.installer import _new_site
from frappe.utils import CallbackManager

APPS = ["crm", "cred_hub"]


def main(site, db_name):
	rollback = CallbackManager()
	# Como o `bench new-site`: o site ainda não tem site_config.json
	frappe.init(site, new_site=True)
	try:
		_new_site(
			db_name,
			site,
			db_root_username=os.environ["CREDHUB_PROVISIONER_DB_USER"],
			db_root_password=os.environ["CREDHUB_PROVISIONER_DB_PASSWORD"],
			# Administrator não é usado no dia a dia (o responsável é o administrador); a CredTech troca a senha
			# com `bench set-admin-password` se precisar dar suporte
			admin_password=secrets.token_urlsafe(24),
			install_apps=APPS,
			rollback_callback=rollback,
			mariadb_user_host_login_scope="%",
		)
	except BaseException:
		rollback.run()
		raise
	finally:
		frappe.destroy()


if __name__ == "__main__":
	main(*sys.argv[1:3])
