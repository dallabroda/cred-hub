"""Cria o site de uma empresa nova (credtech#25). Chamado pelo cadastro, no worker credhub_provision:

	cd sites && ../env/bin/python -m cred_hub.provision_site <site> <db_name>

O banco e o usuário do site são criados pelo procedimento `credhub_admin.create_site_db` do MariaDB (ver
infra/hetzner/credhub/provisioner.sql no repositório da plataforma): o usuário de banco do cadastro
(CREDHUB_PROVISIONER_DB_USER/PASSWORD, por variável de ambiente, nunca na linha de comando) só pode executar esses
procedimentos, que aceitam apenas bancos `chsite` novos. Depois o Frappe instala o site sem precisar de root
(setup_db=False). Se algo falhar, desfaz tudo (banco, usuário de banco e pasta) para o endereço poder ser tentado
de novo.
"""

import os
import secrets
import sys

import frappe
from frappe.installer import _new_site
from frappe.utils import CallbackManager

APPS = ["crm", "cred_hub"]


def _admin_call(procedure, *args):
	"""Chama um procedimento de credhub_admin com o usuário de banco do cadastro."""
	import MySQLdb

	conf = frappe.get_conf()
	connection = MySQLdb.connect(
		host=conf.db_host or "localhost",
		port=int(conf.db_port or 3306),
		user=os.environ["CREDHUB_PROVISIONER_DB_USER"],
		password=os.environ["CREDHUB_PROVISIONER_DB_PASSWORD"],
		charset="utf8mb4",
	)
	try:
		placeholders = ", ".join(["%s"] * len(args))
		connection.cursor().execute(f"CALL credhub_admin.{procedure}({placeholders})", args)
		connection.commit()
	finally:
		connection.close()


def main(site, db_name):
	rollback = CallbackManager()
	# Como o `bench new-site`: o site ainda não tem site_config.json
	frappe.init(site, new_site=True)
	# Só hex: é o que o procedimento aceita
	db_password = secrets.token_hex(24)
	try:
		_admin_call("create_site_db", db_name, db_password)
		rollback.add(lambda: _admin_call("drop_site_db", db_name))
		_new_site(
			db_name,
			site,
			# Administrator não é usado no dia a dia (o responsável é o administrador); a CredTech troca a senha
			# com `bench set-admin-password` se precisar dar suporte
			admin_password=secrets.token_urlsafe(24),
			install_apps=APPS,
			setup_db=False,
			db_user=db_name,
			db_password=db_password,
			rollback_callback=rollback,
		)
		_admin_call("site_ready", db_name)
	except BaseException:
		# A conexão do instalador segura o banco do site (metadata lock): fechar antes de apagá-lo
		if getattr(frappe.local, "db", None):
			frappe.db.rollback()
			frappe.db.close()
		rollback.run()
		raise
	finally:
		frappe.destroy()


if __name__ == "__main__":
	main(*sys.argv[1:3])
