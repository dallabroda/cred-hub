// Chamadas à API do cadastro (cred_hub.signup) usadas por /cadastro e /cadastro/confirmar.
const signupApi = (() => {
	function serverMessage(data) {
		// Erro do servidor: a mensagem traduzida vem em _server_messages (lista JSON de objetos JSON)
		try {
			const list = JSON.parse(data._server_messages || "[]");
			const last = list.length ? JSON.parse(list[list.length - 1]) : null;
			if (last && last.message) {
				// DOMParser não carrega nada (img onerror etc.), só tira as tags
				return new DOMParser().parseFromString(last.message, "text/html").body.textContent;
			}
		} catch (e) {
			// sem mensagem legível: quem chamou mostra a genérica
		}
		return null;
	}

	async function call(method, args, httpMethod) {
		const url = new URL(`/api/method/cred_hub.signup.${method}`, window.location.origin);
		const options = { method: httpMethod, headers: { Accept: "application/json" } };
		if (httpMethod === "GET") {
			Object.entries(args).forEach(([key, value]) => url.searchParams.set(key, value));
		} else {
			options.headers["Content-Type"] = "application/json";
			if (window.frappe && frappe.csrf_token) options.headers["X-Frappe-CSRF-Token"] = frappe.csrf_token;
			options.body = JSON.stringify(args);
		}
		let response;
		let data = {};
		try {
			response = await fetch(url, options);
			data = await response.json();
		} catch (e) {
			return { ok: false, message: null, excType: null };
		}
		if (!response.ok) {
			return { ok: false, message: serverMessage(data), excType: data.exc_type || null, status: response.status };
		}
		return { ok: true, data: data.message };
	}

	return {
		get: (method, args) => call(method, args, "GET"),
		post: (method, args) => call(method, args, "POST"),
	};
})();
