/*
 * Cred Hub: leads from the company's own website (credtech#51).
 *
 * The company pastes on every page of its site:
 *   <script src="https://<empresa>.credtechdigital.com.br/assets/cred_hub/js/lead-form.js"
 *           data-source="<source>" data-key="<key>" defer></script>
 * and adds `data-credhub` to each form that should create leads.
 *
 * - Whoever arrives from an ad brings utm_* / gclid / fbclid / li_fat_id in the address: they are kept in a
 *   first-party cookie for 90 days (the last campaign wins), with the page where the visit started, so a
 *   form sent days later still carries the campaign that brought the person.
 * - Only real submits are seen (a submit button or Enter); a form that the site sends with its own script
 *   without submitting it needs `data-credhub="only"` and a normal submit button.
 * - On submit the form keeps working as before (the site's own action or handler runs); the lead goes to
 *   Cred Hub in parallel. With `data-credhub="only"` Cred Hub is the only destination: the page stays,
 *   the form is cleared and the text in `data-credhub-success` (if any) replaces it.
 * - The key is public by nature (it sits in the page HTML): the server only accepts it from the site's
 *   registered address, limits sends per visitor and drops what fills the hidden `website` field (bots).
 * - Sent as text/plain so the browser makes a simple request (no CORS preflight).
 */
