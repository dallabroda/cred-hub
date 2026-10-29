"""Pedido de integração com outra telefonia (credtech#259).

Na tela de Ligações e em Configurações > Telefonia o cliente vê as telefonias integradas e pode pedir outra
(operadora, link da documentação da API, como usa). Decisão do dono (29/10/2026): o pedido vira card no HubTech da
CredTech e aviso por e-mail para contato@. Os dois saem da mesma origem de leads (tipo webhook) do HubTech da
CredTech: ela cria o card na 1ª etapa do funil e a Notification da origem avisa contato@.

Endereço, origem e chave ficam em `credhub_telephony_request` no common_site_config ({url, source, key}), gravado
pelo deploy.sh credhub.
"""

import frappe
import requests
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import get_url

TIMEOUT = 10
SIZES = {"provider": 140, "docs_url": 500, "usage": 2000}


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=5, seconds=3600)
def request_integration(provider: str, docs_url: str | None = None, usage: str | None = None):
	provider = (provider or "").strip()[: SIZES["provider"]]
	if not provider:
		frappe.throw(_("Tell us which telephony company you use."))
	conf = frappe.conf.get("credhub_telephony_request") or {}
	if not all(conf.get(key) for key in ("url", "source", "key")):
		frappe.throw(_("Requests are not available right now. Write to contato@credtechdigital.com.br."))

	user = frappe.get_doc("User", frappe.session.user)
	body = {
		"fields": {
			"first_name": user.first_name,
			"last_name": user.last_name,
			"email": user.email,
			"mobile_no": user.mobile_no,
			"organization": frappe.db.get_single_value("FCRM Settings", "brand_name") or frappe.local.site,
		},
		"answers": {
			"Pedido": "Integração com outra telefonia",
			"Telefonia": provider,
			"Documentação da API": (docs_url or "").strip()[: SIZES["docs_url"]],
			"Como usa": (usage or "").strip()[: SIZES["usage"]],
			"HubTech do cliente": get_url(),
		},
	}
	try:
		response = requests.post(
			f"{conf['url']}?source={conf['source']}",
			json=body,
			headers={"X-CredHub-Key": conf["key"]},
			timeout=TIMEOUT,
		)
		ok = response.ok and (response.json().get("message") or {}).get("ok") is True
	except (requests.RequestException, ValueError):
		ok = False
	if not ok:
		frappe.log_error(title="Pedido de telefonia não chegou à CredTech", message=frappe.as_json(body))
		frappe.throw(_("We could not send your request. Write to contato@credtechdigital.com.br."))
	return {"ok": True}