;(function () {
  'use strict'

  var script = document.currentScript
  if (!script) return
  var source = script.getAttribute('data-source')
  var key = script.getAttribute('data-key')
  if (!source || !key) return

  var endpoint =
    new URL(script.src).origin +
    '/api/method/cred_hub.lead_intake.receive?source=' +
    encodeURIComponent(source)
  var COOKIE = 'credhub_attribution'
  var DAYS = 90
  var CLICK_PARAMS = [
    'utm_source',
    'utm_medium',
    'utm_campaign',
    'utm_content',
    'utm_term',
    'gclid',
    'fbclid',
    'li_fat_id',
  ]
  // field the server expects empty (bots fill every field); the input has its own name so it never clashes
  // with a real "website" field of the site's form
  var HONEYPOT = 'website'
  var TRAP_INPUT = 'credhub_hp'

  // Attribution

  function readCookie() {
    var match = document.cookie.match(
      new RegExp('(?:^|; )' + COOKIE + '=([^;]*)'),
    )
    if (!match) return null
    try {
      return JSON.parse(decodeURIComponent(match[1]))
    } catch (e) {
      return null
    }
  }

  function writeCookie(value) {
    var expires = new Date(Date.now() + DAYS * 864e5).toUTCString()
    document.cookie =
      COOKIE +
      '=' +
      encodeURIComponent(JSON.stringify(value)) +
      '; expires=' +
      expires +
      '; path=/; SameSite=Lax' +
      (location.protocol === 'https:' ? '; Secure' : '')
  }

  function pageAddress() {
    // address without the query string: it may carry personal data
    return location.origin + location.pathname
  }

  function captureAttribution() {
    var params = new URLSearchParams(location.search)
    var fromAd = {}
    var found = false
    CLICK_PARAMS.forEach(function (name) {
      var value = params.get(name)
      if (value) {
        fromAd[name] = value.slice(0, 200)
        found = true
      }
    })
    var saved = readCookie()
    if (found) {
      // a new campaign replaces the old one (last click)
      fromAd.landing_page = pageAddress()
      writeCookie(fromAd)
      return fromAd
    }
    if (!saved) {
      saved = { landing_page: pageAddress() }
      writeCookie(saved)
    }
    return saved
  }

  var attribution = captureAttribution()

  // Form fields

  var FIELD_NAMES = {
    first_name: ['first_name', 'nome', 'primeiro_nome', 'firstname'],
    last_name: ['last_name', 'sobrenome', 'lastname'],
    full_name: ['name', 'full_name', 'nome_completo', 'your-name'],
    email: ['email', 'e-mail', 'e_mail', 'your-email'],
    mobile_no: [
      'phone',
      'telefone',
      'celular',
      'whatsapp',
      'mobile',
      'mobile_no',
      'tel',
      'your-phone',
    ],
    organization: ['company', 'empresa', 'organization', 'organizacao'],
  }

  function fieldOf(name) {
    var lower = String(name || '').toLowerCase()
    for (var field in FIELD_NAMES) {
      if (FIELD_NAMES[field].indexOf(lower) !== -1) return field
    }
    return null
  }

  function labelOf(form, input) {
    var label =
      (input.id && form.querySelector('label[for="' + input.id + '"]')) ||
      input.closest('label')
    var text = label ? label.textContent : input.placeholder || input.name
    return String(text || input.name)
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 140)
  }

  function collect(form) {
    var fields = {}
    var answers = {}
    var honeypot = ''
    Array.prototype.forEach.call(form.elements, function (input) {
      if (!input.name || input.disabled) return
      var type = (input.type || '').toLowerCase()
      if (['submit', 'button', 'file', 'password', 'reset'].indexOf(type) !== -1)
        return
      if ((type === 'checkbox' || type === 'radio') && !input.checked) return
      var value = String(input.value || '').trim()
      if (input.hasAttribute('data-credhub-trap')) {
        honeypot = value
        return
      }
      if (!value) return
      var field = fieldOf(input.name)
      if (field === 'full_name') {
        var parts = value.split(/\s+/)
        fields.first_name = fields.first_name || parts.shift()
        if (parts.length) fields.last_name = fields.last_name || parts.join(' ')
      } else if (field && !fields[field]) {
        fields[field] = value.slice(0, 140)
      } else {
        var label = labelOf(form, input)
        answers[label] = answers[label]
          ? answers[label] + ', ' + value
          : value.slice(0, 1000)
      }
    })
    return { fields: fields, answers: answers, honeypot: honeypot }
  }

  // Sending

  function send(form) {
    var data = collect(form)
    var body = { key: key, fields: data.fields, answers: data.answers }
    CLICK_PARAMS.concat(['landing_page']).forEach(function (name) {
      if (attribution && attribution[name]) body[name] = attribution[name]
    })
    body[HONEYPOT] = data.honeypot
    body.page = pageAddress()
    var payload = JSON.stringify(body)
    // keepalive: the send survives the site's own submit leaving the page
    if (window.fetch) {
      return fetch(endpoint, {
        method: 'POST',
        mode: 'no-cors',
        keepalive: payload.length < 60000,
        headers: { 'Content-Type': 'text/plain' },
        body: payload,
      })
    }
    if (navigator.sendBeacon) {
      navigator.sendBeacon(endpoint, payload)
    }
    return Promise.resolve()
  }

  function addTrap(form) {
    if (form.querySelector('[data-credhub-trap]')) return
    var trap = document.createElement('input')
    trap.type = 'text'
    trap.name = TRAP_INPUT
    trap.tabIndex = -1
    trap.autocomplete = 'off'
    trap.setAttribute('data-credhub-trap', '')
    trap.setAttribute('aria-hidden', 'true')
    trap.style.cssText =
      'position:absolute!important;left:-9999px!important;width:1px;height:1px;opacity:0'
    form.appendChild(trap)
  }

  function onSubmit(event) {
    var form = event.target
    if (!form || !form.matches || !form.matches('form[data-credhub]')) return
    var only = form.getAttribute('data-credhub') === 'only'
    if (only) event.preventDefault()
    send(form).then(
      function () {
        if (!only) return
        var success = form.getAttribute('data-credhub-success')
        form.reset()
        if (success) {
          var message = document.createElement('p')
          message.textContent = success
          form.replaceWith(message)
        }
      },
      function () {},
    )
  }

  function setup() {
    Array.prototype.forEach.call(
      document.querySelectorAll('form[data-credhub]'),
      addTrap,
    )
  }

  // capture phase: runs before handlers that stop the event
  document.addEventListener('submit', onSubmit, true)
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setup)
  } else {
    setup()
  }
  // forms added later (popups, page builders); at most once per frame
  var pending = false
  function setupSoon() {
    if (pending) return
    pending = true
    requestAnimationFrame(function () {
      pending = false
      setup()
    })
  }
  if (window.MutationObserver) {
    new MutationObserver(setupSoon).observe(document.documentElement, {
      childList: true,
      subtree: true,
    })
  }
})()
